"""Settings — GET redacts secrets; PUT round-trips and stores secrets encrypted."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.web.deps import (
    SettingsStore,
    get_movies_root_optional,
    get_tv_root_optional,
)

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "settings-key"


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
