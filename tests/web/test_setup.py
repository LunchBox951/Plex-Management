"""Setup completion — flips ``initialized``, issues an app key, stores creds."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.config import get_settings
from plex_manager.models import SystemSettings

SessionMaker = async_sessionmaker[AsyncSession]
_SETUP_TOKEN = "boot-token"  # noqa: S105 - fixed test bootstrap token
_SETUP_HEADERS = {"X-Setup-Token": _SETUP_TOKEN}

_COMPLETE_BODY = {
    "plex_url": "http://plex.local:32400",
    "plex_token": "plex-token-xyz",
    "prowlarr_url": "http://prowlarr.local:9696",
    "prowlarr_api_key": "prowlarr-key-xyz",
    "qbittorrent_url": "http://qb.local:8080",
    "qbittorrent_username": "admin",
    "qbittorrent_password": "qb-pass-xyz",
    "tmdb_api_key": "tmdb-key-xyz",
    "movies_root": "/library/movies",
}


@pytest.fixture(autouse=True)
def configured_setup_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", _SETUP_TOKEN)
    get_settings.cache_clear()


async def test_status_pre_init_has_no_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/setup/status")
    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is False
    assert body["app_api_key"] is None
    assert body["setup_token_required"] is True


async def test_status_reports_setup_token_requirement(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", _SETUP_TOKEN)
    get_settings.cache_clear()

    response = await client.get("/api/v1/setup/status")
    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is False
    assert body["app_api_key"] is None
    assert body["setup_token_required"] is True


async def test_status_reports_setup_token_requirement_for_remote_pre_init(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 45231))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as remote:
        response = await remote.get("/api/v1/setup/status")

    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is False
    assert body["setup_token_required"] is True


async def test_complete_requires_configured_setup_token_pre_init(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", _SETUP_TOKEN)
    get_settings.cache_clear()

    missing = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
    assert missing.status_code == 401
    assert missing.json()["detail"] == "invalid_setup_token"

    wrong = await client.post(
        "/api/v1/setup/complete",
        json=_COMPLETE_BODY,
        headers={"X-Setup-Token": "wrong"},
    )
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "invalid_setup_token"

    ok = await client.post(
        "/api/v1/setup/complete",
        json=_COMPLETE_BODY,
        headers=_SETUP_HEADERS,
    )
    assert ok.status_code == 200
    assert ok.json()["initialized"] is True


async def test_complete_rejects_remote_client_without_setup_token(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 45231))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as remote:
        response = await remote.post("/api/v1/setup/complete", json=_COMPLETE_BODY)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_setup_token"


async def test_complete_rejects_loopback_client_with_nonlocal_host(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45231))
    async with httpx.AsyncClient(transport=transport, base_url="http://attacker.test") as remote:
        response = await remote.post("/api/v1/setup/complete", json=_COMPLETE_BODY)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_setup_token"


async def test_complete_rejects_loopback_client_with_cross_origin(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 45231))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as remote:
        response = await remote.post(
            "/api/v1/setup/complete",
            json=_COMPLETE_BODY,
            headers={"Origin": "http://attacker.test"},
        )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_setup_token"


async def test_complete_flips_initialized_and_issues_key(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/setup/complete", json=_COMPLETE_BODY, headers=_SETUP_HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is True
    issued_key = body["app_api_key"]
    assert isinstance(issued_key, str) and issued_key

    # status reports the install is initialized but NEVER re-serves the key —
    # the /complete response above is the single one-time reveal. Re-serving it
    # from this unauthenticated GET would be a total auth bypass.
    status = (await client.get("/api/v1/setup/status")).json()
    assert status["initialized"] is True
    assert status["app_api_key"] is None

    # Stored creds are reachable through the (now authenticated) settings API,
    # with the secret redacted and the plaintext url preserved.
    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.status_code == 200
    data = settings.json()
    assert data["plex_url"] == _COMPLETE_BODY["plex_url"]
    assert data["tmdb_api_key"] == "***"
    # No plaintext secret leaks anywhere in the redacted response.
    assert _COMPLETE_BODY["tmdb_api_key"] not in settings.text
    assert _COMPLETE_BODY["plex_token"] not in settings.text


async def test_complete_stores_the_key_encrypted_not_plaintext(
    client: httpx.AsyncClient, sessionmaker_: SessionMaker
) -> None:
    # The bearer token is revealed once in the response, but it is stored
    # Fernet-encrypted at rest — a DB-backup leak must not yield a usable key.
    response = await client.post(
        "/api/v1/setup/complete", json=_COMPLETE_BODY, headers=_SETUP_HEADERS
    )
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]
    assert isinstance(issued_key, str) and issued_key

    async with sessionmaker_() as session:
        # The ORM read decrypts EncryptedStr, so the plaintext round-trips...
        row = (await session.execute(select(SystemSettings))).scalars().one()
        assert row.app_api_key == issued_key
        # ...but the RAW column (bypassing the TypeDecorator) is Fernet ciphertext.
        raw = (
            await session.execute(text("SELECT app_api_key FROM system_settings WHERE id = 1"))
        ).scalar_one()

    assert isinstance(raw, str)
    assert raw != issued_key
    assert issued_key not in raw
    assert raw.startswith("gAAAA")  # the Fernet token prefix
    # And the still-revealed key authenticates.
    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.status_code == 200


async def test_complete_is_rejected_after_init(client: httpx.AsyncClient) -> None:
    # Setup is one-shot: the first call initializes; a second is rejected so an
    # anonymous caller can't overwrite stored creds or re-disclose the app key.
    first = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY, headers=_SETUP_HEADERS)
    assert first.status_code == 200
    second = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
    assert second.status_code == 409
    assert second.json()["detail"] == "already_initialized"


async def test_complete_without_tv_root_leaves_it_unset(client: httpx.AsyncClient) -> None:
    # tv_root is optional -- an install may complete setup with only a Movies
    # library. It reads back as None, never an empty string.
    response = await client.post(
        "/api/v1/setup/complete", json=_COMPLETE_BODY, headers=_SETUP_HEADERS
    )
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.json()["tv_root"] is None


async def test_complete_with_tv_root_stores_it(client: httpx.AsyncClient) -> None:
    body = {**_COMPLETE_BODY, "tv_root": "/library/tv"}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.json()["tv_root"] == "/library/tv"


async def test_complete_with_only_tv_root_leaves_movies_root_unset(
    client: httpx.AsyncClient,
) -> None:
    body = {**_COMPLETE_BODY, "movies_root": None, "tv_root": "/library/tv"}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.json()["movies_root"] is None
    assert settings.json()["tv_root"] == "/library/tv"


async def test_complete_rejects_missing_library_roots(client: httpx.AsyncClient) -> None:
    body = {key: value for key, value in _COMPLETE_BODY.items() if key != "movies_root"}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)

    assert response.status_code == 422


async def test_complete_rejects_blank_library_roots(client: httpx.AsyncClient) -> None:
    body = {**_COMPLETE_BODY, "movies_root": "   ", "tv_root": ""}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)

    assert response.status_code == 422


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
async def test_complete_rejects_malformed_service_url(
    client: httpx.AsyncClient, field: str, bad_url: str
) -> None:
    # Same shape predicate the setup wizard's "Test connection" probes use
    # (url_validation.url_shape_error), now ALSO enforced on /setup/complete so a
    # direct-API caller can't post a url the wizard UI would never let through.
    body = {**_COMPLETE_BODY, field: bad_url}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["plex_url", "prowlarr_url", "qbittorrent_url"])
async def test_complete_rejects_empty_string_service_url(
    client: httpx.AsyncClient, field: str
) -> None:
    # Unlike SettingsUpdate's partial-update '' (explicit clear-to-unset,
    # allowed), SetupCompleteRequest's urls are REQUIRED -- there is no "leave
    # unchanged" concept on a one-shot install, so an empty string is REJECTED,
    # closing the direct-API-caller bypass of the wizard's connection probes.
    body = {**_COMPLETE_BODY, field: ""}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "good_url",
    [
        "http://prowlarr.local:9696/prowlarr",  # path-prefix (reverse-proxy) base URL
        "http://prowlarr.local:9696/",  # bare trailing slash
        "http://192.168.1.10:9696",  # valid dotted-quad IPv4 host
        "http://[::1]:9696",  # IPv6 literal host (untouched by the IPv4 check)
        # VALID IPv6, despite looking suspicious: 9999 is a legal hex group. This
        # was Codex PR #53 wave 4's claimed-broken example -- empirically urlsplit,
        # ipaddress AND httpx all accept it, so it must stay accepted.
        "http://[9999::1]:9696",
        # VALID punycode (café.local) -- guards the wave-5 httpx gate's .host
        # touch against over-tightening: only UNdecodable xn-- labels reject.
        "http://xn--caf-dma.local:9696",
    ],
)
async def test_complete_accepts_legitimate_base_url_shapes(
    client: httpx.AsyncClient, good_url: str
) -> None:
    # Tightening the shared predicate (query/fragment, IPv4-shaped hosts) must NOT
    # reject a legitimate base URL: a path prefix (reverse-proxy mount), a bare
    # trailing slash, a valid dotted quad, and an IPv6 literal all complete setup
    # and persist verbatim.
    body = {**_COMPLETE_BODY, "prowlarr_url": good_url}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.json()["prowlarr_url"] == good_url


def test_complete_contract_documents_already_initialized(app: FastAPI) -> None:
    responses = app.openapi()["paths"]["/api/v1/setup/complete"]["post"]["responses"]

    assert responses["409"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorDetail"
    )


def test_complete_contract_documents_library_root_invariant(app: FastAPI) -> None:
    schema = app.openapi()["components"]["schemas"]["SetupCompleteRequest"]

    # The invariant quantifies over EVERY library root, including the ADR-0015
    # anime roots -- an anime-only install is completable, so the contract must
    # document all four alternatives.
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


async def test_complete_with_only_anime_movie_root_succeeds(client: httpx.AsyncClient) -> None:
    # ADR-0015 anime-only install: the wizard's completion gate counts the anime
    # roots, so the runtime validator must too -- pre-fix it considered only
    # movies_root/tv_root and 422'd a body the UI legitimately allows.
    body = {key: value for key, value in _COMPLETE_BODY.items() if key != "movies_root"}
    body["anime_movie_root"] = "/library/anime-movies"
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    got = settings.json()
    assert got["anime_movie_root"] == "/library/anime-movies"
    assert got["movies_root"] is None
    assert got["tv_root"] is None


async def test_complete_rejects_all_roots_blank_including_anime(
    client: httpx.AsyncClient,
) -> None:
    # Blank strings normalize to None for EVERY root -- all four blank is still
    # an honest 422, never an install with no importable destination.
    body = {key: value for key, value in _COMPLETE_BODY.items() if key != "movies_root"}
    body.update({"movies_root": " ", "tv_root": "", "anime_movie_root": "  ", "anime_tv_root": ""})
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)

    assert response.status_code == 422


async def test_complete_without_anime_roots_leaves_them_unset(client: httpx.AsyncClient) -> None:
    # Anime roots (ADR-0015) are optional, mirroring tv_root: an install may
    # complete setup with neither configured. They read back as None, never an
    # empty string.
    response = await client.post(
        "/api/v1/setup/complete", json=_COMPLETE_BODY, headers=_SETUP_HEADERS
    )
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    body = settings.json()
    assert body["anime_movie_root"] is None
    assert body["anime_tv_root"] is None


async def test_complete_with_anime_roots_stores_them(client: httpx.AsyncClient) -> None:
    body = {
        **_COMPLETE_BODY,
        "anime_movie_root": "/library/anime-movies",
        "anime_tv_root": "/library/anime-tv",
    }
    response = await client.post("/api/v1/setup/complete", json=body, headers=_SETUP_HEADERS)
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    got = settings.json()
    assert got["anime_movie_root"] == "/library/anime-movies"
    assert got["anime_tv_root"] == "/library/anime-tv"


async def test_double_complete_yields_exactly_one_key_and_one_set_of_creds(
    client: httpx.AsyncClient,
) -> None:
    # Concurrency contract: completion is claimed with a conditional UPDATE, so
    # only the first /complete wins. The second must be rejected WITHOUT re-minting
    # the key or re-writing creds — the original key must still authenticate, and
    # the stored creds must be intact and singular.
    first = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY, headers=_SETUP_HEADERS)
    assert first.status_code == 200
    issued_key = first.json()["app_api_key"]

    second = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
    assert second.status_code == 409
    assert "app_api_key" not in second.json()  # the loser discloses no key

    # The original key was NOT rotated/overwritten by the rejected second call.
    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.status_code == 200
    assert settings.json()["plex_url"] == _COMPLETE_BODY["plex_url"]
