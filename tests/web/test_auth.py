"""``require_api_key`` — missing/bad key 401, correct key passes, dev bypass."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest

from plex_manager.config import get_settings

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "s3cr3t-app-key"


async def test_missing_key_is_unauthorized(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings")
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_key"


async def test_wrong_key_is_unauthorized(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": "nope"})
    assert response.status_code == 401


async def test_correct_key_is_authorized(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200


async def test_dev_bypass_skips_the_check(
    client: httpx.AsyncClient,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    monkeypatch.setenv("PLEX_MANAGER_DEV_AUTH_BYPASS", "true")
    get_settings.cache_clear()
    response = await client.get("/api/v1/settings")
    assert response.status_code == 200
