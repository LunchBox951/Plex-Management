"""``POST /api/v1/requests/{id}/keep-forever`` (ADR-0012) — the operator pin
that protects a title (or, for a show, every one of its seasons) from the
disk-pressure eviction sweep, regardless of watch state or disk pressure.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI

from plex_manager.ports.metadata import MovieMetadata
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "keep-forever-key"
_HEADERS = {"X-Api-Key": _API_KEY}


async def _create_movie_request(app: FastAPI, client: httpx.AsyncClient) -> int:
    override_adapters(
        app,
        tmdb=FakeTmdb(movies={603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999)}),
    )
    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    assert created.json()["keep_forever"] is False  # unset by default
    return created.json()["id"]


async def test_keep_forever_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.post("/api/v1/requests/1/keep-forever", json={"keep_forever": True})
    assert response.status_code == 401


async def test_keep_forever_missing_request_is_404(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.post(
        "/api/v1/requests/99999/keep-forever", json={"keep_forever": True}, headers=_HEADERS
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "request_not_found"


async def test_keep_forever_sets_and_clears_the_pin(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_movie_request(app, client)

    pinned = await client.post(
        f"/api/v1/requests/{request_id}/keep-forever",
        json={"keep_forever": True},
        headers=_HEADERS,
    )
    assert pinned.status_code == 200
    assert pinned.json()["keep_forever"] is True

    # The pin is durable, reflected on a plain re-fetch too.
    fetched = await client.get(f"/api/v1/requests/{request_id}", headers=_HEADERS)
    assert fetched.json()["keep_forever"] is True

    cleared = await client.post(
        f"/api/v1/requests/{request_id}/keep-forever",
        json={"keep_forever": False},
        headers=_HEADERS,
    )
    assert cleared.status_code == 200
    assert cleared.json()["keep_forever"] is False
