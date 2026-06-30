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


async def test_home_composes_rows_and_picks_a_spotlight(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    trending = [
        MediaSearchResult(
            tmdb_id=1,
            media_type="movie",
            title="Backdrop One",
            year=2024,
            backdrop_url="http://img/a.jpg",
        ),
    ]
    popular = [MediaSearchResult(tmdb_id=2, media_type="movie", title="Popular Two", year=2023)]
    override_adapters(app, tmdb=FakeTmdb(trending=trending, popular=popular, upcoming=[]))

    response = await client.get("/api/v1/discover/home", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    # The first item with a backdrop becomes the spotlight.
    assert body["spotlight"]["tmdb_id"] == 1
    assert [row["row_type"] for row in body["rows"]] == ["trending", "popular", "upcoming"]
    assert body["rows"][0]["items"][0]["backdrop_url"] == "http://img/a.jpg"
    assert body["rows"][2]["items"] == []  # upcoming was empty — an honest empty row


async def test_category_returns_a_paginated_list(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    trending = [MediaSearchResult(tmdb_id=5, media_type="movie", title="Trending", year=2024)]
    override_adapters(app, tmdb=FakeTmdb(trending=trending))

    response = await client.get("/api/v1/discover/trending", params={"page": 1}, headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["results"][0]["tmdb_id"] == 5


async def test_unknown_category_is_422(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=FakeTmdb())
    response = await client.get("/api/v1/discover/nonsense", headers=_HEADERS)
    assert response.status_code == 422
