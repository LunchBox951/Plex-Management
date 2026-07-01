"""Disk-pressure eviction sweep (ADR-0012): candidate assembly, execution
(delete + status flip + history), pressure gating, and the proactive mode.

Uses the REAL ``LocalFileSystem`` against ``tmp_path`` (so the root-containment
guard is genuinely exercised) and ``FakeLibrary`` for watch state. Most tests
pass ``threshold_pct=0.0`` so the pressure gate always opens regardless of the
test machine's REAL disk usage (``run_eviction_sweep`` reads real
``shutil.disk_usage`` for the configured root) — the target-based early-stop
test is the one exception, which monkeypatches ``read_disk_usage`` for a
controlled, small total so each candidate's ``size_percent`` is meaningful.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.db import Base, enable_sqlite_fk_enforcement
from plex_manager.domain.disk_usage import DiskUsage
from plex_manager.models import (
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.library import WatchState
from plex_manager.services import eviction_service
from tests.web.fakes import FakeLibrary

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime.now(UTC)
_GRACE_DAYS = 30
_STALE = _NOW - timedelta(days=_GRACE_DAYS + 10)
_RECENT = _NOW - timedelta(days=1)


async def _heartbeat_ticks_during[T](
    awaitable: Awaitable[T], *, tick_seconds: float = 0.01
) -> tuple[T, int]:
    """Run ``awaitable`` while counting a concurrent ``asyncio.sleep`` heartbeat's
    completed ticks -- the non-blocking-event-loop regression guard shared by
    every ``asyncio.to_thread`` offload test below.

    If ``awaitable`` truly never blocks the loop (every synchronous FS/disk
    primitive it calls is off-loaded via ``asyncio.to_thread`` onto a worker
    thread), the heartbeat keeps ticking on its own schedule throughout,
    regardless of how long a *blocking* primitive (a real ``time.sleep``, not
    ``asyncio.sleep``) takes inside that thread. If ``awaitable`` instead calls
    that same blocking primitive INLINE (no thread offload), the single event
    loop is frozen for its whole duration and the heartbeat cannot advance at
    all until ``awaitable`` returns -- so a near-zero tick count is the
    regression signature this catches.
    """
    ticks = 0
    stop = False

    async def _heartbeat() -> None:
        nonlocal ticks
        while not stop:
            await asyncio.sleep(tick_seconds)
            ticks += 1

    heartbeat_task = asyncio.create_task(_heartbeat())
    try:
        result = await awaitable
    finally:
        stop = True
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
    return result, ticks


async def _movie(
    sm: SessionMaker,
    *,
    tmdb_id: int,
    title: str,
    library_path: str | None,
    keep_forever: bool = False,
    status: RequestStatus = RequestStatus.available,
) -> int:
    async with sm() as session:
        row = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.movie,
            title=title,
            status=status,
            library_path=library_path,
            keep_forever=keep_forever,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _show_with_seasons(
    sm: SessionMaker,
    *,
    tmdb_id: int,
    title: str,
    seasons: dict[int, str | None],
    keep_forever: bool = False,
) -> int:
    """Insert a tv ``MediaRequest`` plus one ``SeasonRequest`` per ``seasons``
    entry (season_number -> library_path, status always ``available``)."""
    async with sm() as session:
        show = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title=title,
            status=RequestStatus.available,
            keep_forever=keep_forever,
        )
        session.add(show)
        await session.flush()
        for season_number, library_path in seasons.items():
            session.add(
                SeasonRequest(
                    media_request_id=show.id,
                    season_number=season_number,
                    status=RequestStatus.available,
                    library_path=library_path,
                )
            )
        await session.commit()
        return show.id


def _movie_file(tmp_path: Path, name: str, size: int = 1024) -> str:
    path = tmp_path / "movies" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)
    return str(path)


# --------------------------------------------------------------------------- #
# Movie eviction: happy path + every honesty guard
# --------------------------------------------------------------------------- #


async def test_evicts_a_watched_past_grace_movie_and_deletes_the_file(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Old Movie.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=1, title="Old Movie", library_path=library_path
    )
    library = FakeLibrary(
        watch_states={(1, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert [o.title for o in outcomes] == ["Old Movie"]
    assert not Path(library_path).exists()

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted

        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 1)))
            .scalars()
            .all()
        )
    assert len(history) == 1
    assert history[0].event_type is DownloadHistoryEvent.evicted
    assert history[0].torrent_hash is None
    assert history[0].source_title == "Old Movie"


async def test_never_evicts_an_unwatched_movie(sessionmaker_: SessionMaker, tmp_path: Path) -> None:
    library_path = _movie_file(tmp_path, "Unwatched.mkv")
    await _movie(sessionmaker_, tmdb_id=2, title="Unwatched", library_path=library_path)
    library = FakeLibrary()  # no watch_states entry -> watched=False by default
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(library_path).exists()


async def test_never_evicts_within_the_grace_window(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Recently Watched.mkv")
    await _movie(sessionmaker_, tmdb_id=3, title="Recently Watched", library_path=library_path)
    library = FakeLibrary(
        watch_states={(3, "movie", None): WatchState(watched=True, last_viewed_at=_RECENT)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(library_path).exists()


async def test_never_evicts_a_keep_forever_pinned_movie(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Pinned.mkv")
    await _movie(
        sessionmaker_, tmdb_id=4, title="Pinned", library_path=library_path, keep_forever=True
    )
    library = FakeLibrary(
        watch_states={(4, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(library_path).exists()


async def test_never_evicts_a_title_with_an_active_download_in_flight(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Regrabbing.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=5, title="Regrabbing", library_path=library_path
    )
    async with sessionmaker_() as session:
        session.add(
            Download(
                torrent_hash="abc123",
                status="Downloading",
                media_request_id=request_id,
                tmdb_id=5,
            )
        )
        await session.commit()

    library = FakeLibrary(
        watch_states={(5, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(library_path).exists()


async def test_below_threshold_evicts_nothing(sessionmaker_: SessionMaker, tmp_path: Path) -> None:
    library_path = _movie_file(tmp_path, "Old Movie.mkv")
    await _movie(sessionmaker_, tmdb_id=6, title="Old Movie", library_path=library_path)
    library = FakeLibrary(
        watch_states={(6, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=101.0,  # unreachable -- real usage can never hit this
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(library_path).exists()


async def test_below_threshold_never_resolves_watch_state_or_walks_disk(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # The cheap pre-check regression guard: below threshold_pct, run_eviction_sweep
    # must return [] BEFORE assembling any candidate -- no Plex watch_state call,
    # no directory walk -- rather than paying for both up front only to have
    # select_evictions reject everything on this exact gate afterwards.
    library_path = _movie_file(tmp_path, "Old Movie.mkv")
    await _movie(sessionmaker_, tmdb_id=6, title="Old Movie", library_path=library_path)
    library = FakeLibrary(
        watch_states={(6, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=101.0,  # unreachable -- real usage can never hit this
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert library.watch_state_calls == []
    assert Path(library_path).exists()


async def test_missing_library_path_breadcrumb_is_skipped_and_logged(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    request_id = await _movie(sessionmaker_, tmdb_id=7, title="No Breadcrumb", library_path=None)
    library = FakeLibrary(
        watch_states={(7, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.eviction_service"):
        async with sessionmaker_() as session:
            outcomes = await eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(tmp_path),
                threshold_pct=0.0,
                target_pct=0.0,
                grace_days=_GRACE_DAYS,
            )

    assert outcomes == []
    assert "no stored library_path breadcrumb" in caplog.text
    # Never a silent skip -- and never flipped to evicted either, since nothing
    # was actually reclaimed.
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available


async def test_filesystem_guard_refusal_is_skipped_and_logged(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    library_path = _movie_file(tmp_path, "Outside Root.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=8, title="Outside Root", library_path=library_path
    )
    library = FakeLibrary(
        watch_states={(8, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    # A filesystem instance with NO configured roots refuses every delete --
    # exactly the "root changed after import" / misconfiguration case.
    fs = LocalFileSystem(library_roots=[])

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.eviction_service"):
        async with sessionmaker_() as session:
            outcomes = await eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(tmp_path),
                threshold_pct=0.0,
                target_pct=0.0,
                grace_days=_GRACE_DAYS,
            )

    assert outcomes == []
    assert Path(library_path).exists()
    assert "refused by the filesystem guard" in caplog.text
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available


async def test_unreadable_root_skips_the_whole_sweep_without_crashing(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    missing_root = str(tmp_path / "does" / "not" / "exist")

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=missing_root,
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )
    assert outcomes == []


# --------------------------------------------------------------------------- #
# TV: per-season eviction + parent rollup
# --------------------------------------------------------------------------- #


async def test_evicts_one_watched_season_and_rolls_up_partially_available(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    s1_path = _movie_file(tmp_path, "Show S01.mkv")
    s2_path = _movie_file(tmp_path, "Show S02.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=100, title="Some Show", seasons={1: s1_path, 2: s2_path}
    )
    library = FakeLibrary(
        watch_states={
            (100, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE),
            (100, "tv", 2): WatchState(watched=False, last_viewed_at=None),
        }
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert [(o.title, o.season) for o in outcomes] == [("Some Show", 1)]
    assert not Path(s1_path).exists()
    assert Path(s2_path).exists()  # unwatched season 2 is untouched

    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
                )
            )
            .scalars()
            .all()
        )
        by_season = {s.season_number: s.status for s in seasons}
        show = await session.get(MediaRequest, show_id)

    assert by_season == {1: RequestStatus.evicted, 2: RequestStatus.available}
    assert show is not None
    # Season 1 evicted (file gone), season 2 still genuinely available -- never
    # dishonestly rolled up to plain "available".
    assert show.status is RequestStatus.partially_available


async def test_evicts_every_season_and_rolls_the_show_up_to_evicted(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    s1_path = _movie_file(tmp_path, "Whole Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=101, title="Whole Show", seasons={1: s1_path}
    )
    library = FakeLibrary(
        watch_states={(101, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
    assert show is not None
    assert show.status is RequestStatus.evicted


async def test_pinning_the_show_protects_every_season(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    s1_path = _movie_file(tmp_path, "Pinned Show S01.mkv")
    await _show_with_seasons(
        sessionmaker_,
        tmdb_id=102,
        title="Pinned Show",
        seasons={1: s1_path},
        keep_forever=True,
    )
    library = FakeLibrary(
        watch_states={(102, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(s1_path).exists()


# --------------------------------------------------------------------------- #
# Proactive sweep: no pressure gate
# --------------------------------------------------------------------------- #


async def test_proactive_sweep_evicts_past_grace_content_with_no_pressure(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Proactive.mkv")
    await _movie(sessionmaker_, tmdb_id=9, title="Proactive", library_path=library_path)
    library = FakeLibrary(
        watch_states={(9, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        # threshold_pct=101.0 (unreachable) proves this is NOT pressure-gated --
        # only `proactive=True` bypasses select_evictions' pressure check.
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
            proactive=True,
        )

    assert [o.title for o in outcomes] == ["Proactive"]
    assert not Path(library_path).exists()


# --------------------------------------------------------------------------- #
# Target-based early stop (controlled disk usage via a monkeypatched read)
# --------------------------------------------------------------------------- #


async def test_stops_once_the_target_is_reached(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A small, controlled total so each candidate's real file size is a
    # meaningful percentage -- the point of this test.
    small_path = _movie_file(tmp_path, "Small (100b).mkv", size=100)
    big_path = _movie_file(tmp_path, "Big (200b).mkv", size=200)
    older_stale = _STALE - timedelta(days=5)  # the stalest -- picked FIRST
    await _movie(sessionmaker_, tmdb_id=10, title="Small (100b)", library_path=small_path)
    await _movie(sessionmaker_, tmdb_id=11, title="Big (200b)", library_path=big_path)

    def _fake_disk_usage(_path: str) -> DiskUsage:
        return DiskUsage(root=str(tmp_path), total_bytes=1000, available_bytes=100)

    monkeypatch.setattr(eviction_service, "read_disk_usage", _fake_disk_usage)
    library = FakeLibrary(
        watch_states={
            (10, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
            (11, "movie", None): WatchState(watched=True, last_viewed_at=older_stale),
        }
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=90.0,  # matches the faked used% (900/1000)
            target_pct=70.0,
            grace_days=_GRACE_DAYS,
        )

    # used_pct=90; the stalest candidate ("Big", 200/1000=20%) alone projects
    # 90-20=70 <= target(70) -> the loop stops there, "Small" is never touched.
    assert [o.title for o in outcomes] == ["Big (200b)"]
    assert not Path(big_path).exists()
    assert Path(small_path).exists()


# --------------------------------------------------------------------------- #
# Non-blocking event loop (OP1): every blocking FS primitive this module calls
# from an async function MUST run via ``asyncio.to_thread`` -- ``_size_bytes``
# (candidate sizing), ``fs.delete`` (the actual eviction), and
# ``read_disk_usage`` (the pressure check + the preview). Each test below
# monkeypatches the relevant primitive with a REAL, synchronous ``time.sleep``
# (never ``asyncio.sleep``, which would never block the loop either way,
# threaded or not) and proves a concurrent heartbeat coroutine keeps ticking
# throughout -- the regression this guards against is exactly the "candidate
# sizing / the delete / disk reads are called synchronously inside async
# functions, blocking the event loop" bug.
# --------------------------------------------------------------------------- #

_SLOW_SECONDS = 0.3
_MIN_TICKS_IF_OFFLOADED = 10  # ~0.3s / 0.01s tick, with generous scheduling slack


class _SlowDeleteFileSystem:
    """A minimal :class:`~plex_manager.ports.filesystem.FileSystemPort` whose
    ``delete`` blocks synchronously for ``_SLOW_SECONDS`` (simulating a huge
    ``shutil.rmtree``) -- every other method is unused by eviction and simply
    never implemented."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    def available_bytes(self, path: Path) -> int:
        raise NotImplementedError

    def move(self, src: Path, dst: Path) -> None:
        raise NotImplementedError

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:
        raise NotImplementedError

    def largest_video_file(self, root: str) -> str | None:
        raise NotImplementedError

    def list_video_files(self, root: str) -> list[tuple[str, int, str]]:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        time.sleep(_SLOW_SECONDS)
        self.deleted.append(path)


