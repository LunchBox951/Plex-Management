"""Discovery — TMDB search surfaced through the service + auth enforcement."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI

from plex_manager.ports.metadata import MediaSearchResult
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "discover-key"
_HEADERS = {"X-Api-Key": _API_KEY}


async def test_search_returns_results(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    tmdb = FakeTmdb(
        results=[
            MediaSearchResult(tmdb_id=603, media_type="movie", title="The Matrix", year=1999),
        ]
    )
    override_adapters(app, tmdb=tmdb)

    response = await client.get(
        "/api/v1/discover/search", params={"query": "matrix"}, headers=_HEADERS
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results == [
        {
            "tmdb_id": 603,
            "media_type": "movie",
            "title": "The Matrix",
            "year": 1999,
            "overview": None,
            "poster_url": None,
            "backdrop_url": None,
        }
    ]


async def test_discovery_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/discover/search", params={"query": "matrix"})
    assert response.status_code == 401
