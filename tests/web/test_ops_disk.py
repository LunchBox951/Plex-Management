"""``GET /api/v1/ops/disk`` (ADR-0012, Component 3) — per-root usage plus a
ranked, read-only eviction-candidate preview.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import MediaRequest, MediaType, RequestStatus
from plex_manager.ports.library import WatchState
from plex_manager.web.deps import SettingsStore
from tests.web.fakes import FakeLibrary, override_adapters

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "ops-disk-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_TMDB_ID = 4242
_STALE = datetime.now(UTC) - timedelta(days=45)


async def _set_movies_root(sm: SessionMaker, root: str) -> None:
    async with sm() as session:
        await SettingsStore(session).set("movies_root", root)
        await session.commit()


async def _seed_watched_movie(sm: SessionMaker, *, library_path: str) -> int:
    async with sm() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="Stale Movie",
            status=RequestStatus.available,
            library_path=library_path,
        )
        session.add(request)
        await session.commit()
        return request.id


async def test_disk_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    assert (await client.get("/api/v1/ops/disk")).status_code == 401


async def test_disk_reports_no_roots_when_unconfigured(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["roots"] == []


async def test_disk_reports_usage_and_ranked_candidate_when_plex_configured(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    await _set_movies_root(sessionmaker_, str(tmp_path))
    await _seed_watched_movie(sessionmaker_, library_path=str(movie_file))

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    override_adapters(app, library=library)

    response = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    body = response.json()
    assert len(body["roots"]) == 1
    root = body["roots"][0]
    assert root["root"] == "movies_root"
    assert root["path"] == str(tmp_path)
    assert root["error"] is None
    assert root["total_bytes"] > 0
    assert len(root["candidates"]) == 1
    candidate = root["candidates"][0]
    assert candidate["title"] == "Stale Movie"
    assert candidate["media_type"] == "movie"
    assert candidate["library_path"] == str(movie_file)
    # The preview never deletes anything -- the file and the request are untouched.
    assert movie_file.exists()


async def test_disk_candidates_empty_when_plex_unconfigured(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    # Plex unconfigured (no override_adapters call): watch state cannot be
    # resolved, so the preview is honestly empty -- never fabricated, never a
    # crash -- while the usage gauge itself still reports.
    await seed(initialized=True, app_api_key=_API_KEY)
    await _set_movies_root(sessionmaker_, str(tmp_path))
    await _seed_watched_movie(sessionmaker_, library_path=str(tmp_path / "missing.mkv"))

    response = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    root = response.json()["roots"][0]
    assert root["error"] is None
    assert root["candidates"] == []


async def test_disk_reports_both_roots_when_both_are_configured(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    movies_dir = tmp_path / "movies"
    tv_dir = tmp_path / "tv"
    movies_dir.mkdir()
    tv_dir.mkdir()
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("movies_root", str(movies_dir))
        await store.set("tv_root", str(tv_dir))
        await session.commit()

    response = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    labels = {root["root"] for root in response.json()["roots"]}
    assert labels == {"movies_root", "tv_root"}


async def test_disk_reports_error_for_an_unreadable_root(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    missing = tmp_path / "does-not-exist"
    await seed(initialized=True, app_api_key=_API_KEY)
    await _set_movies_root(sessionmaker_, str(missing))

    response = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    root = response.json()["roots"][0]
    assert root["error"] is not None
    assert root["total_bytes"] == 0
    assert root["candidates"] == []
