"""Settings — GET redacts secrets; PUT round-trips and stores secrets encrypted."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import AuthSession, User
from plex_manager.web.deps import (
    KNOWN_SETTING_KEYS,
    PLEX_MACHINE_ID_SETTING,
    AuthContext,
    AuthMethod,
    SettingsStore,
    get_anime_movie_root_optional,
    get_anime_tv_root_optional,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_eviction_enabled,
    get_eviction_grace_days,
    get_eviction_interval_minutes,
    get_eviction_proactive_enabled,
    get_log_retention_days,
    get_movies_root_optional,
    get_tv_root_optional,
    hash_session_token,
    load_system_settings,
    require_api_key,
)
from plex_manager.web.schemas import SettingsResponse, SettingsUpdate

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "settings-key"
# A throwaway Plex credential for the identity-cache tests. Held in a NAME (not an
# inline keyword literal) so ruff's S106 secret-in-call heuristic stays quiet — it
# is a fixture value, never a real secret.
_SEED_PLEX_TOKEN = "seed-plex-token"  # noqa: S105 — test fixture value, not a credential


def test_every_known_setting_key_has_a_response_and_update_field() -> None:
    """Regression guard for the operability beta's original defect: every
    ``KNOWN_SETTING_KEYS`` entry (what ``SettingsStore.redacted()`` always
    returns a value for) must be a real field on BOTH ``SettingsResponse`` and
    ``SettingsUpdate`` -- otherwise it is readable/writable only via a direct
    DB edit, which violates the "100% web-operable" north star. The 7
    eviction/log-retention settings were once present in ``KNOWN_SETTING_KEYS``
    but absent from both schemas."""
    for key in KNOWN_SETTING_KEYS:
        assert key in SettingsResponse.model_fields, f"{key} missing from SettingsResponse"
        assert key in SettingsUpdate.model_fields, f"{key} missing from SettingsUpdate"


def test_settings_update_rejects_target_above_threshold() -> None:
    # R2-2: a disk_pressure_target above the trigger threshold makes every root in the
    # [threshold, target] band read "under pressure" yet select nothing -> a silent
    # dead band. When both are sent together it must be a visible 422, not accepted.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=90.0)
    # equal and below the threshold are both fine.
    SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=80.0)
    SettingsUpdate(disk_pressure_threshold_percent=80.0, disk_pressure_target_percent=70.0)


async def test_get_starts_empty(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    body = response.json()
    assert body["plex_url"] is None
    assert body["tmdb_api_key"] is None


async def test_get_starts_with_tv_root_unset(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.json()["tv_root"] is None


async def test_put_tv_root_round_trips_independently_of_movies_root(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # tv_root is a plain (non-secret) path, just like movies_root, and settable
    # without touching movies_root -- the two roots are independently optional.
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings", json={"tv_root": "/library/tv"}, headers={"X-Api-Key": _API_KEY}
    )
    assert put.status_code == 200
    assert put.json()["tv_root"] == "/library/tv"
    assert put.json()["movies_root"] is None

    got = (await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})).json()
    assert got["tv_root"] == "/library/tv"


async def test_put_round_trips_and_redacts(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    update = {"plex_url": "http://plex.local:32400", "tmdb_api_key": "super-secret-key"}
    put = await client.put("/api/v1/settings", json=update, headers={"X-Api-Key": _API_KEY})
    assert put.status_code == 200
    put_body = put.json()
    assert put_body["plex_url"] == "http://plex.local:32400"
    assert put_body["tmdb_api_key"] == "***"
    assert "super-secret-key" not in put.text

    # GET reflects the same redacted view.
    got = (await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})).json()
    assert got["plex_url"] == "http://plex.local:32400"
    assert got["tmdb_api_key"] == "***"


# --------------------------------------------------------------------------- #
# Repointing Plex invalidates the cached server identity (post-init sign-in     #
# trusts PLEX_MACHINE_ID_SETTING; a changed plex_url/plex_token must re-derive) #
# --------------------------------------------------------------------------- #
async def _seed_plex_identity(
    sessionmaker_: SessionMaker,
    *,
    plex_url: str,
    machine_id: str,
    plex_token: str = _SEED_PLEX_TOKEN,
) -> None:
    """Store a plex_url + plex_token + cached machine id, as setup-complete would."""
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("plex_url", plex_url)
        await store.set("plex_token", plex_token)
        await store.set(PLEX_MACHINE_ID_SETTING, machine_id)
        await session.commit()


async def _stored_machine_id(sessionmaker_: SessionMaker) -> str | None:
    async with sessionmaker_() as session:
        return await SettingsStore(session).get(PLEX_MACHINE_ID_SETTING)


async def test_put_changed_plex_url_clears_cached_machine_id(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Repointing plex_url drops the cached machine id so the next sign-in
    re-derives it from /identity instead of admitting the OLD server's users."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")

    put = await client.put(
        "/api/v1/settings", json={"plex_url": "http://new:32400"}, headers={"X-Api-Key": _API_KEY}
    )
    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) is None


async def test_put_changed_plex_token_clears_cached_machine_id(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A real (non-masked) plex_token change also invalidates the cached identity."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")

    put = await client.put(
        "/api/v1/settings", json={"plex_token": "new-token"}, headers={"X-Api-Key": _API_KEY}
    )
    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) is None


async def test_put_masked_and_unchanged_plex_values_keep_cached_machine_id(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A round-tripped masked secret ('***') and a same-value plex_url are NOT
    changes: neither may needlessly drop a still-valid cached machine id (which
    would force a pointless /identity re-probe on the next sign-in)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")

    # The FE round-trips the whole object: unchanged plex_url + masked plex_token.
    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://old:32400", "plex_token": "***"},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    assert await _stored_machine_id(sessionmaker_) == "OLD-MID"  # kept, not dropped


async def _active_session_count(sessionmaker_: SessionMaker) -> int:
    """Count auth sessions that are still usable (``revoked_at`` unset)."""
    async with sessionmaker_() as session:
        result = await session.execute(
            select(func.count()).select_from(AuthSession).where(AuthSession.revoked_at.is_(None))
        )
        return result.scalar_one()


async def test_put_plex_repoint_revokes_every_active_session_including_the_callers(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Repointing Plex is an auth-domain change (ADR-0016): clearing the cached
    machine id only fixes FUTURE sign-ins, so every already-minted session — whose
    persisted ``User.permissions`` still encodes the OLD server's authority — must
    be revoked in the same transaction. That includes the admin performing the
    repoint (deliberate, honest self-lockout): their PUT still completes cleanly
    (auth ran at dependency time), and their very NEXT request re-authenticates."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    admin_cookies, admin_csrf = await _admin_session_cookies(app, plex_id=9201, tag="repoint-adm")
    other_cookies, _ = await _admin_session_cookies(app, plex_id=9202, tag="repoint-other")
    assert await _active_session_count(sessionmaker_) == 2

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://new:32400"},
        cookies=admin_cookies,
        headers=admin_csrf,
    )

    # The write itself completes for the now-revoked caller — never a mid-request 401.
    assert put.status_code == 200
    assert await _active_session_count(sessionmaker_) == 0  # everyone, caller included

    # Both old-server sessions must re-sign-in against the NEW server.
    assert (await client.get("/api/v1/settings", cookies=admin_cookies)).status_code == 401
    assert (await client.get("/api/v1/settings", cookies=other_cookies)).status_code == 401
    # The X-Api-Key recovery path is untouched — the repoint never locks the API out.
    assert (
        await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    ).status_code == 200


async def test_put_non_plex_fields_keep_sessions_active(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A PUT that touches no Plex identity field is NOT a repoint: nobody is
    signed out over a library-root or Prowlarr edit."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    cookies, csrf = await _admin_session_cookies(app, plex_id=9203, tag="non-plex")

    put = await client.put(
        "/api/v1/settings",
        json={"tv_root": "/library/tv", "prowlarr_url": "http://prowlarr.local:9696"},
        cookies=cookies,
        headers=csrf,
    )

    assert put.status_code == 200
    assert await _active_session_count(sessionmaker_) == 1  # still signed in
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 200


async def test_put_masked_and_unchanged_plex_values_keep_sessions_active(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The masked-secret round-trip ('***') and a same-value plex_url are NOT
    repoints (the same non-changes that keep the cached machine id): the FE
    saving an unrelated field must never sign the whole install out."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_identity(sessionmaker_, plex_url="http://old:32400", machine_id="OLD-MID")
    cookies, csrf = await _admin_session_cookies(app, plex_id=9204, tag="masked")

    put = await client.put(
        "/api/v1/settings",
        json={"plex_url": "http://old:32400", "plex_token": "***"},
        cookies=cookies,
        headers=csrf,
    )

    assert put.status_code == 200
    assert await _active_session_count(sessionmaker_) == 1  # still signed in
    assert (await client.get("/api/v1/settings", cookies=cookies)).status_code == 200


async def test_secret_is_stored_encrypted(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    plaintext = "another-secret-key"
    await client.put(
        "/api/v1/settings",
        json={"tmdb_api_key": plaintext, "plex_url": "http://plex.local"},
        headers={"X-Api-Key": _API_KEY},
    )

    # Inspect the raw columns, bypassing the EncryptedStr decryption layer.
    async with sessionmaker_() as session:
        secret_row = (
            await session.execute(
                text(
                    "SELECT value, encrypted_value, is_secret "
                    "FROM settings WHERE key = 'tmdb_api_key'"
                )
            )
        ).one()
        plain_row = (
            await session.execute(
                text("SELECT value, encrypted_value FROM settings WHERE key = 'plex_url'")
            )
        ).one()

    raw_value, raw_encrypted, is_secret = secret_row
    assert bool(is_secret) is True
    assert raw_value is None  # the plaintext column is never used for a secret
    assert raw_encrypted is not None
    assert plaintext not in raw_encrypted  # at-rest value is ciphertext, not plaintext

    # The non-secret url is stored in the plaintext column, unencrypted.
    assert plain_row[0] == "http://plex.local"
    assert plain_row[1] is None


async def test_put_mask_round_trip_does_not_clobber_secret(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    # Establish a real secret.
    await client.put(
        "/api/v1/settings",
        json={"tmdb_api_key": "real-tmdb-secret", "plex_url": "http://plex.local"},
        headers=headers,
    )

    # FE GETs the redacted view (secret shows as the mask), edits only a non-secret
    # field, and PUTs the whole object back verbatim — mask and all.
    got = (await client.get("/api/v1/settings", headers=headers)).json()
    assert got["tmdb_api_key"] == "***"
    got["plex_url"] = "http://plex.local:32400"
    put = await client.put("/api/v1/settings", json=got, headers=headers)
    assert put.status_code == 200
    assert put.json()["plex_url"] == "http://plex.local:32400"

    # The real secret must survive — the mask write was a no-op, not a wipe.
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("tmdb_api_key") == "real-tmdb-secret"


async def test_empty_string_root_reads_back_as_unset(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """PUT only skips a field when it's ``None`` (absent) — an empty-string
    ``movies_root``/``tv_root`` (e.g. a frontend "clear" that submits ``""``
    instead of omitting the field) is written verbatim. The importer's
    ``get_*_root_optional`` deps must still report that as unset (``None``), not
    a falsy-but-truthy-looking path: otherwise it would sail past a downstream
    ``is None`` guard and silently resolve relative paths against the process
    CWD instead of tripping the honest ``ImportBlocked`` it's meant to."""
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"movies_root": "", "tv_root": ""},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("movies_root") == ""
        assert await SettingsStore(session).get("tv_root") == ""
        assert await get_movies_root_optional(session) is None
        assert await get_tv_root_optional(session) is None