async def test_size_bytes_lookup_is_offloaded_and_never_blocks_the_event_loop(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library_path = _movie_file(tmp_path, "Old Movie.mkv")
    await _movie(sessionmaker_, tmdb_id=200, title="Old Movie", library_path=library_path)
    library = FakeLibrary(
        watch_states={(200, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    def _slow_size_bytes(_path: str) -> int | None:
        time.sleep(_SLOW_SECONDS)
        return 1024

    monkeypatch.setattr(eviction_service, "_size_bytes", _slow_size_bytes)

    async with sessionmaker_() as session:
        outcomes, ticks = await _heartbeat_ticks_during(
            eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(tmp_path),
                threshold_pct=0.0,
                target_pct=0.0,
                grace_days=_GRACE_DAYS,
            )
        )

    assert [o.title for o in outcomes] == ["Old Movie"]
    assert ticks >= _MIN_TICKS_IF_OFFLOADED


async def test_fs_delete_is_offloaded_and_never_blocks_the_event_loop(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Old Movie.mkv")
    await _movie(sessionmaker_, tmdb_id=201, title="Old Movie", library_path=library_path)
    library = FakeLibrary(
        watch_states={(201, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = _SlowDeleteFileSystem()

    async with sessionmaker_() as session:
        outcomes, ticks = await _heartbeat_ticks_during(
            eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(tmp_path),
                threshold_pct=0.0,
                target_pct=0.0,
                grace_days=_GRACE_DAYS,
            )
        )

    assert [o.title for o in outcomes] == ["Old Movie"]
    assert fs.deleted == [library_path]
    assert ticks >= _MIN_TICKS_IF_OFFLOADED


async def test_read_disk_usage_in_run_eviction_sweep_is_offloaded(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    def _slow_disk_usage(_path: str) -> DiskUsage:
        time.sleep(_SLOW_SECONDS)
        return DiskUsage(root=str(tmp_path), total_bytes=1000, available_bytes=900)

    monkeypatch.setattr(eviction_service, "read_disk_usage", _slow_disk_usage)

    async with sessionmaker_() as session:
        outcomes, ticks = await _heartbeat_ticks_during(
            eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(tmp_path),
                threshold_pct=0.0,
                target_pct=0.0,
                grace_days=_GRACE_DAYS,
            )
        )

    assert outcomes == []  # no candidates seeded -- only the offload matters here
    assert ticks >= _MIN_TICKS_IF_OFFLOADED


async def test_read_disk_usage_in_preview_candidates_is_offloaded(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = FakeLibrary()

    def _slow_disk_usage(_path: str) -> DiskUsage:
        time.sleep(_SLOW_SECONDS)
        return DiskUsage(root=str(tmp_path), total_bytes=1000, available_bytes=900)

    monkeypatch.setattr(eviction_service, "read_disk_usage", _slow_disk_usage)

    async with sessionmaker_() as session:
        candidates, ticks = await _heartbeat_ticks_during(
            eviction_service.preview_candidates(
                session=session,
                library=library,
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
            )
        )

    assert candidates == []
    assert ticks >= _MIN_TICKS_IF_OFFLOADED


# --------------------------------------------------------------------------- #
# C6/C7: the TOCTOU re-check immediately before delete. Candidate assembly runs
# several awaited Plex/FS calls before a candidate is actually deleted; the
# tests below build a STALE ``EvictionCandidate`` (as assembly would have
# produced it) and then change the underlying row out from under it -- via a
# genuinely separate commit -- before calling ``_evict_one`` directly, proving
# the re-check (not the stale candidate's own fields) is what governs the
# outcome.
# --------------------------------------------------------------------------- #


async def test_recheck_honors_a_keep_forever_pin_that_lands_after_assembly(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """C7: an operator's keep_forever pin committed AFTER candidate assembly
    (but before the delete) must stop the eviction -- the correction button
    must actually work even in-flight (north-star #1). Fails before the fix
    (the file is deleted, the status flips to evicted) and passes after the
    ``get_fresh`` re-read."""
    library_path = _movie_file(tmp_path, "Recently Pinned.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=300, title="Recently Pinned", library_path=library_path
    )
    # A stale candidate exactly as assembly would have produced it BEFORE the pin.
    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Recently Pinned",
        season=None,
        status="available",
        watched=True,
        last_viewed_at=_STALE,
        keep_forever=False,
        in_flight=False,
        library_path=library_path,
        size_percent=1.0,
    )
    pending = eviction_service._MoviePending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=request_id, tmdb_id=300, size_bytes=1024
    )

    # The operator's pin lands in a SEPARATE session -- simulating it landing
    # AFTER the stale candidate above was assembled.
    async with sessionmaker_() as pin_session:
        row = await pin_session.get(MediaRequest, request_id)
        assert row is not None
        row.keep_forever = True
        await pin_session.commit()

    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session, fs=fs, candidate=stale, pending=pending
        )

    assert outcome is None
    assert Path(library_path).exists(), "a late keep_forever pin must stop the delete"
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available  # never flipped to evicted
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 300)))
            .scalars()
            .all()
        )
    assert history == []  # no eviction was ever recorded


async def test_recheck_honors_a_keep_forever_pin_on_the_parent_for_a_tv_season(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """C7, tv side: the pin lives on the PARENT show, not the season row -- a
    pin committed on the parent after a season candidate was assembled must
    still stop that season's eviction."""
    s1_path = _movie_file(tmp_path, "Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=301, title="Some Show", seasons={1: s1_path}
    )
    async with sessionmaker_() as session:
        season_row = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
                )
            )
            .scalars()
            .one()
        )
        season_request_id = season_row.id

    stale = eviction_service.EvictionCandidate(
        request_id=season_request_id,
        media_type="tv",
        title="Some Show",
        season=1,
        status="available",
        watched=True,
        last_viewed_at=_STALE,
        keep_forever=False,
        in_flight=False,
        library_path=s1_path,
        size_percent=1.0,
    )
    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=show_id,
        season_request_id=season_request_id,
        season_number=1,
        tmdb_id=301,
        size_bytes=1024,
    )

    async with sessionmaker_() as pin_session:
        parent = await pin_session.get(MediaRequest, show_id)
        assert parent is not None
        parent.keep_forever = True
        await pin_session.commit()

    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session, fs=fs, candidate=stale, pending=pending
        )

    assert outcome is None
    assert Path(s1_path).exists()
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_request_id)
        assert season_row is not None
        assert season_row.status is RequestStatus.available


