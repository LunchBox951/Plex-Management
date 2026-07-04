"""Setup completion — flips ``initialized``, issues an app key, stores creds."""

from __future__ import annotations

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import SystemSettings

SessionMaker = async_sessionmaker[AsyncSession]

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


async def test_status_pre_init_has_no_key(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/setup/status")
    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is False
    assert body["app_api_key"] is None


async def test_complete_flips_initialized_and_issues_key(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
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
    response = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
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
    first = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
    assert first.status_code == 200
    second = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
    assert second.status_code == 409
    assert second.json()["detail"] == "already_initialized"


async def test_complete_without_tv_root_leaves_it_unset(client: httpx.AsyncClient) -> None:
    # tv_root is optional -- an install may complete setup with only a Movies
    # library. It reads back as None, never an empty string.
    response = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.json()["tv_root"] is None


async def test_complete_with_tv_root_stores_it(client: httpx.AsyncClient) -> None:
    body = {**_COMPLETE_BODY, "tv_root": "/library/tv"}
    response = await client.post("/api/v1/setup/complete", json=body)
    assert response.status_code == 200
    issued_key = response.json()["app_api_key"]

    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.json()["tv_root"] == "/library/tv"


async def test_complete_without_anime_roots_leaves_them_unset(client: httpx.AsyncClient) -> None:
    # Anime roots (ADR-0015) are optional, mirroring tv_root: an install may
    # complete setup with neither configured. They read back as None, never an
    # empty string.
    response = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
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
    response = await client.post("/api/v1/setup/complete", json=body)
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
    first = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
    assert first.status_code == 200
    issued_key = first.json()["app_api_key"]

    second = await client.post("/api/v1/setup/complete", json=_COMPLETE_BODY)
    assert second.status_code == 409
    assert "app_api_key" not in second.json()  # the loser discloses no key

    # The original key was NOT rotated/overwritten by the rejected second call.
    settings = await client.get("/api/v1/settings", headers={"X-Api-Key": issued_key})
    assert settings.status_code == 200
    assert settings.json()["plex_url"] == _COMPLETE_BODY["plex_url"]
