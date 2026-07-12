"""``RequestResponse.seasons[i]``'s "N/M episodes" fallback progress (ADR-0018,
issue #178): populated when ``season_episode_states`` rows exist, ``None`` for a
season the fallback has never touched (movies included)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from plex_manager.models import Download, SeasonRequest
from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "requests-episode-counts-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_SHOW_ID = 950
_MOVIE_ID = 951


def _tmdb() -> FakeTmdb:
    return FakeTmdb(
        movies={
            _MOVIE_ID: MovieMetadata(tmdb_id=_MOVIE_ID, title="A Movie", year=2021),
        },
        shows={
            _SHOW_ID: TvMetadata(tmdb_id=_SHOW_ID, title="Some Show", year=2020, season_count=1),
        },
    )


async def _season_request_id(app: FastAPI, request_id: int, season_number: int) -> int:
    async with app.state.sessionmaker() as session:
        row = (
            await session.execute(
                select(SeasonRequest).where(
                    SeasonRequest.media_request_id == request_id,
                    SeasonRequest.season_number == season_number,
                )
            )
        ).scalar_one()
        return row.id


async def test_partially_imported_season_reports_counts(
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
    request_id = created.json()["id"]
    season_request_id = await _season_request_id(app, request_id, 1)

    async with app.state.sessionmaker() as session:
        download = Download(torrent_hash="episode-counts-test-hash", status="imported")
        session.add(download)
        await session.commit()
        repo = SqlSeasonEpisodeStateRepository(session)
        await repo.upsert_target(season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)})
        await repo.mark_imported(season_request_id, [1], download_id=download.id)
        await session.commit()

    single = await client.get(f"/api/v1/requests/{request_id}", headers=_HEADERS)
    assert single.status_code == 200
    season = single.json()["seasons"][0]
    assert season["imported_episode_count"] == 1
    assert season["target_episode_count"] == 2

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    listed_season = listed.json()["requests"][0]["seasons"][0]
    assert listed_season["imported_episode_count"] == 1
    assert listed_season["target_episode_count"] == 2


async def test_unseeded_season_reports_none(
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
    season = created.json()["seasons"][0]
    assert season["imported_episode_count"] is None
    assert season["target_episode_count"] is None

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    listed_season = listed.json()["requests"][0]["seasons"][0]
    assert listed_season["imported_episode_count"] is None
    assert listed_season["target_episode_count"] is None


async def test_movie_request_has_no_seasons_at_all(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": _MOVIE_ID, "media_type": "movie"},
        headers=_HEADERS,
    )
    assert created.status_code == 201
    assert created.json()["seasons"] is None
