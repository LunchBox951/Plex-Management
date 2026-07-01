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

from plex_manager.adapters.plex.library import PlexLibraryError
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


class _UnreachablePlexLibrary(FakeLibrary):
    """A configured-but-unreachable Plex: every ``watch_state`` call raises,
    mirroring a real outage or a bad token (``get_library_optional`` only checks
    that a url/token are SET, not that they actually work) -- ``PlexLibraryError``
    is exactly what the real adapter raises in both cases."""

    async def watch_state(
        self, tmdb_id: int, media_type: str, *, season: int | None = None
    ) -> WatchState:
        raise PlexLibraryError("plex request failed")


async def test_disk_gauges_survive_a_plex_outage_during_the_candidate_preview(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    """C4 regression: a configured-but-unreachable Plex must not take down the
    WHOLE ``/ops/disk`` response. Before the fix, ``preview_candidates`` let a
    ``PlexLibraryError`` from ``watch_state`` propagate uncaught, 500ing the
    endpoint and losing the disk-usage gauges exactly during a Plex outage --
    the one time an operator most needs to see how full the disk is."""
    await seed(initialized=True, app_api_key=_API_KEY)
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    await _set_movies_root(sessionmaker_, str(tmp_path))
    await _seed_watched_movie(sessionmaker_, library_path=str(movie_file))

    override_adapters(app, library=_UnreachablePlexLibrary())

    response = await client.get("/api/v1/ops/disk", headers=_HEADERS)

    assert response.status_code == 200
    root = response.json()["roots"][0]
    # The disk gauge is honestly reported even though Plex is unreachable...
    assert root["error"] is None
    assert root["total_bytes"] > 0
    # ...but the candidate preview honestly degrades to empty rather than
    # taking the whole endpoint down with it.
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


# --------------------------------------------------------------------------- #
# OP3: the disk/candidate preview is TTL-cached per root so a Status-page-style
# poll never maps 1:1 onto a fresh Plex watch_state() call + os.walk per title.
# --------------------------------------------------------------------------- #


async def test_disk_preview_is_cached_and_a_second_poll_never_re_hits_plex(
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

    first = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    assert first.status_code == 200
    calls_after_first = len(library.watch_state_calls)
    assert calls_after_first == 1

    second = await client.get("/api/v1/ops/disk", headers=_HEADERS)
    assert second.status_code == 200
    # A second poll within the ~15s TTL must be served entirely from cache --
    # NO additional Plex watch_state() round trip, and the SAME body.
    assert len(library.watch_state_calls) == calls_after_first
    assert second.json() == first.json()


async def test_disk_preview_cache_is_scoped_per_root(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
) -> None:
    # Regression guard for a cache keyed on something coarser than the root
    # path (e.g. a single shared key): movies_root and tv_root must each be
    # cached and served independently.
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
    labels = {root["root"]: root["path"] for root in response.json()["roots"]}
    assert labels == {"movies_root": str(movies_dir), "tv_root": str(tv_dir)}