# --------------------------------------------------------------------------- #
# Service URL shape validation at write time (issue #44)
# --------------------------------------------------------------------------- #
_BAD_SERVICE_URLS = [
    "http://[::1",  # unterminated IPv6 literal -- urlsplit() itself raises ValueError
    "localhost:9696",  # scheme-less
    "ftp://x",  # wrong scheme
    "http://",  # empty host
    "not a url at all",
    "http://x:bad",  # non-numeric port -> would otherwise raise httpx.InvalidURL
    "http://x:0",  # port 0 parses cleanly but is never connectable
    "http://x:99999",  # out-of-range port
    "http://\nx",  # embedded control char (CR/LF log-forging shape)
    "http://x/\x01",  # control char in path
    "http://plex local",  # whitespace in the authority -- urlsplit still yields a host
    "http://x/base path",  # whitespace anywhere (here in the path) is rejected too
    "http://x?y=1",  # query -- adapters append API paths, so a query is swallowed
    "http://x#frag",  # fragment -- likewise swallows the appended API path
    "http://x?",  # BARE query delimiter -- urlsplit yields an EMPTY query, raw '?' remains
    "http://x#",  # bare fragment delimiter -- likewise
    "http://999.999.999.999",  # IPv4-shaped host with out-of-range octets
    "http://01.02.03.04",  # IPv4-shaped host with leading-zero octets
    "http://[v7.abc]",  # IPvFuture -- urlsplit tolerates it, httpx raises InvalidURL
    "http://[fe80::1%eth0]",  # IPv6 zone id -- rejected by policy for a base URL
    "http://[fe80::1%25eth0]",  # RFC 6874 percent-encoded zone id -- likewise
    "http://\N{PILE OF POO}.local",  # IDNA-unencodable label -- httpx.URL() ctor raises
    "http://xn--zzzzzz",  # bogus punycode A-label -- raises only from httpx .host decode
    "http://xn--ls8h.local",  # pre-encoded emoji label -- same class, punycode form
]


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
@pytest.mark.parametrize("bad_url", _BAD_SERVICE_URLS)
async def test_put_settings_rejects_malformed_service_url_and_does_not_persist(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    field: str,
    bad_url: str,
) -> None:
    # Shape-validated at write time (issue #44): the exact predicate the setup
    # wizard's "Test connection" probes use (``url_validation.url_shape_error``),
    # now ALSO enforced on the authenticated PUT /settings write path so a
    # malformed url is a visible 422 before it is ever persisted, not just a
    # later opaque failure from the downstream service.
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    put = await client.put("/api/v1/settings", json={field: bad_url}, headers=headers)
    assert put.status_code == 422

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get(field) is None


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
async def test_put_settings_accepts_valid_https_service_url(
    client: httpx.AsyncClient, seed: SeedFn, field: str
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    put = await client.put(
        "/api/v1/settings", json={field: "https://example.com:8443"}, headers=headers
    )
    assert put.status_code == 200
    assert put.json()[field] == "https://example.com:8443"


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
@pytest.mark.parametrize(
    "good_url",
    [
        "http://prowlarr.local:9696/prowlarr",  # path-prefix (reverse-proxy) base URL
        "http://prowlarr.local:9696/",  # bare trailing slash
        "http://192.168.1.10:32400",  # valid dotted-quad IPv4 host
        "http://[::1]:32400",  # IPv6 literal host (untouched by the IPv4 check)
        # VALID IPv6, despite looking suspicious: 9999 is a legal hex group. This
        # was Codex PR #53 wave 4's claimed-broken example -- empirically urlsplit,
        # ipaddress AND httpx all accept it, so it must stay accepted.
        "http://[9999::1]:32400",
        # VALID punycode (café.local) -- guards the wave-5 httpx gate's .host
        # touch against over-tightening: only UNdecodable xn-- labels reject.
        "http://xn--caf-dma.local:32400",
    ],
)
async def test_put_settings_accepts_legitimate_base_url_shapes(
    client: httpx.AsyncClient, seed: SeedFn, field: str, good_url: str
) -> None:
    # Tightening the shared predicate (query/fragment, IPv4-shaped hosts) must NOT
    # reject a legitimate base URL: a path prefix (reverse-proxy mount), a bare
    # trailing slash, a valid dotted-quad IPv4, and an IPv6 literal all round-trip
    # through the write path unchanged.
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    put = await client.put("/api/v1/settings", json={field: good_url}, headers=headers)
    assert put.status_code == 200
    assert put.json()[field] == good_url


async def test_put_settings_partial_update_omitting_urls_leaves_them_untouched(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    await client.put(
        "/api/v1/settings",
        json={
            "plex_url": "http://plex.local:32400",
            "prowlarr_url": "http://prowlarr.local:9696",
            "qbittorrent_url": "http://qb.local:8080",
        },
        headers=headers,
    )

    # A later partial update naming only an unrelated field must not fire the
    # validator for the omitted (absent -> ``None``) url fields, and must leave
    # every previously-stored url exactly as it was.
    put = await client.put(
        "/api/v1/settings", json={"qbittorrent_username": "admin"}, headers=headers
    )
    assert put.status_code == 200
    body = put.json()
    assert body["plex_url"] == "http://plex.local:32400"
    assert body["prowlarr_url"] == "http://prowlarr.local:9696"
    assert body["qbittorrent_url"] == "http://qb.local:8080"


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
async def test_put_settings_empty_string_service_url_clears_and_is_not_shape_checked(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker, field: str
) -> None:
    # '' is an explicit clear-to-unset (matching movies_root's convention) --
    # ALLOWED, never shape-checked/rejected. The adapters already treat a falsy
    # stored url as unconfigured (an honest 409 service_not_configured), so this
    # is a valid, intentional write.
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    await client.put(
        "/api/v1/settings", json={field: "http://configured.example:1234"}, headers=headers
    )

    put = await client.put("/api/v1/settings", json={field: ""}, headers=headers)
    assert put.status_code == 200

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get(field) == ""


# --------------------------------------------------------------------------- #
# Anime library routing (ADR-0015): anime_movie_root / anime_tv_root
# --------------------------------------------------------------------------- #
async def test_get_starts_with_anime_roots_unset(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    body = response.json()
    assert body["anime_movie_root"] is None
    assert body["anime_tv_root"] is None


async def test_put_anime_roots_round_trip_independently_of_movies_and_tv_root(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"anime_movie_root": "/library/anime-movies", "anime_tv_root": "/library/anime-tv"},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["anime_movie_root"] == "/library/anime-movies"
    assert body["anime_tv_root"] == "/library/anime-tv"
    # Untouched by the anime-only PUT.
    assert body["movies_root"] is None
    assert body["tv_root"] is None

    got = (await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})).json()
    assert got["anime_movie_root"] == "/library/anime-movies"
    assert got["anime_tv_root"] == "/library/anime-tv"


async def test_put_partial_anime_root_only_leaves_the_other_and_normal_roots_untouched(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await client.put(
        "/api/v1/settings",
        json={"movies_root": "/library/movies", "anime_movie_root": "/library/anime-movies"},
        headers={"X-Api-Key": _API_KEY},
    )
    put = await client.put(
        "/api/v1/settings",
        json={"anime_tv_root": "/library/anime-tv"},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200
    body = put.json()
    assert body["anime_tv_root"] == "/library/anime-tv"
    assert body["anime_movie_root"] == "/library/anime-movies"  # untouched by this partial PUT
    assert body["movies_root"] == "/library/movies"  # untouched by this partial PUT


async def test_empty_string_anime_root_reads_back_as_unset(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Mirrors ``test_empty_string_root_reads_back_as_unset``: an empty-string
    anime root (a frontend clear-on-Plex-reconnect) reads back as unset, never
    a falsy-but-truthy path that would sail past the importer's ``is None``
    guard."""
    await seed(initialized=True, app_api_key=_API_KEY)
    put = await client.put(
        "/api/v1/settings",
        json={"anime_movie_root": "", "anime_tv_root": ""},
        headers={"X-Api-Key": _API_KEY},
    )
    assert put.status_code == 200

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("anime_movie_root") == ""
        assert await SettingsStore(session).get("anime_tv_root") == ""
        assert await get_anime_movie_root_optional(session) is None
        assert await get_anime_tv_root_optional(session) is None


# --------------------------------------------------------------------------- #
# Operability beta (ADR-0012) settings: disk-pressure eviction + log retention
# --------------------------------------------------------------------------- #
async def test_put_round_trips_operability_settings(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}
    update = {
        "disk_pressure_threshold_percent": 88.5,
        "disk_pressure_target_percent": 75,
        "eviction_grace_days": 14,
        "eviction_enabled": False,
        "eviction_proactive_enabled": True,
        "eviction_interval_minutes": 45,
        "log_retention_days": 3,
    }
    put = await client.put("/api/v1/settings", json=update, headers=headers)
    assert put.status_code == 200
    body = put.json()
    assert body["disk_pressure_threshold_percent"] == 88.5
    assert body["disk_pressure_target_percent"] == 75.0
    assert body["eviction_grace_days"] == 14
    assert body["eviction_enabled"] is False
    assert body["eviction_proactive_enabled"] is True
    assert body["eviction_interval_minutes"] == 45.0
    assert body["log_retention_days"] == 3

    # GET reflects the identical stored values.
    got = (await client.get("/api/v1/settings", headers=headers)).json()
    assert got == body

    # The typed getters the eviction/log-retention loops actually read must see
    # the SAME values -- not just a wire-level round trip (guards against e.g.
    # a bool serialized in a form the case-insensitive parser wouldn't accept).
    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 88.5
        assert await get_disk_pressure_target_percent(session) == 75.0
        assert await get_eviction_grace_days(session) == 14
        assert await get_eviction_enabled(session) is False
        assert await get_eviction_proactive_enabled(session) is True
        assert await get_eviction_interval_minutes(session) == 45.0
        assert await get_log_retention_days(session) == 3


async def test_put_rejects_out_of_range_operability_settings(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    over_100 = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 150},
        headers=headers,
    )
    assert over_100.status_code == 422

    zero_interval = await client.put(
        "/api/v1/settings",
        json={"eviction_interval_minutes": 0},
        headers=headers,
    )
    assert zero_interval.status_code == 422

    negative_days = await client.put(
        "/api/v1/settings",
        json={"log_retention_days": -1},
        headers=headers,
    )
    assert negative_days.status_code == 422


async def test_put_single_field_threshold_below_stored_target_rejects_and_does_not_persist(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """R4-2: ``SettingsUpdate``'s own ``model_validator`` only catches a target
    above the threshold when BOTH fields are sent in the SAME request -- ``PUT``
    is a PARTIAL update, so a request naming just ONE side against an
    already-stored (now-inverted) other side must ALSO 422, cross-checked
    against what is actually persisted (see
    ``routers.settings._validate_disk_pressure_pair``), or the whole
    threshold-to-target band silently stops relieving pressure."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    # Establish a stored target of 80 (paired with a valid threshold of 95).
    seeded = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 95.0, "disk_pressure_target_percent": 80.0},
        headers=headers,
    )
    assert seeded.status_code == 200

    # A split update naming ONLY the threshold, now BELOW the stored target (80).
    put = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 70.0},
        headers=headers,
    )
    assert put.status_code == 422

    # Never persisted -- both sides stay at their last valid stored values.
    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 95.0
        assert await get_disk_pressure_target_percent(session) == 80.0


