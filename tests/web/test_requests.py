"""Requests — create resolves TMDB detail and dedups; list + get."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI

from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from tests.web.fakes import FakeLibrary, FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "requests-key"
_HEADERS = {"X-Api-Key": _API_KEY}


def _tmdb() -> FakeTmdb:
    return FakeTmdb(
        movies={
            603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999, is_anime=False),
        }
    )


async def test_create_resolves_detail_and_lists(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "The Matrix"
    assert body["year"] == 1999
    assert body["status"] == "pending"

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    assert len(listed.json()["requests"]) == 1


async def test_create_dedups_active_request(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    first = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    second = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert first.json()["id"] == second.json()["id"]

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert len(listed.json()["requests"]) == 1


async def test_create_records_already_in_plex_as_available(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # A movie already in Plex is recorded directly as `available` (poster art
    # persisted), short-circuiting search/grab — never a wasted request.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb(), library=FakeLibrary(available={603}))

    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    assert created.json()["status"] == "available"


async def test_create_proceeds_when_not_in_plex(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb(), library=FakeLibrary(available=set()))

    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    assert created.json()["status"] == "pending"


async def test_create_unknown_media_is_404(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=FakeTmdb())
    response = await client.post(
        "/api/v1/requests", json={"tmdb_id": 999, "media_type": "movie"}, headers=_HEADERS
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "media_not_found"


async def test_create_tv_request_is_deferred(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=FakeTmdb(shows={44: TvMetadata(tmdb_id=44, title="Show")}))

    response = await client.post(
        "/api/v1/requests", json={"tmdb_id": 44, "media_type": "tv"}, headers=_HEADERS
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "media_type_deferred"

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.json()["requests"] == []


async def test_get_missing_request_is_404(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/requests/12345", headers=_HEADERS)
    assert response.status_code == 404
