"""Setup completion — keyless: flips ``initialized`` and stores the creds + server.

The wizard's auth model (admin session vs the optional hardening token) lives in
``test_setup_flow.py``; this file pins the request-schema contract the #53 URL
validation + ADR-0015 library-root work established, driven through an admin
(dev-bypass) context so the focus stays on the body validation and storage, not the
credential path.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.config import get_settings
from plex_manager.web.deps import PLEX_MACHINE_ID_SETTING, SettingsStore

SessionMaker = async_sessionmaker[AsyncSession]

_MACHINE_ID = "apollo-machine-id"
_PLEX_URL = "http://plex.local:32400"
_PLEX_TOKEN = "plex-token-xyz"  # noqa: S105 - fixture value, not a credential
_TMDB_KEY = "tmdb-key-xyz"


def _complete_body(movies_root: str) -> dict[str, object]:
    # ``movies_root`` is a required param (not a default) so every call site is an
    # explicit reminder that the write-time visibility gate (issue #132) needs a
    # REAL directory here -- a literal "/library/movies" would 422.
    return {
        "plex_url": _PLEX_URL,
        "plex_machine_identifier": _MACHINE_ID,
        "plex_token": _PLEX_TOKEN,
        "prowlarr_url": "http://prowlarr.local:9696",
        "prowlarr_api_key": "prowlarr-key-xyz",
        "qbittorrent_url": "http://qb.local:8080",
        "qbittorrent_username": "admin",
        "qbittorrent_password": "qb-pass-xyz",
        "tmdb_api_key": _TMDB_KEY,
        "movies_root": movies_root,
    }


@pytest.fixture(autouse=True)
def dev_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Authenticate every request as a dev admin so tests focus on body validation."""
    monkeypatch.setenv("PLEX_MANAGER_DEV_AUTH_BYPASS", "1")
    get_settings.cache_clear()