async def test_recheck_skips_a_row_a_concurrent_sweep_already_evicted(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """C6: overlapping sweeps must never double-count an eviction. A stale
    candidate assembled before a CONCURRENT sweep already evicted the same row
    (deleted its file, flipped its status, recorded its history) must be
    skipped -- not re-recorded as a second successful eviction with the same
    freed_bytes."""
    library_path = _movie_file(tmp_path, "Double Swept.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=302, title="Double Swept", library_path=library_path
    )
    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Double Swept",
        season=None,
        status="available",
        watched=True,
        last_viewed_at=_STALE,
        keep_forever=False,
        in_flight=False,
        library_path=library_path,
        size_percent=1.0,
    )
    pending = eviction_service._MoviePending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=request_id, tmdb_id=302, size_bytes=1024
    )

    # A CONCURRENT sweep already evicted this exact row in a SEPARATE
    # session/commit -- deleted the file, flipped the status, logged the
    # history -- simulating the overlapping-sweeps race (a manual /ops/evict
    # racing the periodic loop).
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    fs.delete(library_path)
    async with sessionmaker_() as other_session:
        row = await other_session.get(MediaRequest, request_id)
        assert row is not None
        row.status = RequestStatus.evicted
        other_session.add(
            DownloadHistory(
                tmdb_id=302,
                torrent_hash=None,
                event_type=DownloadHistoryEvent.evicted,
                source_title="Double Swept",
                message="evicted by the other sweep",
            )
        )
        await other_session.commit()

    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session, fs=fs, candidate=stale, pending=pending
        )

    assert outcome is None  # never double-counted
    async with sessionmaker_() as session:
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 302)))
            .scalars()
            .all()
        )
    assert len(history) == 1  # still just the ONE real eviction, never two


