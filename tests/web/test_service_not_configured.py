"""ServiceNotConfiguredError -> honest 409 through HTTP (correction-not-terminal).

These tests deliberately DO NOT call ``override_adapters``: the REAL adapter
factories run against an initialized system with no service credentials, so each
raises :class:`ServiceNotConfiguredError`, which the app-level handler must render
as an actionable ``{"detail": "service_not_configured", "service": <name>}`` 409
(a button back to setup) rather than an opaque crash.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "snc-key"
_HEADERS = {"X-Api-Key": _API_KEY}


async def test_discover_search_unconfigured_tmdb_returns_409(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get(
        "/api/v1/discover/search", params={"query": "dune"}, headers=_HEADERS
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "service_not_configured", "service": "tmdb"}


async def test_search_preview_unconfigured_prowlarr_returns_409(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.post(
        "/api/v1/search-preview",
        json={"tmdb_id": 603, "media_type": "movie", "title": "Some Movie", "year": 2020},
        headers=_HEADERS,
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "service_not_configured", "service": "prowlarr"}


async def test_queue_unconfigured_qbittorrent_returns_409(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # GET /queue is now a passive DB read (no qBittorrent dependency). The
    # import-retry endpoint still resolves get_qbittorrent (its qbt parameter is
    # declared before library / movies_root, so it is the first SNC-raising adapter),
    # so an unconfigured client is an honest 409 service_not_configured there.
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.post("/api/v1/queue/1/import", headers=_HEADERS)
    assert response.status_code == 409
    assert response.json() == {"detail": "service_not_configured", "service": "qbittorrent"}