async def test_complete_flips_initialized_and_is_keyless(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    body = _complete_body(str(tmp_path))
    response = await client.post("/api/v1/setup/complete", json=body)
    assert response.status_code == 200
    resp_body = response.json()
    assert resp_body["initialized"] is True
    # Keyless: Plex sign-in is the credential model — no app key is minted/disclosed.
    assert "app_api_key" not in resp_body

    # status reports the install is initialized (and never carries an app key).
    status = (await client.get("/api/v1/setup/status")).json()
    assert status["initialized"] is True
    assert "app_api_key" not in status

    # Stored creds are reachable through the settings API, secret redacted, plaintext
    # url preserved — and no plaintext secret leaks anywhere in the response.
    settings = await client.get("/api/v1/settings")
    assert settings.status_code == 200
    data = settings.json()
    assert data["plex_url"] == body["plex_url"]
    assert data["tmdb_api_key"] == "***"
    assert _TMDB_KEY not in settings.text
    assert _PLEX_TOKEN not in settings.text


async def test_complete_stores_the_chosen_machine_id(
    client: httpx.AsyncClient, sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    response = await client.post("/api/v1/setup/complete", json=_complete_body(str(tmp_path)))
    assert response.status_code == 200

    async with sessionmaker_() as session:
        assert await SettingsStore(session).get(PLEX_MACHINE_ID_SETTING) == _MACHINE_ID


async def test_complete_is_rejected_after_init(client: httpx.AsyncClient, tmp_path: Path) -> None:
    # One-shot: the first call initializes; a second is rejected so it cannot
    # overwrite the stored creds or re-claim the install.
    body = _complete_body(str(tmp_path))
    first = await client.post("/api/v1/setup/complete", json=body)
    assert first.status_code == 200
    second = await client.post("/api/v1/setup/complete", json=body)
    assert second.status_code == 409
    assert second.json()["detail"] == "already_initialized"


async def test_double_complete_leaves_exactly_one_intact_set_of_creds(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    # The conditional-update claim makes completion one-shot: the loser is rejected
    # WITHOUT re-writing creds, so the stored config stays intact and singular.
    body = _complete_body(str(tmp_path))
    first = await client.post("/api/v1/setup/complete", json=body)
    assert first.status_code == 200
    second = await client.post(
        "/api/v1/setup/complete", json={**body, "plex_url": "http://evil.local:32400"}
    )
    assert second.status_code == 409

    settings = await client.get("/api/v1/settings")
    assert settings.json()["plex_url"] == _PLEX_URL


async def test_complete_without_tv_root_leaves_it_unset(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    # tv_root is optional -- an install may complete with only a Movies library. It
    # reads back as None, never an empty string.
    response = await client.post("/api/v1/setup/complete", json=_complete_body(str(tmp_path)))
    assert response.status_code == 200

    settings = await client.get("/api/v1/settings")
    assert settings.json()["tv_root"] is None


async def test_complete_with_tv_root_stores_it(client: httpx.AsyncClient, tmp_path: Path) -> None:
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    body = {**_complete_body(str(movies_root)), "tv_root": str(tv_root)}
    response = await client.post("/api/v1/setup/complete", json=body)
    assert response.status_code == 200

    settings = await client.get("/api/v1/settings")
    assert settings.json()["tv_root"] == str(tv_root)


async def test_complete_with_only_tv_root_leaves_movies_root_unset(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    body = {**_complete_body(str(tmp_path)), "movies_root": None, "tv_root": str(tv_root)}
    response = await client.post("/api/v1/setup/complete", json=body)
    assert response.status_code == 200

    settings = await client.get("/api/v1/settings")
    assert settings.json()["movies_root"] is None
    assert settings.json()["tv_root"] == str(tv_root)


async def test_complete_rejects_missing_library_roots(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    body = {
        key: value for key, value in _complete_body(str(tmp_path)).items() if key != "movies_root"
    }
    response = await client.post("/api/v1/setup/complete", json=body)

    assert response.status_code == 422


async def test_complete_rejects_blank_library_roots(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    body = {**_complete_body(str(tmp_path)), "movies_root": "   ", "tv_root": ""}
    response = await client.post("/api/v1/setup/complete", json=body)

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Service URL shape validation at write time (issue #44 / #53)
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
async def test_complete_rejects_malformed_service_url(
    client: httpx.AsyncClient, field: str, bad_url: str, tmp_path: Path
) -> None:
    # Same shape predicate the wizard's "Test connection" probes use
    # (url_validation.url_shape_error), enforced on /setup/complete so a direct-API
    # caller can't post a url the wizard UI would never let through.
    body = {**_complete_body(str(tmp_path)), field: bad_url}
    response = await client.post("/api/v1/setup/complete", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
async def test_complete_rejects_empty_string_service_url(
    client: httpx.AsyncClient, field: str, tmp_path: Path
) -> None:
    # SetupCompleteRequest's urls are REQUIRED -- there is no "leave unchanged"
    # concept on a one-shot install, so an empty string is REJECTED.
    body = {**_complete_body(str(tmp_path)), field: ""}
    response = await client.post("/api/v1/setup/complete", json=body)

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["plex_token", "prowlarr_api_key"])
@pytest.mark.parametrize("bad_value", ["key\r\ninjected", "key\x00nul", "kéy-nonascii"])
async def test_complete_rejects_header_unsafe_credential(
    client: httpx.AsyncClient, field: str, bad_value: str, tmp_path: Path
) -> None:
    # A credential that cannot ride its outbound HTTP header (plex_token ->
    # X-Plex-Token, prowlarr_api_key -> X-Api-Key) is rejected at the persistence
    # boundary -- BEFORE it is stored and later leaked via httpx's str(exc) (or
    # crashes the grab loop) when an adapter sends it as a header. Under dev-bypass
    # the Plex verification ladder is skipped, so the SCHEMA validator is what
    # rejects here, proving the guard is the write-time check itself, not a probe.
    body = {**_complete_body(str(tmp_path)), field: bad_value}
    response = await client.post("/api/v1/setup/complete", json=body)

    assert response.status_code == 422
    # A rejected body must not have claimed the install.
    status = (await client.get("/api/v1/setup/status")).json()
    assert status["initialized"] is False


@pytest.mark.parametrize("field", ["plex_token", "prowlarr_api_key"])
async def test_complete_422_never_echoes_the_submitted_credential(
    client: httpx.AsyncClient, field: str, tmp_path: Path
) -> None:
    # north star #3: rejecting a header-unsafe credential (422) must NEVER echo the
    # submitted value back in the error body. FastAPI's DEFAULT RequestValidationError
    # handler returns each error's raw ``input`` -- which for these fields is the very
    # token the guard just refused, undoing the guard. The secret-redacting handler
    # scrubs it. Assert on the RAW response text (not just the parsed ``input``), so a
    # leak in any part of the body (msg/ctx/input) is caught.
    sentinel = "leak-SENTINEL-\r\nZZZINJECT"
    body = {**_complete_body(str(tmp_path)), field: sentinel}

    response = await client.post("/api/v1/setup/complete", json=body)

    assert response.status_code == 422
    assert "SENTINEL" not in response.text
    assert "ZZZINJECT" not in response.text
    # The standard {"detail": [...]} envelope is preserved so the typed client parses it.
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert any(err.get("loc", [])[-1:] == [field] for err in detail)
    # And the leak did not sneak through in a different shape (still uninitialized).
    status = (await client.get("/api/v1/setup/status")).json()
    assert status["initialized"] is False


@pytest.mark.parametrize(
    "good_url",
    [
        "http://prowlarr.local:9696/prowlarr",  # path-prefix (reverse-proxy) base URL
        "http://prowlarr.local:9696/",  # bare trailing slash
        "http://192.168.1.10:9696",  # valid dotted-quad IPv4 host
        "http://[::1]:9696",  # IPv6 literal host (untouched by the IPv4 check)
        "http://[9999::1]:9696",  # valid IPv6 (9999 is a legal hex group)
        "http://xn--caf-dma.local:9696",  # valid punycode (café.local)
    ],
)
async def test_complete_accepts_legitimate_base_url_shapes(
    client: httpx.AsyncClient, good_url: str, tmp_path: Path
) -> None:
    # Tightening the shared predicate must NOT reject a legitimate base URL: a path
    # prefix, a bare trailing slash, a valid dotted quad, and an IPv6 literal all
    # complete setup and persist verbatim.
    body = {**_complete_body(str(tmp_path)), "prowlarr_url": good_url}
    response = await client.post("/api/v1/setup/complete", json=body)
    assert response.status_code == 200

    settings = await client.get("/api/v1/settings")
    assert settings.json()["prowlarr_url"] == good_url


def test_complete_contract_documents_already_initialized(app: FastAPI) -> None:
    responses = app.openapi()["paths"]["/api/v1/setup/complete"]["post"]["responses"]

    assert responses["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorDetail"
    )


def test_complete_contract_documents_library_root_invariant(app: FastAPI) -> None:
    schema = app.openapi()["components"]["schemas"]["SetupCompleteRequest"]

    # The invariant quantifies over EVERY library root, including the ADR-0015 anime
    # roots -- an anime-only install is completable, so the contract documents all
    # four alternatives.
    assert schema["allOf"] == [
        {
            "anyOf": [
                {
                    "required": [field],
                    "properties": {field: {"type": "string", "pattern": "\\S"}},
                }
                for field in ("movies_root", "tv_root", "anime_movie_root", "anime_tv_root")
            ]
        }
    ]


async def test_complete_with_only_anime_movie_root_succeeds(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    # ADR-0015 anime-only install: the wizard's completion gate counts the anime
    # roots, so the runtime validator must too.
    anime_movie_root = tmp_path / "anime-movies"
    anime_movie_root.mkdir()
    body = {
        key: value for key, value in _complete_body(str(tmp_path)).items() if key != "movies_root"
    }
    body["anime_movie_root"] = str(anime_movie_root)
    response = await client.post("/api/v1/setup/complete", json=body)
    assert response.status_code == 200

    settings = await client.get("/api/v1/settings")
    got = settings.json()
    assert got["anime_movie_root"] == str(anime_movie_root)
    assert got["movies_root"] is None
    assert got["tv_root"] is None


async def test_complete_rejects_all_roots_blank_including_anime(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    # Blank strings normalize to None for EVERY root -- all four blank is still an
    # honest 422, never an install with no importable destination.
    body = {
        key: value for key, value in _complete_body(str(tmp_path)).items() if key != "movies_root"
    }
    body.update({"movies_root": " ", "tv_root": "", "anime_movie_root": "  ", "anime_tv_root": ""})
    response = await client.post("/api/v1/setup/complete", json=body)

    assert response.status_code == 422


async def test_complete_without_anime_roots_leaves_them_unset(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    # Anime roots (ADR-0015) are optional, mirroring tv_root: an install may complete
    # with neither configured. They read back as None, never an empty string.
    response = await client.post("/api/v1/setup/complete", json=_complete_body(str(tmp_path)))
    assert response.status_code == 200

    settings = await client.get("/api/v1/settings")
    body = settings.json()
    assert body["anime_movie_root"] is None
    assert body["anime_tv_root"] is None


async def test_complete_with_anime_roots_stores_them(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_movie_root = tmp_path / "anime-movies"
    anime_movie_root.mkdir()
    anime_tv_root = tmp_path / "anime-tv"
    anime_tv_root.mkdir()
    body = {
        **_complete_body(str(movies_root)),
        "anime_movie_root": str(anime_movie_root),
        "anime_tv_root": str(anime_tv_root),
    }
    response = await client.post("/api/v1/setup/complete", json=body)
    assert response.status_code == 200

    settings = await client.get("/api/v1/settings")
    got = settings.json()
    assert got["anime_movie_root"] == str(anime_movie_root)
    assert got["anime_tv_root"] == str(anime_tv_root)