class _PinsSecondCandidateOnFirstDeleteFs:
    """A :class:`~plex_manager.ports.filesystem.FileSystemPort` whose ``delete``
    commits ``keep_forever=True`` for a SECOND, not-yet-processed request via a
    genuinely separate session/connection on its FIRST call -- simulating an
    operator's pin landing MID-SWEEP, in the gap between two candidates'
    deletes. Every other method is unused by eviction and simply never
    implemented.

    The pin commit runs a real async DB write from a SYNCHRONOUS context
    (``delete`` executes off the event loop, inside ``asyncio.to_thread``): it
    schedules the write coroutine onto the CALLER's event loop via
    ``asyncio.run_coroutine_threadsafe`` and blocks this worker thread on the
    result -- the standard, safe pattern for a sync callback to drive async
    code on a loop that is concurrently idle (awaiting this very ``to_thread``
    call).
    """

    def __init__(
        self,
        *,
        sessionmaker: SessionMaker,
        loop: asyncio.AbstractEventLoop,
        second_request_id: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._loop = loop
        self._second_request_id = second_request_id
        self._calls = 0
        self.deleted: list[str] = []

    def available_bytes(self, path: Path) -> int:
        raise NotImplementedError

    def move(self, src: Path, dst: Path) -> None:
        raise NotImplementedError

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:
        raise NotImplementedError

    def largest_video_file(self, root: str) -> str | None:
        raise NotImplementedError

    def list_video_files(self, root: str) -> list[tuple[str, int, str]]:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        self._calls += 1
        if self._calls == 1:

            async def _pin() -> None:
                async with self._sessionmaker() as session:
                    row = await session.get(MediaRequest, self._second_request_id)
                    assert row is not None
                    row.keep_forever = True
                    await session.commit()

            future = asyncio.run_coroutine_threadsafe(_pin(), self._loop)
            future.result(timeout=5)
        real = os.path.realpath(path)
        if os.path.isdir(real):
            shutil.rmtree(real)
        else:
            os.remove(real)
        self.deleted.append(path)


async def test_mid_sweep_pin_stops_the_in_flight_eviction_of_a_later_candidate(
    tmp_path: Path,
) -> None:
    """Integration variant of C7: drives the FULL ``run_eviction_sweep`` (not
    just ``_evict_one`` directly) against a REAL file-backed database -- so a
    genuinely separate connection can land a write mid-sweep -- to prove a pin
    landing BETWEEN the first and second candidate's deletes actually stops the
    second candidate's eviction. Uses its own file-backed engine (not the
    shared in-memory ``StaticPool`` fixture): two AsyncSessions truly open at
    once needs two real connections, exactly like production.
    """
    db_path = tmp_path / "eviction_race.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    enable_sqlite_fk_enforcement(engine)  # also sets busy_timeout, like production
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm: SessionMaker = async_sessionmaker(engine, expire_on_commit=False)

    first_path = _movie_file(tmp_path, "First (older stale).mkv")
    second_path = _movie_file(tmp_path, "Second (about to be pinned).mkv")
    older_stale = _STALE - timedelta(days=5)  # stalest-first -> processed FIRST
    first_id = await _movie(sm, tmdb_id=310, title="First", library_path=first_path)
    second_id = await _movie(sm, tmdb_id=311, title="Second", library_path=second_path)

    library = FakeLibrary(
        watch_states={
            (310, "movie", None): WatchState(watched=True, last_viewed_at=older_stale),
            (311, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
        }
    )
    fs = _PinsSecondCandidateOnFirstDeleteFs(
        sessionmaker=sm, loop=asyncio.get_running_loop(), second_request_id=second_id
    )

    try:
        async with sm() as session:
            outcomes = await eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(tmp_path),
                threshold_pct=0.0,
                target_pct=0.0,
                grace_days=_GRACE_DAYS,
            )

        # The first (stalest) candidate was genuinely evicted...
        assert [o.title for o in outcomes] == ["First"]
        assert not Path(first_path).exists()
        # ...but the mid-sweep pin stopped the SECOND candidate's eviction: its
        # file survives and its status is untouched.
        assert Path(second_path).exists()
        async with sm() as session:
            first_row = await session.get(MediaRequest, first_id)
            second_row = await session.get(MediaRequest, second_id)
        assert first_row is not None and first_row.status is RequestStatus.evicted
        assert second_row is not None and second_row.status is RequestStatus.available
    finally:
        await engine.dispose()


class _ConcurrentSecondEvictFs:
    """A :class:`~plex_manager.ports.filesystem.FileSystemPort` whose ``delete``
    spawns a genuinely CONCURRENT ``_evict_one`` call for the SAME candidate, in
    a SEPARATE session, on its FIRST call -- simulating two truly overlapping
    sweeps (the periodic loop racing a manual ``POST /ops/evict`` trigger) both
    reaching the delete step for the SAME row before EITHER has committed
    anything. Proves the compare-and-swap status flip (not just the pre-delete
    ``_still_evictable`` read-recheck, which both racers pass identically) is
    what stops the second one from also recording an eviction.

    Mirrors ``_PinsSecondCandidateOnFirstDeleteFs``'s technique: the nested call
    runs on the event loop via ``asyncio.run_coroutine_threadsafe`` (this method
    executes off-loaded in a worker thread, per ``_evict_one``'s
    ``asyncio.to_thread``), and this thread blocks on its result before doing
    its OWN (by-then redundant, idempotent) file removal.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        second_call: Callable[[], Coroutine[Any, Any, eviction_service.EvictionOutcome | None]],
    ) -> None:
        self._loop = loop
        self._second_call = second_call
        self._calls = 0
        self.second_outcome: eviction_service.EvictionOutcome | None = None

    def available_bytes(self, path: Path) -> int:
        raise NotImplementedError

    def move(self, src: Path, dst: Path) -> None:
        raise NotImplementedError

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:
        raise NotImplementedError

    def largest_video_file(self, root: str) -> str | None:
        raise NotImplementedError

    def list_video_files(self, root: str) -> list[tuple[str, int, str]]:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        self._calls += 1
        if self._calls == 1:
            future = asyncio.run_coroutine_threadsafe(self._second_call(), self._loop)
            self.second_outcome = future.result(timeout=5)
        real = os.path.realpath(path)
        if os.path.isdir(real):
            shutil.rmtree(real, ignore_errors=True)
        elif os.path.exists(real):
            os.remove(real)


async def test_concurrent_evict_one_calls_for_the_same_row_never_double_count(
    tmp_path: Path,
) -> None:
    """C6, closed: two genuinely concurrent ``_evict_one`` calls for the SAME
    candidate -- each in its OWN uncommitted session, each having independently
    passed its OWN pre-delete ``_still_evictable`` re-check (both see
    ``available``, since neither has committed anything yet) -- must still
    result in EXACTLY ONE eviction: one ``evicted`` status flip, one
    ``download_history`` row, one non-``None`` outcome. Before the CAS fix this
    doubled: both proceeded to flip + log unconditionally. Uses a real
    file-backed engine (not the shared in-memory ``StaticPool`` fixture): two
    AsyncSessions truly open at once needs two real connections.
    """
    db_path = tmp_path / "eviction_double_count.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    enable_sqlite_fk_enforcement(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm: SessionMaker = async_sessionmaker(engine, expire_on_commit=False)

    library_path = _movie_file(tmp_path, "Raced By Two Sweeps.mkv")
    request_id = await _movie(
        sm, tmdb_id=320, title="Raced By Two Sweeps", library_path=library_path
    )
    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Raced By Two Sweeps",
        season=None,
        status="available",
        watched=True,
        last_viewed_at=_STALE,
        keep_forever=False,
        in_flight=False,
        library_path=library_path,
        size_percent=1.0,
    )
    pending = eviction_service._MoviePending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=request_id, tmdb_id=320, size_bytes=1024
    )

    async def _second_call() -> eviction_service.EvictionOutcome | None:
        async with sm() as second_session:
            return await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
                session=second_session, fs=fs, candidate=stale, pending=pending
            )

    fs = _ConcurrentSecondEvictFs(loop=asyncio.get_running_loop(), second_call=_second_call)

    try:
        async with sm() as first_session:
            first_outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
                session=first_session, fs=fs, candidate=stale, pending=pending
            )

        # EXACTLY ONE of the two overlapping calls actually recorded the
        # eviction -- never both, never neither.
        outcomes = [o for o in (first_outcome, fs.second_outcome) if o is not None]
        assert len(outcomes) == 1

        assert not Path(library_path).exists()  # the file IS gone (idempotent double-delete)
        async with sm() as session:
            row = await session.get(MediaRequest, request_id)
            assert row is not None
            assert row.status is RequestStatus.evicted  # flipped exactly once
            history = (
                (
                    await session.execute(
                        select(DownloadHistory).where(DownloadHistory.tmdb_id == 320)
                    )
                )
                .scalars()
                .all()
            )
        assert len(history) == 1  # one eviction, one history row -- never double-counted
    finally:
        await engine.dispose()
