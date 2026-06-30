"""SetupGuardMiddleware — 409 for a protected route pre-init, pass-through after."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "guard-key"


async def test_protected_route_blocked_pre_init(client: httpx.AsyncClient) -> None:
    # No system_settings row at all -> treated as uninitialized.
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 409
    assert response.json() == {"detail": "setup_required", "setup_path": "/setup"}


async def test_health_is_always_reachable_pre_init(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_setup_status_reachable_pre_init(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/setup/status")
    assert response.status_code == 200
    assert response.json()["initialized"] is False


async def test_protected_route_passes_post_init(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/settings", headers={"X-Api-Key": _API_KEY})
    assert response.status_code == 200