async def test_put_single_field_threshold_above_stored_target_still_succeeds(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The stored-value cross-check only rejects an INVERTED effective pair -- a
    valid partial update (threshold alone, still above the stored target) must
    succeed normally, never over-rejected by the new check."""
    await seed(initialized=True, app_api_key=_API_KEY)
    headers = {"X-Api-Key": _API_KEY}

    seeded = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 85.0, "disk_pressure_target_percent": 80.0},
        headers=headers,
    )
    assert seeded.status_code == 200

    put = await client.put(
        "/api/v1/settings",
        json={"disk_pressure_threshold_percent": 90.0},
        headers=headers,
    )
    assert put.status_code == 200
    assert put.json()["disk_pressure_threshold_percent"] == 90.0

    async with sessionmaker_() as session:
        assert await get_disk_pressure_threshold_percent(session) == 90.0
        assert await get_disk_pressure_target_percent(session) == 80.0  # untouched


# --------------------------------------------------------------------------- #
# App-key reveal / rotate (issue #28's OAuth-deferral hardening)
# --------------------------------------------------------------------------- #
async def test_reveal_app_key_returns_the_current_key(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    assert response.json() == {"app_api_key": _API_KEY}


async def test_reveal_app_key_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key")
    assert response.status_code == 401


async def test_rotate_app_key_mints_a_new_key_and_invalidates_the_old_one(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    rotate = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert rotate.status_code == 200
    new_key = rotate.json()["app_api_key"]
    assert new_key != _API_KEY
    assert len(new_key) > 20  # matches setup.complete()'s token_urlsafe(32) shape

    # The OLD key (still in this request's headers) is immediately invalid --
    # rotation replaces the single live key, so every other device holding the
    # old value is locked out at once.
    old_key_check = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert old_key_check.status_code == 401

    # The NEW key works.
    new_key_check = await client.get("/api/v1/settings", headers={"X-Api-Key": new_key})
    assert new_key_check.status_code == 200


async def test_rotate_app_key_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.post("/api/v1/settings/app-key/rotate")
    assert response.status_code == 401


async def test_rotate_app_key_cas_rejects_racing_rotation_with_stale_key(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two rotations racing with the SAME old key must not clobber each other.

    Both requests clear ``require_api_key`` against the old stored key before
    either commits; the compare-and-swap must turn the loser into an honest 409
    instead of silently overwriting the winner's freshly minted key (which would
    leave the winner's client displaying an already-dead key).

    The race is simulated deterministically: while THIS request is in flight (it
    has already authenticated against the old key), a concurrent rotation commits
    a new key in a separate session. The handler's in-transaction re-read must
    observe that change and bail out 409, leaving the concurrent winner's key
    intact.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    winner_key = "winner-rotation-committed-mid-flight-0123456789"
    state = {"raced": False}

    async def racing_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        if not state["raced"]:
            # Fire exactly once: a competing rotation commits its own new key on a
            # separate session AFTER this request authenticated against the old key
            # but BEFORE it writes its own.
            state["raced"] = True
            async with sessionmaker_() as other:
                other_row = await real_ensure(other)
                other_row.app_api_key = winner_key
                await other.commit()
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", racing_ensure)

    losing = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert losing.status_code == 409
    assert losing.json()["detail"] == "app_key_changed"

    # The concurrent winner's key survived -- the loser did not overwrite it.
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == winner_key


async def test_rotate_app_key_lock_serializes_two_concurrent_rotations(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two genuinely concurrent rotations with the SAME old key: exactly one wins.

    This exercises the window the previous CAS test could not: BOTH requests are
    forced into the handler and past authentication (against the old key) BEFORE
    EITHER commits, so a bare check-then-act would let both re-read the old key,
    both pass the compare, and both 200 -- the second silently clobbering the
    first's freshly minted key. The rendezvous is a barrier planted in
    ``ensure_system_settings`` (which runs BEFORE ``_rotate_lock`` is acquired):
    neither request can proceed to the locked read-modify-write until both have
    entered the handler, guaranteeing the both-in-flight-before-any-commit
    interleaving. ``_rotate_lock`` must then serialize them into one 200 + one 409;
    without the lock this assertion fails with two 200s.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    # Barrier(2): the first request to reach it blocks until the second arrives,
    # so BOTH are inside the handler (authenticated, nothing committed yet) before
    # either advances to acquire _rotate_lock.
    both_in_handler = asyncio.Barrier(2)

    async def rendezvous_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        # Timeout so a regression that never lets both sides in (or a broken lock)
        # fails loudly instead of hanging the suite.
        await asyncio.wait_for(both_in_handler.wait(), timeout=5.0)
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", rendezvous_ensure)

    first, second = await asyncio.gather(
        client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY}),
        client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY}),
    )

    # Exactly one 200 (the winner) and one 409 (the loser) -- never two 200s.
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner, loser = (first, second) if first.status_code == 200 else (second, first)
    assert loser.json()["detail"] == "app_key_changed"

    # The stored key is the winner's minted key, and the OLD key is dead -- the
    # loser did not clobber the winner with a second, unreturned key.
    new_key = winner.json()["app_api_key"]
    assert new_key != _API_KEY
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == new_key


async def test_rotate_app_key_cas_returns_409_when_stored_key_already_advanced(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A rotation that authenticated against a now-superseded key gets 409, not 200.

    A first rotation commits and advances the stored key. A second request that
    had already cleared auth against the OLD key (simulated by stubbing the auth
    dependency, exactly what a same-old-key racer would have done before the first
    committed) reaches the handler with that stale key; the CAS must reject it 409
    and must not clobber the first rotation's result.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    first = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert first.status_code == 200
    new_key = first.json()["app_api_key"]

    # The racing second request already passed require_api_key against the old key.
    # ``require_api_key`` now returns an ``AuthContext`` (was ``None``); the stale
    # racer authenticated via the static key, so mirror that method here so the
    # rotate handler takes its api-key CAS path and the guard sees an admin.
    app.dependency_overrides[require_api_key] = lambda: AuthContext(
        method=AuthMethod.api_key, is_admin=True
    )
    try:
        stale = await client.post(
            "/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY}
        )
    finally:
        del app.dependency_overrides[require_api_key]

    assert stale.status_code == 409
    assert stale.json()["detail"] == "app_key_changed"

    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == new_key


async def test_rotate_app_key_cas_rejects_rotate_after_concurrent_revoke(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The revoke null-hole: a rotate that OBSERVED a key must not resurrect it.

    A rotation authenticates against the live key, then a concurrent REVOKE clears
    the stored key to NULL before this request writes. The old CAS skipped its
    check whenever the stored key was null ('nothing to compare, just mint') and so
    minted a fresh key — silently undoing the revoke. The fixed CAS treats a null
    stored key as the genuine first-key generate ONLY when this request also
    observed null; here it observed a non-null key, so it must 409 and leave the
    revoke standing (no key resurrected).
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    state = {"raced": False}

    async def revoking_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        if not state["raced"]:
            # Fire once: a competing REVOKE clears the key on a separate session
            # AFTER this rotation authenticated against it but BEFORE it writes.
            state["raced"] = True
            async with sessionmaker_() as other:
                other_row = await real_ensure(other)
                other_row.app_api_key = None
                await other.commit()
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", revoking_ensure)

    losing = await client.post("/api/v1/settings/app-key/rotate", headers={"X-Api-Key": _API_KEY})
    assert losing.status_code == 409
    assert losing.json()["detail"] == "app_key_changed"

    # The revoke held: no key was resurrected by the losing rotation.
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key is None


async def _admin_session_cookies(
    app: FastAPI, *, plex_id: int, tag: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Mint a live ADMIN (owner) browser session; returns (cookies, csrf headers)."""
    token = f"admin-session-{tag}"
    csrf = f"csrf-{tag}"
    async with app.state.sessionmaker() as session:
        user = User(plex_id=plex_id, username=f"owner-{tag}", permissions=1)
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return {"plexmgr.session": token, "plexmgr.csrf": csrf}, {"X-CSRF-Token": csrf}


async def test_rotate_app_key_cas_serializes_two_concurrent_session_rotations(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two Plex-SESSION admins rotating concurrently: exactly one wins, one 409s.

    Session auth never presents an ``X-Api-Key`` header, so the CAS compares the
    stored key against the value each request's session LOADED at auth time. Both
    requests are forced into the handler (each having observed the OLD key)
    before either commits — the same rendezvous as the api-key barrier test
    above. Were the CAS still gated to ``AuthMethod.api_key`` (the wave-2
    finding), both would 200 and the second would silently clobber the first's
    freshly minted key — the exact dead-key race the CAS exists to prevent.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies_a, headers_a = await _admin_session_cookies(app, plex_id=9101, tag="a")
    cookies_b, headers_b = await _admin_session_cookies(app, plex_id=9102, tag="b")

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    both_in_handler = asyncio.Barrier(2)

    async def rendezvous_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        await asyncio.wait_for(both_in_handler.wait(), timeout=5.0)
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", rendezvous_ensure)
    # A CONTENDED asyncio.Lock binds to the event loop of the test that first
    # contended it (the api-key barrier test above); this test runs in its own
    # loop, so give it a fresh, loop-local lock — same serialization semantics.
    monkeypatch.setattr(settings_router, "_rotate_lock", asyncio.Lock())

    first, second = await asyncio.gather(
        client.post("/api/v1/settings/app-key/rotate", cookies=cookies_a, headers=headers_a),
        client.post("/api/v1/settings/app-key/rotate", cookies=cookies_b, headers=headers_b),
    )

    # Exactly one 200 (the winner) and one honest 409 (the loser) — never two 200s.
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner, loser = (first, second) if first.status_code == 200 else (second, first)
    assert loser.json()["detail"] == "app_key_changed"

    # The stored key is the winner's minted key — the loser did not clobber it
    # with a second, unreturned key (which would strand the winner's client on a
    # dead key).
    new_key = winner.json()["app_api_key"]
    assert new_key != _API_KEY
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == new_key


# --------------------------------------------------------------------------- #
# Opt-in recovery key — status / generate-from-null / revoke (keyless setup)
# --------------------------------------------------------------------------- #
async def test_app_key_status_false_on_fresh_keyless_init(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """A fresh install mints no key (setup is keyless), so status reports absence.

    ``GET /app-key/status`` answers the Settings→Access UI's Generate-vs-Rotate
    question WITHOUT the break-glass reveal. With no key stored, the only way in
    is a Plex-session admin, so authenticate that way.
    """
    await seed(initialized=True)
    cookies, _ = await _admin_session_cookies(app, plex_id=7001, tag="status-empty")

    response = await client.get("/api/v1/settings/app-key/status", cookies=cookies)
    assert response.status_code == 200
    assert response.json() == {"exists": False}


async def test_app_key_status_true_when_a_key_exists_without_revealing_it(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key/status", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
    assert response.json() == {"exists": True}
    # The status probe never discloses the plaintext key — only its existence.
    assert _API_KEY not in response.text


async def test_app_key_status_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings/app-key/status")
    assert response.status_code == 401


async def test_reveal_app_key_404s_when_no_key_exists(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Reveal on a keyless install is an honest 404 envelope, not a bare 409.

    The structured ``app_key_not_set`` envelope carries an operator-facing hint so
    the UI can nudge toward Generate rather than surface an opaque failure.
    """
    await seed(initialized=True)
    cookies, _ = await _admin_session_cookies(app, plex_id=7002, tag="reveal-absent")

    response = await client.get("/api/v1/settings/app-key", cookies=cookies)
    assert response.status_code == 404
    body = response.json()
    assert body["detail"] == "app_key_not_set"
    assert body["hint"]  # a non-empty nudge toward generating one


async def test_generate_app_key_from_null_mints_and_flips_status_true(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Rotate IS the generate path when no key exists: it mints, returns once, and
    flips status to present; the freshly minted key then authenticates + reveals."""
    await seed(initialized=True)
    cookies, csrf = await _admin_session_cookies(app, plex_id=7003, tag="generate")

    generate = await client.post("/api/v1/settings/app-key/rotate", cookies=cookies, headers=csrf)
    assert generate.status_code == 200
    new_key = generate.json()["app_api_key"]
    assert len(new_key) > 20  # matches setup's historical token_urlsafe(32) shape

    # Status now reports a key exists, without disclosing it.
    status_after = await client.get(
        "/api/v1/settings/app-key/status", headers={"X-Api-Key": new_key}
    )
    assert status_after.status_code == 200
    assert status_after.json() == {"exists": True}

    # The freshly minted key authenticates and reveals its own plaintext.
    reveal = await client.get("/api/v1/settings/app-key", headers={"X-Api-Key": new_key})
    assert reveal.status_code == 200
    assert reveal.json() == {"app_api_key": new_key}


async def test_revoke_app_key_returns_204_and_old_key_401s(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Revoke clears the stored key: 204 no-content, the old key 401s everywhere,
    and status flips back to absent (checked via a Plex-session admin, since the
    revoked key can no longer authenticate)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    key_headers = {"X-Api-Key": _API_KEY}

    revoke = await client.delete("/api/v1/settings/app-key", headers=key_headers)
    assert revoke.status_code == 204
    assert revoke.content == b""  # 204 carries no body

    # The revoked key no longer authenticates anywhere.
    dead = await client.get("/api/v1/settings", headers=key_headers)
    assert dead.status_code == 401

    # A Plex-session admin still gets in and sees the key is gone.
    cookies, _ = await _admin_session_cookies(app, plex_id=7004, tag="revoked")
    status_after = await client.get("/api/v1/settings/app-key/status", cookies=cookies)
    assert status_after.status_code == 200
    assert status_after.json() == {"exists": False}


async def test_revoke_app_key_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.delete("/api/v1/settings/app-key")
    assert response.status_code == 401


async def test_revoke_app_key_is_idempotent_when_no_key_exists(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Revoking a keyless install is a no-op 204, not an error — the end state
    (no key) is the same whether or not one was present."""
    await seed(initialized=True)
    cookies, csrf = await _admin_session_cookies(app, plex_id=7005, tag="revoke-noop")

    revoke = await client.delete("/api/v1/settings/app-key", cookies=cookies, headers=csrf)
    assert revoke.status_code == 204
    assert revoke.content == b""


async def test_revoke_app_key_cas_rejects_stale_revoke_after_concurrent_rotation(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale revoke must not wipe a key rotated in between (lost update).

    The revoke authenticates against the live key; a concurrent ROTATE then commits
    a fresh key before this request writes ``None``. The earlier draft loaded
    ``system`` and unconditionally cleared it, silently clobbering the rotation. The
    revoke CAS (mirroring the rotate CAS) must re-read under the lock, see the key
    is no longer the value it observed, and 409 — leaving the rotated key intact.
    """
    await seed(initialized=True, app_api_key=_API_KEY)

    from plex_manager.web.routers import settings as settings_router

    real_ensure = settings_router.ensure_system_settings
    winner_key = "rotated-mid-revoke-0123456789abcdef"
    state = {"raced": False}

    async def racing_ensure(session: AsyncSession) -> object:
        row = await real_ensure(session)
        if not state["raced"]:
            # Fire once: a competing ROTATE commits a new key on a separate session
            # AFTER this revoke authenticated against the old key but BEFORE it writes.
            state["raced"] = True
            async with sessionmaker_() as other:
                other_row = await real_ensure(other)
                other_row.app_api_key = winner_key
                await other.commit()
        return row

    monkeypatch.setattr(settings_router, "ensure_system_settings", racing_ensure)

    losing = await client.delete("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})
    assert losing.status_code == 409
    assert losing.json()["detail"] == "app_key_changed"

    # The concurrently-rotated key survived — the stale revoke did not wipe it.
    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key == winner_key


async def test_revoke_app_key_leaves_key_none_on_success(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A normal (non-raced) revoke still commits ``None`` and returns 204 under the
    new CAS: the observed key matches the stored key, so the clear proceeds."""
    await seed(initialized=True, app_api_key=_API_KEY)

    revoke = await client.delete("/api/v1/settings/app-key", headers={"X-Api-Key": _API_KEY})
    assert revoke.status_code == 204
    assert revoke.content == b""

    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key is None


async def test_revoke_app_key_via_session_auth_clears_present_key(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A Plex-SESSION admin revoking a PRESENT key exercises the CAS's other
    ``observed`` source: session auth carries no ``X-Api-Key`` header, so the CAS
    compares against the value this request's session loaded at auth time. An
    unraced revoke must therefore still clear the key (204), not spuriously 409.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies, csrf = await _admin_session_cookies(app, plex_id=7006, tag="revoke-present")

    revoke = await client.delete("/api/v1/settings/app-key", cookies=cookies, headers=csrf)
    assert revoke.status_code == 204
    assert revoke.content == b""

    async with sessionmaker_() as session:
        system = await load_system_settings(session)
        assert system is not None
        assert system.app_api_key is None
