"""Requests — create resolves TMDB detail and dedups; list + get."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.ports.repositories import SeasonRequestRecord
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from tests.web.fakes import FakeLibrary, FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "requests-key"
_HEADERS = {"X-Api-Key": _API_KEY}

_SHOW_ID = 900


def _tmdb() -> FakeTmdb:
    return FakeTmdb(
        movies={
            603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999, is_anime=False),
        },
        shows={
            _SHOW_ID: TvMetadata(tmdb_id=_SHOW_ID, title="Some Show", year=2020, season_count=2),
        },
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
    assert first.status_code == 201
    assert second.status_code == 200
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


async def test_movie_request_seasons_is_none(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # Movies have no SeasonRequest rows -- ``seasons`` is always None, never [].
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())
    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.json()["seasons"] is None

    fetched = await client.get(f"/api/v1/requests/{created.json()['id']}", headers=_HEADERS)
    assert fetched.json()["seasons"] is None


async def test_create_tv_request_with_no_seasons_tracks_the_whole_aired_series(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # Omitted `seasons` = whole aired series: every season 1..season_count.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": _SHOW_ID, "media_type": "tv"}, headers=_HEADERS
    )
    assert created.status_code == 201
    body = created.json()
    assert body["media_type"] == "tv"
    assert sorted(s["season_number"] for s in body["seasons"]) == [1, 2]
    assert all(s["status"] == "pending" for s in body["seasons"])


async def test_create_tv_request_with_explicit_seasons_tracks_only_those(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": _SHOW_ID, "media_type": "tv", "seasons": [1]},
        headers=_HEADERS,
    )
    assert created.status_code == 201
    assert [s["season_number"] for s in created.json()["seasons"]] == [1]


async def test_second_post_with_a_new_season_grows_the_tracked_set(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # A repeat POST for the same show naming a NEW season GROWS the tracked set
    # rather than being dropped by the request-level (tmdb_id, media_type) dedup.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    first = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": _SHOW_ID, "media_type": "tv", "seasons": [1]},
        headers=_HEADERS,
    )
    assert first.status_code == 201
    request_id = first.json()["id"]
    assert [s["season_number"] for s in first.json()["seasons"]] == [1]

    second = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": _SHOW_ID, "media_type": "tv", "seasons": [1, 2]},
        headers=_HEADERS,
    )
    assert second.status_code == 201
    assert second.json()["id"] == request_id  # the SAME request row, dedup'd
    assert sorted(s["season_number"] for s in second.json()["seasons"]) == [1, 2]

    # The list endpoint reflects the grown set too.
    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert len(listed.json()["requests"]) == 1
    listed_seasons = listed.json()["requests"][0]["seasons"]
    assert sorted(s["season_number"] for s in listed_seasons) == [1, 2]


async def test_list_requests_batches_season_rows_not_one_query_per_tv_row(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tv shows + a movie on the list endpoint must issue exactly ONE
    ``list_for_requests`` batch call and ZERO per-row ``list_for_request`` calls --
    proving the N+1 the blueprint calls out is actually avoided, not just that the
    result happens to look right."""
    await seed(initialized=True, app_api_key=_API_KEY)
    tmdb = FakeTmdb(
        movies={603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999)},
        shows={
            900: TvMetadata(tmdb_id=900, title="Show One", year=2020, season_count=1),
            901: TvMetadata(tmdb_id=901, title="Show Two", year=2021, season_count=1),
        },
    )
    override_adapters(app, tmdb=tmdb)

    await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    await client.post(
        "/api/v1/requests", json={"tmdb_id": 900, "media_type": "tv"}, headers=_HEADERS
    )
    await client.post(
        "/api/v1/requests", json={"tmdb_id": 901, "media_type": "tv"}, headers=_HEADERS
    )

    batch_calls = {"n": 0}
    per_row_calls = {"n": 0}
    real_batch = SqlSeasonRequestRepository.list_for_requests
    real_per_row = SqlSeasonRequestRepository.list_for_request

    async def counting_batch(
        self: SqlSeasonRequestRepository, media_request_ids: list[int]
    ) -> dict[int, list[SeasonRequestRecord]]:
        batch_calls["n"] += 1
        return await real_batch(self, media_request_ids)

    async def counting_per_row(
        self: SqlSeasonRequestRepository, media_request_id: int
    ) -> list[SeasonRequestRecord]:
        per_row_calls["n"] += 1
        return await real_per_row(self, media_request_id)

    monkeypatch.setattr(SqlSeasonRequestRepository, "list_for_requests", counting_batch)
    monkeypatch.setattr(SqlSeasonRequestRepository, "list_for_request", counting_per_row)

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    assert len(listed.json()["requests"]) == 3

    assert batch_calls["n"] == 1
    assert per_row_calls["n"] == 0
