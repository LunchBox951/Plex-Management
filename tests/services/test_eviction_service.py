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
from typing import Any, Literal, cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.plex.library import PlexLibraryError
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
    User,
    WatchlistItem,
)
from plex_manager.ports.library import WatchState
from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.ports.repositories import SeasonRequestRecord
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import eviction_service, request_service, season_request_service
from plex_manager.services.purge_service import PurgeOutcome, PurgeResult
from tests.web.fakes import FakeLibrary, FakeTmdb

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime.now(UTC)
_GRACE_DAYS = 30
_STALE = _NOW - timedelta(days=_GRACE_DAYS + 10)
_RECENT = _NOW - timedelta(days=1)
# The cutoff ``run_eviction_sweep`` would compute for ``_GRACE_DAYS`` -- passed
# explicitly by every test that calls ``_evict_one`` directly (bypassing
# ``run_eviction_sweep``, which normally computes and threads it), so the #209
# pre-claim re-read's watched+grace check evaluates the same way a real sweep's
# would.
_GRACE_CUTOFF = _NOW - timedelta(days=_GRACE_DAYS)


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
    # R2-3: Plex is refreshed for the deleted path so a later "Request again" sees it
    # gone (pending) rather than a stale in-library 'available'.
    assert (library_path, "movie") in library.scan_calls

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted
        # The finalize cleared the breadcrumb in the same commit as the history
        # row: 'evicted' + a non-NULL library_path always means "claimed but not
        # finalized" (the crash-recovery signature), so a COMPLETED eviction must
        # never keep looking like an interrupted one.
        assert row.library_path is None

        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 1)))
            .scalars()
            .all()
        )
    assert len(history) == 1
    assert history[0].event_type is DownloadHistoryEvent.evicted
    assert history[0].torrent_hash is None
    assert history[0].source_title == "Old Movie"


async def test_candidate_outside_the_swept_root_is_not_evicted(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # A watched, past-grace, un-pinned movie whose library_path is under a DIFFERENT
    # root than the one being swept must NOT be a candidate for THIS root's sweep --
    # otherwise it could consume the target here (starving valid candidates) or delete
    # from the wrong filesystem while this root's pressure is measured. The fs guard
    # would ALLOW the delete (the file is under the allowed tmp_path root), so only the
    # candidate-scope filter stops it.
    swept_root = tmp_path / "movies"
    swept_root.mkdir()
    other_root = tmp_path / "other"
    other_root.mkdir()
    outside = other_root / "Elsewhere.mkv"
    outside.write_bytes(b"0" * 1024)
    request_id = await _movie(
        sessionmaker_, tmdb_id=99, title="Elsewhere", library_path=str(outside)
    )
    library = FakeLibrary(
        watch_states={(99, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(swept_root),  # sweeping movies/, but the file lives under other/
            threshold_pct=0.0,  # always-evict pressure, so only the scope filter can stop it
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []  # excluded from this root's candidates
    assert outside.exists()  # the wrong-root file is untouched
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None and row.status is RequestStatus.available


async def test_parent_root_sweep_never_claims_a_nested_child_roots_content(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # Codex P2 (nested roots): anime_movie_root is configured INSIDE movies_root
    # (its own mount, its own disk pressure, its own sweep iteration). The PARENT
    # root's sweep must not claim breadcrumbs the nested child root owns -- plain
    # lexical containment would evict the child mount's content under the parent's
    # pressure. Deepest-match assignment (``all_roots``) gives each breadcrumb
    # exactly one owning sweep: the parent evicts only its own direct content, and
    # the child's own sweep evicts the nested title.
    movies_root = tmp_path / "movies"
    anime_root = movies_root / "anime"
    anime_root.mkdir(parents=True)
    normal_file = movies_root / "Old Movie.mkv"
    normal_file.write_bytes(b"0" * 1024)
    anime_file = anime_root / "Old Anime.mkv"
    anime_file.write_bytes(b"0" * 1024)
    normal_id = await _movie(
        sessionmaker_, tmdb_id=501, title="Old Movie", library_path=str(normal_file)
    )
    anime_id = await _movie(
        sessionmaker_, tmdb_id=502, title="Old Anime", library_path=str(anime_file)
    )
    # Both watched + past grace: only the ownership scope decides who sweeps what.
    library = FakeLibrary(
        watch_states={
            (501, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
            (502, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
        }
    )
    fs = LocalFileSystem(library_roots=[str(movies_root), str(anime_root)])
    all_roots = [str(movies_root), str(anime_root)]

    # The PARENT sweep: evicts its own direct movie, never the nested anime title.
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(movies_root),
            all_roots=all_roots,
            threshold_pct=0.0,  # always-evict pressure: only ownership can stop it
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )
    assert [o.title for o in outcomes] == ["Old Movie"]
    assert not normal_file.exists()
    assert anime_file.exists()  # the child mount's content is untouched
    async with sessionmaker_() as session:
        normal_row = await session.get(MediaRequest, normal_id)
        anime_row = await session.get(MediaRequest, anime_id)
    assert normal_row is not None and normal_row.status is RequestStatus.evicted
    assert anime_row is not None and anime_row.status is RequestStatus.available

    # The CHILD root's own sweep is the one that owns (and evicts) the anime title.
    async with sessionmaker_() as session:
        child_outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(anime_root),
            all_roots=all_roots,
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )
    assert [o.title for o in child_outcomes] == ["Old Anime"]
    assert not anime_file.exists()


async def test_assemble_candidates_assigns_nested_root_content_to_the_child_only(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # The same nested-root ownership through ``assemble_candidates`` -- the shared
    # raw read behind BOTH the ops /disk preview (via preview_candidates) and the
    # retention-telemetry sweep, so neither double-counts a nested child root's
    # content under its parent's row/metrics.
    movies_root = tmp_path / "movies"
    anime_root = movies_root / "anime"
    anime_root.mkdir(parents=True)
    normal_file = movies_root / "Plain.mkv"
    normal_file.write_bytes(b"0" * 512)
    anime_file = anime_root / "Nested.mkv"
    anime_file.write_bytes(b"0" * 512)
    await _movie(sessionmaker_, tmdb_id=511, title="Plain", library_path=str(normal_file))
    await _movie(sessionmaker_, tmdb_id=512, title="Nested", library_path=str(anime_file))
    library = FakeLibrary()
    all_roots = [str(movies_root), str(anime_root)]

    async with sessionmaker_() as session:
        parent = await eviction_service.assemble_candidates(
            session=session,
            library=library,
            media_type="movie",
            root_path=str(movies_root),
            root_total_bytes=0,
            all_roots=all_roots,
        )
        child = await eviction_service.assemble_candidates(
            session=session,
            library=library,
            media_type="movie",
            root_path=str(anime_root),
            root_total_bytes=0,
            all_roots=all_roots,
        )
    assert [c.title for c in parent] == ["Plain"]
    assert [c.title for c in child] == ["Nested"]


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


async def test_never_evicts_a_movie_on_any_user_watchlist(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Watchlisted.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=4004, title="Watchlisted", library_path=library_path
    )
    async with sessionmaker_() as session:
        user = User(username="watcher")
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=4004, media_type=MediaType.movie))
        await session.commit()
    library = FakeLibrary(
        watch_states={(4004, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
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
        row = await session.get(MediaRequest, request_id)

    assert outcomes == []
    assert row is not None and row.status is RequestStatus.available
    assert Path(library_path).exists()


async def test_movie_claim_cas_rejects_watchlist_added_after_assembly(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    request_id = await _movie(
        sessionmaker_,
        tmdb_id=4005,
        title="Late Watchlist",
        library_path=_movie_file(tmp_path, "Late Watchlist.mkv"),
    )
    async with sessionmaker_() as session:
        user = User(username="late-watcher")
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=4005, media_type=MediaType.movie))
        await session.commit()

    async with sessionmaker_() as session:
        claimed = await SqlRequestRepository(session).set_status_if_in(
            request_id,
            RequestStatus.evicted.value,
            frozenset({RequestStatus.available.value}),
            require_unpinned=True,
            require_not_watchlisted=True,
        )
        row = await session.get(MediaRequest, request_id)
    assert claimed is False
    assert row is not None and row.status is RequestStatus.available


async def test_tv_claim_cas_rejects_watchlist_added_after_assembly(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    show_id = await _show_with_seasons(
        sessionmaker_,
        tmdb_id=4006,
        title="Late Show Watchlist",
        seasons={1: _movie_file(tmp_path, "Late Show Watchlist.mkv")},
    )
    async with sessionmaker_() as session:
        season = (
            await session.execute(
                select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
            )
        ).scalar_one()
        season_id = season.id
        user = User(username="late-show-watcher")
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=4006, media_type=MediaType.tv))
        await session.commit()

    async with sessionmaker_() as session:
        claimed = await SqlSeasonRequestRepository(session).set_status_if_in(
            season_id,
            RequestStatus.evicted.value,
            frozenset({RequestStatus.available.value}),
            require_parent_unpinned=True,
            require_not_watchlisted=True,
        )
        row = await session.get(SeasonRequest, season_id)
    assert claimed is False
    assert row is not None and row.status is RequestStatus.available


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
        breadcrumbs = {s.season_number: s.library_path for s in seasons}
        show = await session.get(MediaRequest, show_id)

    assert by_season == {1: RequestStatus.evicted, 2: RequestStatus.available}
    # The finalize cleared season 1's breadcrumb (completed eviction, never to be
    # mistaken for an interrupted one); season 2's untouched breadcrumb survives.
    assert breadcrumbs == {1: None, 2: s2_path}
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
# R4-P1 (ADR-0012): a season eviction's parent-rollup write must tolerate
# colliding with a NEWER active request for the same show, without losing the
# season's own CAS-to-evicted + history row -- the honest "the file is gone"
# record must never be rolled back just because the coarser parent rollup
# couldn't also be written.
# --------------------------------------------------------------------------- #


async def test_evicting_a_season_of_a_settled_parent_tolerates_an_active_dedup_collision(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """An OLD parent whose rollup already settled to 'available' (OUTSIDE
    ``uq_media_requests_active``'s predicate) can legitimately coexist with a
    NEWER, genuinely active request for the SAME ``(tmdb_id, media_type)`` --
    e.g. a fresh request naming a later season after the old one finished.
    Evicting one of the OLD parent's remaining seasons folds its rollup back to
    the ACTIVE ``partially_available`` (``[evicted, available] ->
    partially_available``, ``domain/season_rollup``), which collides with the
    newer row's slot in that same partial unique index. Before the fix, that
    collision's ``IntegrityError`` bubbled out of ``_recompute_parent`` AFTER
    ``fs.delete`` had already removed the file, was caught by the sweep's
    per-candidate guard, and rolled back the season's own CAS + history along
    with it -- leaving the DB honestly reporting 'available' for a season whose
    file was already gone. This proves the season CAS/history now survive."""
    s1_path = _movie_file(tmp_path, "Old Show S01.mkv")
    s2_path = _movie_file(tmp_path, "Old Show S02.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=400, title="Old Show", seasons={1: s1_path, 2: s2_path}
    )

    # A NEWER, SEPARATE active request for the SAME (tmdb_id, media_type) --
    # legal today because the OLD parent's rollup ('available') sits OUTSIDE
    # uq_media_requests_active's predicate, so this insert does not collide.
    async with sessionmaker_() as session:
        newer = MediaRequest(
            tmdb_id=400,
            media_type=MediaType.tv,
            title="Old Show",
            status=RequestStatus.downloading,
        )
        session.add(newer)
        await session.commit()
        newer_id = newer.id

    library = FakeLibrary(
        watch_states={
            (400, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE),
            (400, "tv", 2): WatchState(watched=False, last_viewed_at=None),
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

    # (c) freed bytes were counted -- a real EvictionOutcome, not a skip.
    assert [(o.title, o.season) for o in outcomes] == [("Old Show", 1)]
    assert outcomes[0].freed_bytes is not None
    # The delete itself is the ultimate source of truth -- files-gone must match
    # status-evicted, with no rollback undoing one but not the other.
    assert not Path(s1_path).exists()
    assert Path(s2_path).exists()  # unwatched season 2 untouched

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
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 400)))
            .scalars()
            .all()
        )
        old_show = await session.get(MediaRequest, show_id)
        newer_row = await session.get(MediaRequest, newer_id)

    # (a) the season CAS is the source of truth and it committed, despite the
    # parent-rollup write colliding with the newer request's active slot.
    assert by_season[1] is RequestStatus.evicted
    assert by_season[2] is RequestStatus.available  # untouched
    # (b) the eviction history row survived alongside the season CAS.
    assert len(history) == 1
    assert history[0].event_type is DownloadHistoryEvent.evicted
    assert history[0].torrent_hash is None
    # The coarser parent-rollup write failed SOFTLY: the old parent is left at
    # its PRIOR (still-honest) status rather than a half-applied 'partially_
    # available' that never actually made it past the collision.
    assert old_show is not None
    assert old_show.status is RequestStatus.available
    # (d) the newer active request for the same show is completely untouched --
    # same id, same active status, never resurrected/rewritten by the collision.
    assert newer_row is not None
    assert newer_row.id == newer_id
    assert newer_row.status is RequestStatus.downloading
    # (e) no IntegrityError escaped run_eviction_sweep -- the outcome above is
    # not None/skipped, proving the sweep's per-candidate rollback guard never
    # had to catch (and undo) this eviction.


async def test_parent_rollup_collision_is_logged_when_tolerated(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The tolerated collision is never silently swallowed -- honesty over
    silence: an operator can see the coarse rollup is momentarily stale even
    though the season itself evicted cleanly."""
    s1_path = _movie_file(tmp_path, "Quiet Show S01.mkv")
    s2_path = _movie_file(tmp_path, "Quiet Show S02.mkv")
    await _show_with_seasons(
        sessionmaker_, tmdb_id=402, title="Quiet Show", seasons={1: s1_path, 2: s2_path}
    )
    async with sessionmaker_() as session:
        session.add(
            MediaRequest(
                tmdb_id=402,
                media_type=MediaType.tv,
                title="Quiet Show",
                status=RequestStatus.downloading,
            )
        )
        await session.commit()

    library = FakeLibrary(
        watch_states={
            (402, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE),
            (402, "tv", 2): WatchState(watched=False, last_viewed_at=None),
        }
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.season_request_service"):
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

    assert [(o.title, o.season) for o in outcomes] == [("Quiet Show", 1)]
    assert "active-dedup slot" in caplog.text


async def test_normal_season_transition_still_raises_on_the_same_active_dedup_collision(
    sessionmaker_: SessionMaker,
) -> None:
    """The tolerance is an OPT-IN scoped to eviction: a normal (non-eviction)
    caller of ``season_request_service.set_status_if_in``
    (``tolerate_active_conflict`` defaults to ``False``) must still see the
    IDENTICAL parent-rollup collision as a hard failure -- proving the fix did
    not weaken ``uq_media_requests_active``'s dedup guarantee for every other
    season-transition call site, only eviction's."""
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=401, title="Another Old Show", seasons={1: None, 2: None}
    )
    async with sessionmaker_() as session:
        session.add(
            MediaRequest(
                tmdb_id=401,
                media_type=MediaType.tv,
                title="Another Old Show",
                status=RequestStatus.downloading,
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        season_row = (
            (
                await session.execute(
                    select(SeasonRequest).where(
                        SeasonRequest.media_request_id == show_id,
                        SeasonRequest.season_number == 1,
                    )
                )
            )
            .scalars()
            .one()
        )
        with pytest.raises(IntegrityError):
            # Same collision as the tolerated test above, but WITHOUT the
            # eviction-only opt-in -- must propagate, never be swallowed.
            await season_request_service.set_status_if_in(
                session,
                media_request_id=show_id,
                season_request_id=season_row.id,
                status=RequestStatus.evicted.value,
                allowed_from=frozenset({RequestStatus.available.value}),
            )


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
# R4-6 (ADR-0012): hardlink-aware freed-bytes accounting. A same-filesystem
# import (``fs.hardlink_or_copy``) can leave the placed library file with
# another hard link still present (the download client's own seed copy the
# import never removes) -- deleting only the library path in that case
# reclaims ~0 bytes. The eviction itself stays honest (the library copy IS
# removed, status/history still flip to 'evicted'); only the freed-bytes
# COUNT must reflect reality instead of the file's nominal size.
# --------------------------------------------------------------------------- #


async def test_hardlinked_movie_reports_zero_freed_bytes_but_still_evicts_honestly(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Hardlinked.mkv", size=4096)
    # A second directory entry pointing at the SAME inode -- simulating the
    # download client's seed copy that hardlink_or_copy left behind at import.
    seed_copy = tmp_path / "seed" / "Hardlinked.mkv"
    seed_copy.parent.mkdir(parents=True)
    os.link(library_path, seed_copy)
    assert os.stat(library_path).st_nlink >= 2

    request_id = await _movie(
        sessionmaker_, tmdb_id=500, title="Hardlinked", library_path=library_path
    )
    library = FakeLibrary(
        watch_states={(500, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
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

    # The library copy IS removed -- the eviction itself is still honest -- but
    # the SEED copy (another hard link to the same inode) keeps the bytes
    # allocated, so this reclaimed NOTHING.
    assert [o.title for o in outcomes] == ["Hardlinked"]
    assert outcomes[0].freed_bytes == 0
    assert not Path(library_path).exists()
    assert seed_copy.exists()  # the other link is untouched, and still holds the bytes
    assert seed_copy.read_bytes() == b"0" * 4096

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        # Still flipped -- the LIBRARY no longer has it, regardless of what the
        # freed-bytes accounting says.
        assert row.status is RequestStatus.evicted
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 500)))
            .scalars()
            .all()
        )
    assert len(history) == 1
    assert history[0].event_type is DownloadHistoryEvent.evicted


async def test_single_link_movie_reports_its_full_size_as_freed_bytes(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "SingleLink.mkv", size=2048)
    await _movie(sessionmaker_, tmdb_id=501, title="SingleLink", library_path=library_path)
    library = FakeLibrary(
        watch_states={(501, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
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

    assert [o.title for o in outcomes] == ["SingleLink"]
    assert outcomes[0].freed_bytes == 2048


async def test_sweep_keeps_evicting_past_the_estimate_when_actual_freed_bytes_fall_short(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upfront (estimate-based) ``select_evictions`` projects the stalest
    candidate alone ("HardlinkedBig") is enough to reach the target -- but it
    actually frees ~0 bytes (hardlinked). The sweep must keep going, drawing
    the NEXT stalest candidate, rather than stopping on a projection that never
    actually happened -- the "keeps going until it actually reclaims enough"
    half of R4-6."""
    hardlinked_path = _movie_file(tmp_path, "HardlinkedBig (200b).mkv", size=200)
    seed_copy = tmp_path / "seed" / "HardlinkedBig (200b).mkv"
    seed_copy.parent.mkdir(parents=True)
    os.link(hardlinked_path, seed_copy)
    small_path = _movie_file(tmp_path, "Small (100b).mkv", size=100)
    older_stale = _STALE - timedelta(days=5)  # the stalest -- picked FIRST
    await _movie(
        sessionmaker_, tmdb_id=502, title="HardlinkedBig (200b)", library_path=hardlinked_path
    )
    await _movie(sessionmaker_, tmdb_id=503, title="Small (100b)", library_path=small_path)

    def _fake_disk_usage(_path: str) -> DiskUsage:
        return DiskUsage(root=str(tmp_path), total_bytes=1000, available_bytes=100)

    monkeypatch.setattr(eviction_service, "read_disk_usage", _fake_disk_usage)
    library = FakeLibrary(
        watch_states={
            (502, "movie", None): WatchState(watched=True, last_viewed_at=older_stale),
            (503, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
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

    # Before the fix: only HardlinkedBig would be evicted (the ESTIMATE alone
    # projected 90-20=70<=70), leaving Small untouched despite HardlinkedBig
    # having reclaimed nothing. After the fix: HardlinkedBig frees 0 bytes
    # (hardlinked) -> the running total never reaches the target -> the sweep
    # keeps going and also evicts Small.
    assert [o.title for o in outcomes] == ["HardlinkedBig (200b)", "Small (100b)"]
    assert outcomes[0].freed_bytes == 0
    assert outcomes[1].freed_bytes == 100
    assert not Path(hardlinked_path).exists()
    assert not Path(small_path).exists()
    assert seed_copy.exists()  # the other hard link survives, still holding the bytes


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
    ``shutil.rmtree``) -- every other method except ``reclaimable_bytes`` (which
    ``_evict_one`` now calls BEFORE every delete, see R4-6) is unused by
    eviction and simply never implemented."""

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

    def delete_guard_refuses(self, path: str) -> bool:
        # These fakes only ever model in-root deletion targets (real tmp_path files
        # the test intends to delete), so purge_library_path's pre-measure containment
        # gate must pass -- return False (delete would NOT refuse) so it proceeds to
        # measure + delete as before.
        return False

    def reclaimable_bytes(self, path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

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
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(300, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
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
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(301, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
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
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(302, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
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
    deletes. Every other method except ``reclaimable_bytes`` (called BEFORE
    every delete, see R4-6) is unused by eviction and simply never implemented.

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

    def delete_guard_refuses(self, path: str) -> bool:
        # These fakes only ever model in-root deletion targets (real tmp_path files
        # the test intends to delete), so purge_library_path's pre-measure containment
        # gate must pass -- return False (delete would NOT refuse) so it proceeds to
        # measure + delete as before.
        return False

    def reclaimable_bytes(self, path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

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

    def delete_guard_refuses(self, path: str) -> bool:
        # These fakes only ever model in-root deletion targets (real tmp_path files
        # the test intends to delete), so purge_library_path's pre-measure containment
        # gate must pass -- return False (delete would NOT refuse) so it proceeds to
        # measure + delete as before.
        return False

    def reclaimable_bytes(self, path: str) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

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
                session=second_session,
                fs=fs,
                library=FakeLibrary(
                    watch_states={
                        (320, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)
                    }
                ),
                candidate=stale,
                pending=pending,
                grace_cutoff=_GRACE_CUTOFF,
            )

    fs = _ConcurrentSecondEvictFs(loop=asyncio.get_running_loop(), second_call=_second_call)

    try:
        async with sm() as first_session:
            first_outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
                session=first_session,
                fs=fs,
                library=FakeLibrary(
                    watch_states={
                        (320, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)
                    }
                ),
                candidate=stale,
                pending=pending,
                grace_cutoff=_GRACE_CUTOFF,
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


# --------------------------------------------------------------------------- #
# #67 -- the eviction CLAIM: the status compare-and-swap runs BEFORE any delete,
# folding the pin into the compared predicate. These tests deliberately bypass
# the cheap ``_still_evictable`` early read-filter (monkeypatched to always pass)
# so the assertions prove the CLAIM ITSELF -- not the pre-read -- is what gates
# the delete. A fix that only re-reads keep_forever (without moving the delete
# after the claim) would fail these: the delete would run before the claim.
# --------------------------------------------------------------------------- #


async def _always_evictable(_session: AsyncSession, _pending: object) -> bool:
    """Force ``_evict_one`` past its cheap read-filter so only the CLAIM can stop it."""
    return True


async def test_claim_refuses_to_delete_a_movie_pinned_before_the_claim(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#67 (movie): a ``keep_forever`` pin committed after candidate assembly but
    before the claim makes the pre-delete CAS (``... AND keep_forever = false``)
    match no row -- so the file is left intact, the row stays ``available`` (never
    ``evicted``), and ``purge_library_path`` is never even reached. Proves the
    delete is gated on the CLAIM, not merely on a re-read."""
    library_path = _movie_file(tmp_path, "Pinned Before Claim.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=400, title="Pinned Before Claim", library_path=library_path
    )
    # The operator's pin lands (separate session) AFTER the candidate below was
    # assembled but BEFORE the claim runs.
    async with sessionmaker_() as pin_session:
        row = await pin_session.get(MediaRequest, request_id)
        assert row is not None
        row.keep_forever = True
        await pin_session.commit()

    monkeypatch.setattr(eviction_service, "_still_evictable", _always_evictable)

    async def _forbidden_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        raise AssertionError("purge must never run once the claim has lost")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _forbidden_purge)

    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Pinned Before Claim",
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
        media_request_id=request_id, tmdb_id=400, size_bytes=1024
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(400, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert Path(library_path).exists(), "the pin must stop the delete at the claim"
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available  # never flipped to evicted
        assert row.keep_forever is True  # the pin stands
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 400)))
            .scalars()
            .all()
        )
    assert history == []  # no eviction was ever recorded


async def test_claim_refuses_to_delete_a_season_whose_parent_pinned_before_the_claim(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#67 (tv): the pin lives on the PARENT show, so the season claim folds it in
    via a correlated subquery (``require_parent_unpinned``). A parent pin landing
    before the claim makes the season CAS match no row -- season file intact,
    season row stays ``available``. Proves the parent-pin guard is enforced at the
    CLAIM, past the bypassed read-filter."""
    s1_path = _movie_file(tmp_path, "Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=401, title="Pinned Parent Show", seasons={1: s1_path}
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

    async with sessionmaker_() as pin_session:
        parent = await pin_session.get(MediaRequest, show_id)
        assert parent is not None
        parent.keep_forever = True
        await pin_session.commit()

    monkeypatch.setattr(eviction_service, "_still_evictable", _always_evictable)

    async def _forbidden_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        raise AssertionError("purge must never run once the parent-pin claim has lost")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _forbidden_purge)

    stale = eviction_service.EvictionCandidate(
        request_id=season_request_id,
        media_type="tv",
        title="Pinned Parent Show",
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
        tmdb_id=401,
        size_bytes=1024,
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(401, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert Path(s1_path).exists(), "the parent pin must stop the delete at the claim"
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_request_id)
        assert season_row is not None
        assert season_row.status is RequestStatus.available


async def test_claim_loser_on_a_concurrent_status_change_never_deletes(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#67: a row moved OUT of ``available`` (to a non-evictable status) by a
    concurrent writer between assembly and the claim loses the CAS -- so the file
    is never deleted and no second ``evicted`` history is written. Bypasses the
    read-filter to prove the CLAIM is the gate."""
    library_path = _movie_file(tmp_path, "Concurrently Moved.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=402, title="Concurrently Moved", library_path=library_path
    )
    # A concurrent writer moves it to a non-evictable status (e.g. re-opened for
    # a supplementary download) after the candidate was assembled.
    async with sessionmaker_() as other_session:
        row = await other_session.get(MediaRequest, request_id)
        assert row is not None
        row.status = RequestStatus.completed
        await other_session.commit()

    monkeypatch.setattr(eviction_service, "_still_evictable", _always_evictable)

    async def _forbidden_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        raise AssertionError("purge must never run for a claim loser")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _forbidden_purge)

    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Concurrently Moved",
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
        media_request_id=request_id, tmdb_id=402, size_bytes=1024
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(402, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert Path(library_path).exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.completed  # the concurrent status stands
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 402)))
            .scalars()
            .all()
        )
    assert history == []


async def test_failed_delete_restores_the_claimed_row_to_available(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#67: the claim wins (row flips to ``evicted``, committed) and THEN the
    filesystem delete fails -- the file is still on disk. The row must be RESTORED
    to ``available`` so a failed unlink never strands an ``evicted`` status over a
    still-watchable file, and no eviction history is written for a delete that did
    not happen. The next sweep can retry honestly."""
    library_path = _movie_file(tmp_path, "Unlink Fails.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=403, title="Unlink Fails", library_path=library_path
    )

    async def _erroring_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        # The claim already flipped the row to 'evicted' and committed; the delete
        # itself now fails (e.g. EACCES / EIO) with nothing removed.
        return PurgeResult(PurgeOutcome.error, 0, "OSError")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _erroring_purge)

    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Unlink Fails",
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
        media_request_id=request_id, tmdb_id=403, size_bytes=1024
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(403, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert Path(library_path).exists()  # the file is still there -- delete failed
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        # Restored: never left stranded as 'evicted' over a live file.
        assert row.status is RequestStatus.available
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 403)))
            .scalars()
            .all()
        )
    assert history == []  # no eviction recorded for a delete that never happened


async def test_deferred_purge_does_not_restore_while_replacement_import_owns_path(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A purge deferred to an active replacement import is not a real unlink
    failure: restoring the old row would leave two live rows claiming one path
    once the replacement import finalizes. Keep the eviction claim + breadcrumb
    standing so the next sweep's interrupted-eviction recovery can decide after
    the import settles."""
    library_path = _movie_file(tmp_path, "Import Owns Path.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=423, title="Import Owns Path", library_path=library_path
    )

    async def _deferred_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        assert hold_purge_registration is True
        return PurgeResult(
            PurgeOutcome.deferred,
            0,
            "deferred: an import is placing into this path",
        )

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _deferred_purge)

    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Import Owns Path",
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
        media_request_id=request_id, tmdb_id=423, size_bytes=1024
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(423, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert Path(library_path).exists()  # the import owns the path; nothing was deleted
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 423)))
            .scalars()
            .all()
        )
    assert row is not None
    assert row.status is RequestStatus.evicted
    assert row.library_path == library_path
    assert history == []


async def test_tv_purge_registration_stays_held_until_eviction_finalizes(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The import-vs-purge exclusion must cover finalization too, not only the
    filesystem delete. A same-row TV re-import beginning after delete but before
    the breadcrumb clear can stamp the same deterministic season_dir; eviction's
    value-predicated clear would then erase the fresh breadcrumb. Holding the
    purge registration through the finalize commit makes that placement defer."""
    season_dir = tmp_path / "tv" / "Finalize Race" / "Season 01"
    episode = season_dir / "Finalize Race - S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"0" * 1024)
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=424, title="Finalize Race", seasons={1: str(season_dir)}
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

    attempted_placement_allowed: list[bool] = []
    original_clear = SqlSeasonRequestRepository.clear_library_path_if_set

    async def _clear_after_import_attempt(
        self: SqlSeasonRequestRepository,
        season_request_id: int,
        *,
        expected_path: str | None = None,
        expected_statuses: frozenset[str] | None = None,
    ) -> bool:
        attempted_placement_allowed.append(
            eviction_service.purge_service.begin_placement(str(season_dir))
        )
        if attempted_placement_allowed[-1]:
            eviction_service.purge_service.end_placement(str(season_dir))
        return await original_clear(
            self,
            season_request_id,
            expected_path=expected_path,
            expected_statuses=expected_statuses,
        )

    monkeypatch.setattr(
        SqlSeasonRequestRepository,
        "clear_library_path_if_set",
        _clear_after_import_attempt,
    )

    stale = eviction_service.EvictionCandidate(
        request_id=season_request_id,
        media_type="tv",
        title="Finalize Race",
        season=1,
        status="available",
        watched=True,
        last_viewed_at=_STALE,
        keep_forever=False,
        in_flight=False,
        library_path=str(season_dir),
        size_percent=1.0,
    )
    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=show_id,
        season_request_id=season_request_id,
        season_number=1,
        tmdb_id=424,
        size_bytes=1024,
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path / "tv")])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(424, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is not None
    assert attempted_placement_allowed == [False]
    assert not season_dir.exists()
    # Registration was released after finalize, so later imports can proceed.
    assert eviction_service.purge_service.begin_placement(str(season_dir)) is True
    eviction_service.purge_service.end_placement(str(season_dir))


async def test_failed_season_delete_restores_the_claimed_season_to_available(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#67 (tv): the season claim wins and its delete then fails -- the season row
    is restored from ``evicted`` back to ``available`` (parent rollup recomputed),
    never stranded over a still-present season file."""
    s1_path = _movie_file(tmp_path, "Restore Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=404, title="Restore Show", seasons={1: s1_path}
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

    async def _erroring_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        return PurgeResult(PurgeOutcome.error, 0, "OSError")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _erroring_purge)

    stale = eviction_service.EvictionCandidate(
        request_id=season_request_id,
        media_type="tv",
        title="Restore Show",
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
        tmdb_id=404,
        size_bytes=1024,
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(404, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert Path(s1_path).exists()
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_request_id)
        assert season_row is not None
        assert season_row.status is RequestStatus.available
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.status is RequestStatus.available  # rollup restored too


async def test_failed_season_delete_after_rearm_keeps_the_breadcrumb(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#117: a same-row TV re-request re-arms the claimed season (evicted ->
    pending) WHILE the eviction purge is still in flight; the purge then errors.
    The restore folds the season back to 'available' (its file never left disk),
    and the ``library_path`` breadcrumb MUST survive the whole claim window -- an
    'available' row over a live file has to keep its eviction/report handle, or
    disk pressure could never reclaim it. Before the fix the re-arm cleared the
    breadcrumb during the claim window, leaving a permanently unreclaimable live
    file behind whenever the in-flight purge failed."""
    season_dir = tmp_path / "tv" / "Rearm Restore" / "Season 01"
    episode = season_dir / "Rearm Restore - S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"0" * 1024)
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=811, title="Rearm Restore", seasons={1: str(season_dir)}
    )
    async with sessionmaker_() as session:
        season_request_id = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
                )
            )
            .scalars()
            .one()
        ).id

    async def _rearm_then_error(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        # The concurrent re-request lands DURING the purge window: it re-arms the
        # just-committed 'evicted' claim back to 'pending' (ensure_seasons, the
        # exact path request_service drives) in its own session, then the purge
        # errors.
        async with sessionmaker_() as other:
            await season_request_service.ensure_seasons(
                other, None, media_request_id=show_id, tmdb_id=811, seasons=[1]
            )
            await other.commit()
        return PurgeResult(PurgeOutcome.error, 0, "OSError")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _rearm_then_error)

    stale = eviction_service.EvictionCandidate(
        request_id=season_request_id,
        media_type="tv",
        title="Rearm Restore",
        season=1,
        status="available",
        watched=True,
        last_viewed_at=_STALE,
        keep_forever=False,
        in_flight=False,
        library_path=str(season_dir),
        size_percent=1.0,
    )
    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=show_id,
        season_request_id=season_request_id,
        season_number=1,
        tmdb_id=811,
        size_bytes=1024,
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path / "tv")])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(811, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert season_dir.exists()  # the purge errored; the file never left disk
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_request_id)
    assert season_row is not None
    assert season_row.status is RequestStatus.available  # folded back from the re-arm
    # THE #117 invariant: an 'available' row over a live file always carries its
    # breadcrumb, so a future eviction / report-issue purge can still reclaim it.
    assert season_row.library_path == str(season_dir)


async def test_successful_season_delete_after_rearm_clears_the_breadcrumb_once(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#117 counterpart: when the purge SUCCEEDS after a same-row re-request
    re-armed the claimed season, the finalize still clears the (now-preserved)
    breadcrumb exactly once -- the re-armed pre-grab status is in
    ``_STALE_SEASON_BREADCRUMB_CLEAR_STATUSES`` -- so the re-grab proceeds over a
    genuinely-deleted file with no stale handle left behind and a single eviction
    history row."""
    season_dir = tmp_path / "tv" / "Rearm Delete" / "Season 01"
    episode = season_dir / "Rearm Delete - S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"0" * 1024)
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=812, title="Rearm Delete", seasons={1: str(season_dir)}
    )
    async with sessionmaker_() as session:
        season_request_id = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
                )
            )
            .scalars()
            .one()
        ).id

    async def _rearm_then_delete(
        _fs: object, path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        async with sessionmaker_() as other:
            await season_request_service.ensure_seasons(
                other, None, media_request_id=show_id, tmdb_id=812, seasons=[1]
            )
            await other.commit()
        shutil.rmtree(path)  # a real, successful delete
        return PurgeResult(PurgeOutcome.deleted, 1024)

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _rearm_then_delete)

    stale = eviction_service.EvictionCandidate(
        request_id=season_request_id,
        media_type="tv",
        title="Rearm Delete",
        season=1,
        status="available",
        watched=True,
        last_viewed_at=_STALE,
        keep_forever=False,
        in_flight=False,
        library_path=str(season_dir),
        size_percent=1.0,
    )
    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=show_id,
        season_request_id=season_request_id,
        season_number=1,
        tmdb_id=812,
        size_bytes=1024,
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path / "tv")])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(812, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is not None  # the eviction finalized
    assert not season_dir.exists()  # genuinely deleted
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_request_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 812)))
            .scalars()
            .all()
        )
    assert season_row is not None
    assert season_row.status is RequestStatus.pending  # the re-grab proceeds
    # The finalize cleared the preserved breadcrumb (the file IS gone now), so a
    # later sweep never misreads this re-grabbing row as still reclaimable.
    assert season_row.library_path is None
    assert len(history) == 1
    assert history[0].event_type is DownloadHistoryEvent.evicted


# --------------------------------------------------------------------------- #
# Codex round-2 finding 1: crash resumability. The claim commits 'evicted'
# BEFORE the purge and the finalize clears the breadcrumb AFTER it, so
# 'evicted' + a non-NULL library_path is always a claimed-but-not-finalized
# eviction (a crash landed between the two). Sweeps only assemble 'available'
# rows, so without the resume pass such a row -- and its live file -- would be
# invisible to every later sweep forever. The tests seed that exact post-crash
# state directly and prove the next sweep recovers it, in BOTH directions.
# --------------------------------------------------------------------------- #


async def test_sweep_resumes_an_interrupted_movie_eviction_and_re_evicts(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Crash between claim-commit and purge, still under pressure: the next sweep
    restores the row to 'available' (file still on disk) and then re-decides the
    eviction FRESH through the normal claim -> purge path -- the interrupted
    purge is effectively resumed, the pressured disk actually gets relieved."""
    library_path = _movie_file(tmp_path, "Interrupted.mkv")
    request_id = await _movie(
        sessionmaker_,
        tmdb_id=600,
        title="Interrupted",
        library_path=library_path,
        status=RequestStatus.evicted,  # the committed claim the crash stranded
    )
    library = FakeLibrary(
        watch_states={(600, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=0.0,  # pressure still on -> the fresh decision re-evicts
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert [o.title for o in outcomes] == ["Interrupted"]
    assert not Path(library_path).exists()  # the interrupted delete finally ran
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted
        assert row.library_path is None  # finalized this time
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 600)))
            .scalars()
            .all()
        )
    assert [h.event_type for h in history] == [DownloadHistoryEvent.evicted]


async def test_sweep_restores_an_interrupted_movie_eviction_even_without_pressure(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Crash recovery never waits for disk pressure: below threshold (where the
    sweep evicts nothing) the resume pass still runs -- the stranded 'evicted'
    row over a live file goes back to 'available', and nothing is deleted (the
    pressure that justified the eviction is gone, so the decision is honestly
    re-made as 'keep')."""
    library_path = _movie_file(tmp_path, "Stranded.mkv")
    request_id = await _movie(
        sessionmaker_,
        tmdb_id=601,
        title="Stranded",
        library_path=library_path,
        status=RequestStatus.evicted,
    )
    library = FakeLibrary(
        watch_states={(601, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=101.0,  # unreachable -- no pressure, sweep evicts nothing
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(library_path).exists()  # nothing deleted without pressure
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available  # restored, re-evictable later
        assert row.library_path == library_path  # breadcrumb kept for that retry


async def test_sweep_finalizes_an_interrupted_movie_eviction_whose_file_is_gone(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Crash AFTER the purge but before the finalize: the file is gone but the
    history row, breadcrumb clear, and Plex refresh never happened. The resume
    pass finalizes -- never restores 'available' over nothing."""
    missing_path = str(tmp_path / "movies" / "Already Gone.mkv")  # never created
    request_id = await _movie(
        sessionmaker_,
        tmdb_id=602,
        title="Already Gone",
        library_path=missing_path,
        status=RequestStatus.evicted,
    )
    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=101.0,  # recovery must not need pressure
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted  # the eviction stands
        assert row.library_path is None  # finalized: never matched again
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 602)))
            .scalars()
            .all()
        )
    assert [h.event_type for h in history] == [DownloadHistoryEvent.evicted]
    assert history[0].message is not None and "finalized" in history[0].message
    # The Plex refresh the interrupted sweep never got to fire.
    assert (missing_path, "movie") in library.scan_calls


async def test_sweep_restores_an_interrupted_season_eviction_without_pressure(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The TV twin of the movie restore: a season claimed 'evicted' whose file
    still exists goes back to 'available' (parent rollup recomputed) on the next
    tv sweep, pressure or not."""
    s1_path = _movie_file(tmp_path, "Interrupted Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=610, title="Interrupted Show", seasons={1: s1_path}
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
        season_row.status = RequestStatus.evicted  # the stranded claim
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.evicted  # its rollup, as the claim left it
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(s1_path).exists()
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        show = await session.get(MediaRequest, show_id)
    assert season_row is not None and season_row.status is RequestStatus.available
    assert season_row.library_path == s1_path
    assert show is not None and show.status is RequestStatus.available  # rollup too


async def test_sweep_finalizes_an_interrupted_season_eviction_whose_file_is_gone(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The TV twin of the movie finalize: purge done, finalize crashed away --
    the season keeps 'evicted', its breadcrumb is cleared, the history row and
    Plex refresh land."""
    missing_path = str(tmp_path / "tv" / "Gone Show" / "Season 01")  # never created
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=611, title="Gone Show", seasons={1: missing_path}
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
        season_row.status = RequestStatus.evicted
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.evicted
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 611)))
            .scalars()
            .all()
        )
    assert season_row is not None
    assert season_row.status is RequestStatus.evicted  # the eviction stands
    assert season_row.library_path is None  # finalized
    assert [h.event_type for h in history] == [DownloadHistoryEvent.evicted]
    assert (missing_path, "tv") in library.scan_calls


async def test_interrupted_season_finalize_does_not_clear_same_path_replacement_import(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovered evicted-season finalize must not erase a same-row replacement
    import that committed the same deterministic season directory between the
    file-gone stat and breadcrumb clear."""
    missing_path = str(tmp_path / "tv" / "Same Path Finalize Race" / "Season 01")
    show_id = await _show_with_seasons(
        sessionmaker_,
        tmdb_id=612,
        title="Same Path Finalize Race",
        seasons={1: missing_path},
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
        season_row.status = RequestStatus.evicted
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.evicted
        await session.commit()
        season_id = season_row.id

    original_clear = SqlSeasonRequestRepository.clear_library_path_if_set

    async def _complete_same_path_import_before_clear(
        self: SqlSeasonRequestRepository,
        season_request_id: int,
        *,
        expected_path: str | None = None,
        expected_statuses: frozenset[str] | None = None,
    ) -> bool:
        assert expected_statuses is not None
        assert RequestStatus.completed.value not in expected_statuses
        async with sessionmaker_() as race_session:
            row = await race_session.get(SeasonRequest, season_request_id)
            assert row is not None
            row.status = RequestStatus.completed
            row.library_path = expected_path
            parent = await race_session.get(MediaRequest, show_id)
            assert parent is not None
            parent.status = RequestStatus.completed
            await race_session.commit()
        return await original_clear(
            self,
            season_request_id,
            expected_path=expected_path,
            expected_statuses=expected_statuses,
        )

    monkeypatch.setattr(
        SqlSeasonRequestRepository,
        "clear_library_path_if_set",
        _complete_same_path_import_before_clear,
    )

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 612)))
            .scalars()
            .all()
        )
    assert season_row is not None
    assert season_row.status is RequestStatus.completed
    assert season_row.library_path == missing_path
    assert history == []
    assert library.scan_calls == []


# --------------------------------------------------------------------------- #
# Codex round-2 finding 3: a restore (failed delete OR resumed crash) must not
# leave the in-window re-grab it made redundant standing -- the file never
# left, so a pre-grab re-request is cancelled (movie / sibling season) or
# folded back to 'available' (the same-row TV re-arm); anything that already
# grabbed is left to the reconciler / import dedup.
# --------------------------------------------------------------------------- #


async def test_failed_delete_restore_cancels_the_in_window_movie_regrab(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claim commits -> a re-request lands ('pending', per the stale-Plex guard)
    -> the purge then FAILS -> the old row is restored 'available'. Without
    reconciliation the live 'available' row and the active 'pending' re-grab now
    coexist for content that never left disk, and the app downloads a duplicate.
    The restore must cancel the pre-grab re-grab (with a history row)."""
    library_path = _movie_file(tmp_path, "Never Left.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=620, title="Never Left", library_path=library_path
    )
    # The in-window re-grab (as create_request's guard mints it mid-window) --
    # ``eviction_regrab=True`` because it is exactly the eviction guard's OWN
    # re-grab (issue #156's provenance marker), the shape this restore's
    # reconciliation must cancel.
    async with sessionmaker_() as session:
        regrab = MediaRequest(
            tmdb_id=620,
            media_type=MediaType.movie,
            title="Never Left",
            status=RequestStatus.pending,
            eviction_regrab=True,
        )
        session.add(regrab)
        await session.commit()
        regrab_id = regrab.id

    async def _erroring_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        return PurgeResult(PurgeOutcome.error, 0, "OSError")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _erroring_purge)

    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Never Left",
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
        media_request_id=request_id, tmdb_id=620, size_bytes=1024
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(620, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert Path(library_path).exists()  # nothing deleted
    async with sessionmaker_() as session:
        old_row = await session.get(MediaRequest, request_id)
        regrab_row = await session.get(MediaRequest, regrab_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 620)))
            .scalars()
            .all()
        )
    assert old_row is not None and old_row.status is RequestStatus.available  # restored
    # The redundant re-grab is cancelled, never left to download a duplicate.
    assert regrab_row is not None and regrab_row.status is RequestStatus.cancelled
    assert [h.event_type for h in history] == [DownloadHistoryEvent.cancelled]


async def test_failed_delete_restore_never_cancels_an_operator_forced_reacquire(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #156 regression: a deliberate #148 forced re-acquire (``eviction_
    regrab=False`` -- it explicitly bypasses the eviction guard that stamps the
    marker) is a pre-grab row for the SAME movie, in the SAME shape the restore's
    dedup used to cancel unconditionally. When THIS eviction's delete fails and
    it restores its own row to 'available', the operator's re-acquire must
    SURVIVE untouched -- it is not this eviction's own redundant re-grab, and
    silently cancelling it would vanish a request the operator explicitly made
    with no explanation the user could ever see."""
    library_path = _movie_file(tmp_path, "Ghost Movie.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=622, title="Ghost Movie", library_path=library_path
    )
    # The operator's forced re-acquire (request_service.create_request's
    # ``force=True`` path) -- pre-grab, but NEVER stamped ``eviction_regrab``
    # because it deliberately skips the ``latest_request_evicted`` guard.
    async with sessionmaker_() as session:
        reacquire = MediaRequest(
            tmdb_id=622,
            media_type=MediaType.movie,
            title="Ghost Movie",
            status=RequestStatus.pending,
            eviction_regrab=False,
        )
        session.add(reacquire)
        await session.commit()
        reacquire_id = reacquire.id

    async def _erroring_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        return PurgeResult(PurgeOutcome.error, 0, "OSError")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _erroring_purge)

    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Ghost Movie",
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
        media_request_id=request_id, tmdb_id=622, size_bytes=1024
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(622, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    assert Path(library_path).exists()  # nothing deleted
    async with sessionmaker_() as session:
        old_row = await session.get(MediaRequest, request_id)
        reacquire_row = await session.get(MediaRequest, reacquire_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 622)))
            .scalars()
            .all()
        )
    assert old_row is not None and old_row.status is RequestStatus.available  # restored
    # The operator's forced re-acquire is left completely untouched -- not this
    # eviction's own re-grab, so the dedup must never cancel it.
    assert reacquire_row is not None and reacquire_row.status is RequestStatus.pending
    assert history == []  # no cancellation recorded -- nothing was cancelled


async def test_failed_delete_restore_leaves_a_regrab_that_already_grabbed(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconciliation CAS is scoped to PRE-GRAB statuses only: a re-grab that
    already advanced to 'downloading' has a live torrent -- cancelling underneath
    it would orphan the download, so it is left to the reconciler/import dedup
    (the import simply re-places the file)."""
    library_path = _movie_file(tmp_path, "Already Grabbing.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=621, title="Already Grabbing", library_path=library_path
    )
    async with sessionmaker_() as session:
        regrab = MediaRequest(
            tmdb_id=621,
            media_type=MediaType.movie,
            title="Already Grabbing",
            status=RequestStatus.downloading,
        )
        session.add(regrab)
        await session.commit()
        regrab_id = regrab.id

    async def _erroring_purge(
        _fs: object, _path: str, *, hold_purge_registration: bool = False
    ) -> PurgeResult:
        return PurgeResult(PurgeOutcome.error, 0, "OSError")

    monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _erroring_purge)
    monkeypatch.setattr(eviction_service, "_still_evictable", _always_evictable)

    stale = eviction_service.EvictionCandidate(
        request_id=request_id,
        media_type="movie",
        title="Already Grabbing",
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
        media_request_id=request_id, tmdb_id=621, size_bytes=1024
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(621, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is None
    async with sessionmaker_() as session:
        old_row = await session.get(MediaRequest, request_id)
        regrab_row = await session.get(MediaRequest, regrab_id)
    assert old_row is not None and old_row.status is RequestStatus.available
    # The in-flight download is untouched -- reconciler/import dedup owns it.
    assert regrab_row is not None and regrab_row.status is RequestStatus.downloading


async def test_restore_folds_a_rearmed_tv_season_back_to_available(
    sessionmaker_: SessionMaker,
) -> None:
    """The mixed-show TV shape: the in-window re-request re-arms the SAME season
    row (ensure_seasons, evicted -> pending), so the restore's evicted->available
    CAS loses. The row is the season's only tracking record and its file never
    left -- it must be folded straight back to 'available' (never cancelled, and
    never left 'pending' to download a duplicate)."""
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=630, title="Rearmed Show", seasons={1: "/media/tv/Rearmed/S01"}
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
        season_row.status = RequestStatus.pending  # as the in-window re-arm left it
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.pending
        await session.commit()
        season_id = season_row.id

    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=show_id,
        season_request_id=season_id,
        season_number=1,
        tmdb_id=630,
        size_bytes=None,
    )
    async with sessionmaker_() as session:
        await eviction_service._restore_after_failed_delete(  # pyright: ignore[reportPrivateUsage]
            session, pending
        )

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        show = await session.get(MediaRequest, show_id)
    assert season_row is not None and season_row.status is RequestStatus.available
    assert show is not None and show.status is RequestStatus.available  # rollup follows


async def test_restore_cancels_the_sibling_season_regrab_under_a_newer_request(
    sessionmaker_: SessionMaker,
) -> None:
    """The wholly-evicted TV shape: the in-window re-request created a NEW
    MediaRequest tracking the same season ('pending'). Restoring the OLD season
    to 'available' makes that duplicate redundant -- it is CAS-cancelled and the
    new parent's rollup recomputed, with a history row (honesty over silence)."""
    old_show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=631, title="Whole Show", seasons={1: "/media/tv/Whole/S01"}
    )
    async with sessionmaker_() as session:
        old_season = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == old_show_id)
                )
            )
            .scalars()
            .one()
        )
        old_season.status = RequestStatus.evicted  # the committed claim
        old_show = await session.get(MediaRequest, old_show_id)
        assert old_show is not None
        old_show.status = RequestStatus.evicted  # rollup: wholly evicted
        # The in-window re-request: a NEW request for the same show + season.
        # ``eviction_regrab=True`` on the season -- exactly the eviction guard's
        # OWN re-grab (issue #156's provenance marker), the shape this restore's
        # sibling reconciliation must cancel.
        new_show = MediaRequest(
            tmdb_id=631,
            media_type=MediaType.tv,
            title="Whole Show",
            status=RequestStatus.pending,
        )
        session.add(new_show)
        await session.flush()
        new_season = SeasonRequest(
            media_request_id=new_show.id,
            season_number=1,
            status=RequestStatus.pending,
            eviction_regrab=True,
        )
        session.add(new_season)
        await session.commit()
        old_season_id = old_season.id
        new_show_id, new_season_id = new_show.id, new_season.id

    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=old_show_id,
        season_request_id=old_season_id,
        season_number=1,
        tmdb_id=631,
        size_bytes=None,
    )
    async with sessionmaker_() as session:
        await eviction_service._restore_after_failed_delete(  # pyright: ignore[reportPrivateUsage]
            session, pending
        )

    async with sessionmaker_() as session:
        old_season = await session.get(SeasonRequest, old_season_id)
        new_season = await session.get(SeasonRequest, new_season_id)
        new_show = await session.get(MediaRequest, new_show_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 631)))
            .scalars()
            .all()
        )
    assert old_season is not None and old_season.status is RequestStatus.available  # restored
    # The duplicate under the newer request is cancelled, its parent's rollup
    # recomputed off the cancelled season.
    assert new_season is not None and new_season.status is RequestStatus.cancelled
    assert new_show is not None and new_show.status is RequestStatus.cancelled
    assert [h.event_type for h in history] == [DownloadHistoryEvent.cancelled]


async def test_restore_never_cancels_a_sibling_season_that_is_not_its_own_regrab(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #156 regression, TV twin: a sibling season under a NEWER request that
    is NOT this eviction's own re-grab (``eviction_regrab=False`` -- e.g. an
    unrelated concurrently-tracked request for the same show/season that simply
    happens to be pre-grab right now) must survive the restore's sibling
    reconciliation untouched. Only a season THIS eviction's own guard re-armed
    is a redundant duplicate; anything else is left exactly as it is."""
    old_show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=632, title="Another Show", seasons={1: "/media/tv/Another/S01"}
    )
    async with sessionmaker_() as session:
        old_season = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == old_show_id)
                )
            )
            .scalars()
            .one()
        )
        old_season.status = RequestStatus.evicted  # the committed claim
        old_show = await session.get(MediaRequest, old_show_id)
        assert old_show is not None
        old_show.status = RequestStatus.evicted  # rollup: wholly evicted
        # A sibling row for the SAME (show, season) that is pre-grab for some
        # OTHER reason -- NOT this eviction's own re-grab.
        other_show = MediaRequest(
            tmdb_id=632,
            media_type=MediaType.tv,
            title="Another Show",
            status=RequestStatus.pending,
        )
        session.add(other_show)
        await session.flush()
        other_season = SeasonRequest(
            media_request_id=other_show.id,
            season_number=1,
            status=RequestStatus.pending,
            eviction_regrab=False,
        )
        session.add(other_season)
        await session.commit()
        old_season_id = old_season.id
        other_show_id, other_season_id = other_show.id, other_season.id

    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=old_show_id,
        season_request_id=old_season_id,
        season_number=1,
        tmdb_id=632,
        size_bytes=None,
    )
    async with sessionmaker_() as session:
        await eviction_service._restore_after_failed_delete(  # pyright: ignore[reportPrivateUsage]
            session, pending
        )

    async with sessionmaker_() as session:
        old_season = await session.get(SeasonRequest, old_season_id)
        other_season = await session.get(SeasonRequest, other_season_id)
        other_show = await session.get(MediaRequest, other_show_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 632)))
            .scalars()
            .all()
        )
    assert old_season is not None and old_season.status is RequestStatus.available  # restored
    # The unrelated sibling is left completely untouched -- not this eviction's
    # own re-grab, so the dedup must never cancel it.
    assert other_season is not None and other_season.status is RequestStatus.pending
    assert other_show is not None and other_show.status is RequestStatus.pending
    assert history == []  # no cancellation recorded -- nothing was cancelled


async def test_resume_restores_and_cancels_the_regrab_after_a_crash(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Findings 1 + 3 composed: crash mid-eviction (claim committed, file still
    on disk) AND an in-window re-request already landed 'pending'. The next
    sweep's resume restores the old row to 'available' AND cancels the now
    redundant re-grab -- converging to exactly one honest row over the live
    file, with nothing queued to download a duplicate."""
    library_path = _movie_file(tmp_path, "Crashed Mid Evict.mkv")
    request_id = await _movie(
        sessionmaker_,
        tmdb_id=640,
        title="Crashed Mid Evict",
        library_path=library_path,
        status=RequestStatus.evicted,  # the stranded claim
    )
    async with sessionmaker_() as session:
        regrab = MediaRequest(
            tmdb_id=640,
            media_type=MediaType.movie,
            title="Crashed Mid Evict",
            status=RequestStatus.pending,  # the in-window re-grab
            eviction_regrab=True,  # the eviction guard's OWN re-grab (issue #156)
        )
        session.add(regrab)
        await session.commit()
        regrab_id = regrab.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=101.0,  # no pressure: recovery only, no re-evict
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(library_path).exists()
    async with sessionmaker_() as session:
        old_row = await session.get(MediaRequest, request_id)
        regrab_row = await session.get(MediaRequest, regrab_id)
    assert old_row is not None and old_row.status is RequestStatus.available
    assert regrab_row is not None and regrab_row.status is RequestStatus.cancelled


# --------------------------------------------------------------------------- #
# Codex round-2 findings 1 + 2 (under-stamping): the eviction guard must stamp
# ``eviction_regrab`` on ANY fresh non-force row it creates whenever the
# newest tracked history is 'evicted' -- regardless of what THIS call's own
# Plex probe reported (presence-proven, absent, or erroring) -- or a genuine
# in-window regrab is invisible to the restore's redundant-regrab dedup.
# --------------------------------------------------------------------------- #


async def test_create_request_regrab_stamped_when_plex_errors_is_cancelled_by_restore(
    sessionmaker_: SessionMaker,
) -> None:
    """Composes finding 1 end-to-end: ``create_request`` stamps
    ``eviction_regrab=True`` on a fresh movie row even though THIS call's own
    Plex probe ERRORED (never proving presence) -- and once stamped, a
    DIFFERENT eviction's failed-delete restore correctly recognizes + cancels
    it as its own redundant duplicate. Before the fix the row was created
    UNMARKED (the guard never ran outside the ``force or _already_in_library``
    branch), so the restore's dedup skipped it and left both the restored file
    AND this redundant re-download standing."""
    request_id = await _movie(
        sessionmaker_,
        tmdb_id=900,
        title="Erroring Plex Movie",
        library_path=None,
        status=RequestStatus.evicted,  # the claim, mid-window
    )

    tmdb = FakeTmdb(
        movies={900: MovieMetadata(tmdb_id=900, title="Erroring Plex Movie", year=2022)}
    )
    library = FakeLibrary(raises=PlexLibraryError("plex is down"))
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(
            session, tmdb, tmdb_id=900, media_type="movie", library=library
        )
    assert fresh.id != request_id
    assert fresh.status == RequestStatus.pending.value
    assert fresh.eviction_regrab is True  # finding-1 fix

    # A DIFFERENT eviction's failed-delete restore (``_restore_after_failed_
    # delete``'s reconciliation) must recognize this fresh row as its own
    # redundant re-grab and cancel it.
    pending = eviction_service._MoviePending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=request_id, tmdb_id=900, size_bytes=None
    )
    async with sessionmaker_() as session:
        await eviction_service._restore_after_failed_delete(  # pyright: ignore[reportPrivateUsage]
            session, pending
        )

    async with sessionmaker_() as session:
        old_row = await session.get(MediaRequest, request_id)
        regrab_row = await session.get(MediaRequest, fresh.id)
    assert old_row is not None and old_row.status is RequestStatus.available  # restored
    assert regrab_row is not None and regrab_row.status is RequestStatus.cancelled


async def test_ensure_seasons_regrab_stamped_when_plex_crawl_errors_is_cancelled_by_restore(
    sessionmaker_: SessionMaker,
) -> None:
    """The TV twin of the test above (finding 2): ``ensure_seasons`` stamps a
    fresh season's ``eviction_regrab=True`` even though THIS call's own Plex
    crawl (``_present_seasons``) ERRORED and returned an empty set -- never
    proving the season present -- because the season is still in
    ``evicted_seasons`` (the DB-only signal). Before the fix,
    ``evicted_regrab_seasons`` was only ever ``present & evicted_seasons``, so
    an erroring crawl (empty ``present``) left the fresh season unmarked, and
    the restore's sibling dedup would have skipped the exact duplicate it
    exists to catch."""
    old_show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=901, title="Erroring Plex Show", seasons={1: None}
    )
    async with sessionmaker_() as session:
        old_season = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == old_show_id)
                )
            )
            .scalars()
            .one()
        )
        old_season.status = RequestStatus.evicted  # the committed claim
        old_show = await session.get(MediaRequest, old_show_id)
        assert old_show is not None
        old_show.status = RequestStatus.evicted  # rollup: wholly evicted
        await session.commit()
        old_season_id = old_season.id

    # A fresh show tracking the same season, created while Plex's season crawl
    # ERRORS (mirrors ``request_service.create_request``'s wholly-evicted-show
    # re-request path calling straight into ``ensure_seasons``).
    async with sessionmaker_() as session:
        new_show = MediaRequest(
            tmdb_id=901,
            media_type=MediaType.tv,
            title="Erroring Plex Show",
            status=RequestStatus.pending,
        )
        session.add(new_show)
        await session.commit()
        new_show_id = new_show.id

    library = FakeLibrary(raises=PlexLibraryError("plex is down"))
    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, library, media_request_id=new_show_id, tmdb_id=901, seasons=[1]
        )
        await session.commit()

    assert len(records) == 1
    assert records[0].status == RequestStatus.pending.value
    assert records[0].eviction_regrab is True  # finding-2 fix
    new_season_id = records[0].id

    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=old_show_id,
        season_request_id=old_season_id,
        season_number=1,
        tmdb_id=901,
        size_bytes=None,
    )
    async with sessionmaker_() as session:
        await eviction_service._restore_after_failed_delete(  # pyright: ignore[reportPrivateUsage]
            session, pending
        )

    async with sessionmaker_() as session:
        old_season = await session.get(SeasonRequest, old_season_id)
        new_season = await session.get(SeasonRequest, new_season_id)
        new_show = await session.get(MediaRequest, new_show_id)
    assert old_season is not None and old_season.status is RequestStatus.available  # restored
    assert new_season is not None and new_season.status is RequestStatus.cancelled
    assert new_show is not None and new_show.status is RequestStatus.cancelled


# --------------------------------------------------------------------------- #
# Codex round-2 findings 3 + 4 (stale markers): ``eviction_regrab`` must be
# retired the moment a row stops being "some eviction's own in-flight regrab"
# -- confirmed available, or re-armed by an operator (report-issue) for a
# brand-new search -- or a LATER, UNRELATED eviction's restore can cancel a row
# that has nothing to do with it anymore.
# --------------------------------------------------------------------------- #


async def test_operator_rearmed_former_regrab_movie_survives_an_unrelated_restore(
    sessionmaker_: SessionMaker,
) -> None:
    """A movie row that WAS some eviction's own regrab, since re-armed by the
    operator (report-issue) for a brand-new search, must survive a DIFFERENT
    eviction's failed-delete restore untouched -- the marker was retired the
    moment ``reset_for_research`` re-armed it (finding 3), so the restore's
    dedup no longer recognizes it as a redundant duplicate of ITS OWN regrab.
    Before the fix the stale marker would have let the restore cancel the
    operator's live re-search."""
    # The row this test's (unrelated) eviction restores; only its id/tmdb_id
    # matter to ``_cancel_redundant_movie_regrabs`` (it excludes this id).
    restored_id = await _movie(sessionmaker_, tmdb_id=902, title="Rearmed Movie", library_path=None)

    # A PAST eviction regrab for the SAME movie, under a different request.
    async with sessionmaker_() as session:
        rearmed = MediaRequest(
            tmdb_id=902,
            media_type=MediaType.movie,
            title="Rearmed Movie",
            status=RequestStatus.pending,
            eviction_regrab=True,  # this row WAS some eviction's own regrab
        )
        session.add(rearmed)
        await session.commit()
        rearmed_id = rearmed.id

    # report-issue's re-arm verb -- the fix clears the marker here.
    async with sessionmaker_() as session:
        await SqlRequestRepository(session).reset_for_research(rearmed_id)
        await session.commit()

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, rearmed_id)
        assert row is not None
        assert row.status is RequestStatus.searching
        assert row.eviction_regrab is False  # cleared by the fix (finding 3)

    # A DIFFERENT eviction's failed-delete restore now runs its redundant-regrab
    # dedup against this movie.
    pending = eviction_service._MoviePending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=restored_id, tmdb_id=902, size_bytes=None
    )
    async with sessionmaker_() as session:
        await eviction_service._cancel_redundant_movie_regrabs(  # pyright: ignore[reportPrivateUsage]
            session, pending
        )
        await session.commit()

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, rearmed_id)
    assert row is not None
    assert row.status is RequestStatus.searching  # NOT cancelled


async def test_operator_rearmed_former_regrab_season_survives_an_unrelated_restore(
    sessionmaker_: SessionMaker,
) -> None:
    """The TV twin of the test above: a season that WAS some eviction's own
    regrab, since re-armed by report-issue for a brand-new search, must survive
    a DIFFERENT eviction's failed-delete restore untouched."""
    restored_show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=903, title="Rearmed Show", seasons={1: None}
    )
    async with sessionmaker_() as session:
        restored_season = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == restored_show_id)
                )
            )
            .scalars()
            .one()
        )
        restored_season_id = restored_season.id

    # A PAST eviction regrab for the SAME show/season, under a different request.
    async with sessionmaker_() as session:
        rearmed_show = MediaRequest(
            tmdb_id=903,
            media_type=MediaType.tv,
            title="Rearmed Show",
            status=RequestStatus.pending,
        )
        session.add(rearmed_show)
        await session.flush()
        rearmed_season = SeasonRequest(
            media_request_id=rearmed_show.id,
            season_number=1,
            status=RequestStatus.pending,
            eviction_regrab=True,  # this season WAS some eviction's own regrab
        )
        session.add(rearmed_season)
        await session.commit()
        rearmed_show_id, rearmed_season_id = rearmed_show.id, rearmed_season.id

    # report-issue's re-arm verb -- the fix clears the marker here.
    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=rearmed_show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, rearmed_season_id)
        assert season is not None
        assert season.status is RequestStatus.searching
        assert season.eviction_regrab is False  # cleared by the fix (finding 3)

    # A DIFFERENT eviction's failed-delete restore now runs its redundant-regrab
    # dedup against this show's season.
    pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
        media_request_id=restored_show_id,
        season_request_id=restored_season_id,
        season_number=1,
        tmdb_id=903,
        size_bytes=None,
    )
    async with sessionmaker_() as session:
        await eviction_service._cancel_redundant_season_regrabs(  # pyright: ignore[reportPrivateUsage]
            session, pending
        )
        await session.commit()

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, rearmed_season_id)
    assert season is not None
    assert season.status is RequestStatus.searching  # NOT cancelled


# --------------------------------------------------------------------------- #
# Codex round-3: mechanism REMOVED -- sweeps are serialized in-process (a
# module latch), which deletes the overlapping-sweep permutation class the
# per-row registry used to (incompletely) defend, so the registry is gone.
# Recovery is keyed on the BREADCRUMB (not only the 'evicted' status), and a
# breadcrumb whose path another live row claims is released, never restored.
# --------------------------------------------------------------------------- #


async def test_second_sweep_invocation_no_ops_while_one_is_running(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sweeps are serialized: a second run_eviction_sweep entered while one is in
    flight (the manual POST /ops/evict button landing mid-tick) no-ops with a log
    line and returns [] -- exactly one sweep does the work, one eviction, one
    history row. This serialization is what deleted the overlapping-sweep race
    class (double-claim, recovery-vs-mid-purge) outright."""
    library_path = _movie_file(tmp_path, "Only Once.mkv")
    await _movie(sessionmaker_, tmdb_id=650, title="Only Once", library_path=library_path)
    library = FakeLibrary(
        watch_states={(650, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async def _sweep() -> list[eviction_service.EvictionOutcome]:
        async with sessionmaker_() as session:
            return await eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(tmp_path),
                threshold_pct=0.0,
                target_pct=0.0,
                grace_days=_GRACE_DAYS,
            )

    with caplog.at_level(logging.INFO, logger="plex_manager.services.eviction_service"):
        first, second = await asyncio.gather(_sweep(), _sweep())

    # Exactly ONE invocation swept; the other no-op'd (never a double eviction).
    outcome_lists = [o for o in (first, second) if o]
    assert len(outcome_lists) == 1
    assert [o.title for o in outcome_lists[0]] == ["Only Once"]
    assert "already in progress" in caplog.text
    assert not Path(library_path).exists()
    async with sessionmaker_() as session:
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 650)))
            .scalars()
            .all()
        )
    assert len(history) == 1  # one sweep, one eviction record


async def test_sweep_recovers_a_rearmed_pending_season_when_its_file_still_exists(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Round-3 finding 2: crash window, then a re-request re-armed the claimed
    season to 'pending' (same row, breadcrumb still set) BEFORE recovery ran --
    the 'evicted' enumeration alone would miss it and the re-grab would download
    a duplicate of a file that never left. Recovery keys on the BREADCRUMB:
    'pending' + breadcrumb is uniquely that re-arm shape (report-issue's
    keep-the-breadcrumb re-arm sets 'searching'), so the season is folded back
    to 'available'."""
    s1_path = _movie_file(tmp_path, "Rearmed Crash Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=651, title="Rearmed Crash Show", seasons={1: s1_path}
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
        season_row.status = RequestStatus.pending  # the crash-window re-arm
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.pending
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,  # recovery must not need pressure
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(s1_path).exists()  # the file never left, and nothing deleted it
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        show = await session.get(MediaRequest, show_id)
    assert season_row is not None
    assert season_row.status is RequestStatus.available  # folded back, no duplicate download
    assert season_row.library_path == s1_path  # still the honest breadcrumb
    assert show is not None and show.status is RequestStatus.available


async def test_sweep_releases_the_breadcrumb_of_a_rearmed_season_whose_file_is_gone(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The other half of the re-armed shape: the interrupted purge actually
    completed (file gone) before the crash, so the re-grab is legitimate -- the
    season stays 'pending', the stale breadcrumb is released, and the eviction
    gets the history row + Plex refresh the crash swallowed."""
    missing_path = str(tmp_path / "tv" / "Rearmed Gone" / "Season 01")  # never created
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=652, title="Rearmed Gone", seasons={1: missing_path}
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
        season_row.status = RequestStatus.pending
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.pending
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 652)))
            .scalars()
            .all()
        )
    assert season_row is not None
    assert season_row.status is RequestStatus.pending  # the re-grab proceeds
    assert season_row.library_path is None  # stale breadcrumb released
    assert [h.event_type for h in history] == [DownloadHistoryEvent.evicted]
    assert (missing_path, "tv") in library.scan_calls


async def test_resume_releases_a_legacy_movie_breadcrumb_when_a_newer_row_owns_the_path(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Round-3 finding 3: a LEGACY eviction (breadcrumb never cleared by the old
    flow) whose media was later re-imported to the SAME path under a newer row.
    The file exists, but restoring the legacy row would put two rows over one
    file -- and a later sweep evicting either would delete the path out from
    under the current owner. Recovery must recognize finalized-not-interrupted:
    release the breadcrumb, restore nothing, write no duplicate history."""
    shared_path = _movie_file(tmp_path, "Reimported.mkv")
    legacy_id = await _movie(
        sessionmaker_,
        tmdb_id=653,
        title="Reimported",
        library_path=shared_path,
        status=RequestStatus.evicted,  # the legacy eviction, breadcrumb never cleared
    )
    current_id = await _movie(  # the newer re-import that owns the path today
        sessionmaker_, tmdb_id=653, title="Reimported", library_path=shared_path
    )

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(shared_path).exists()  # the current owner's file is untouched
    async with sessionmaker_() as session:
        legacy = await session.get(MediaRequest, legacy_id)
        current = await session.get(MediaRequest, current_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 653)))
            .scalars()
            .all()
        )
    assert legacy is not None
    assert legacy.status is RequestStatus.evicted  # NOT restored over the owner's file
    assert legacy.library_path is None  # breadcrumb released -- never matched again
    assert current is not None
    assert current.status is RequestStatus.available  # the owner is untouched
    assert current.library_path == shared_path
    assert history == []  # nothing was evicted NOW -- no duplicate record


async def test_resume_releases_a_legacy_season_breadcrumb_when_a_newer_row_owns_the_path(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The TV twin of the legacy superseded-path release."""
    shared_path = _movie_file(tmp_path, "Reimported Show S01.mkv")
    old_show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=654, title="Reimported Show", seasons={1: shared_path}
    )
    async with sessionmaker_() as session:
        old_season = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == old_show_id)
                )
            )
            .scalars()
            .one()
        )
        old_season.status = RequestStatus.evicted  # legacy eviction, breadcrumb kept
        old_show = await session.get(MediaRequest, old_show_id)
        assert old_show is not None
        old_show.status = RequestStatus.evicted
        await session.commit()
        old_season_id = old_season.id
    # The newer request whose re-import owns the path today.
    new_show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=654, title="Reimported Show", seasons={1: shared_path}
    )

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert Path(shared_path).exists()
    async with sessionmaker_() as session:
        old_season = await session.get(SeasonRequest, old_season_id)
        new_seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == new_show_id)
                )
            )
            .scalars()
            .all()
        )
    assert old_season is not None
    assert old_season.status is RequestStatus.evicted  # not restored
    assert old_season.library_path is None  # breadcrumb released
    assert len(new_seasons) == 1
    assert new_seasons[0].status is RequestStatus.available  # the owner untouched
    assert new_seasons[0].library_path == shared_path


# --------------------------------------------------------------------------- #
# Issue #155: the shared-breadcrumb-twins guard, in the NORMAL sweep (not just
# crash recovery). Two 'available' rows can legitimately share one exact
# library_path (remove-then-reacquire's pre-existing leftover, or the #148
# force-reacquire shape: the old row is left untouched while the new row's
# import stamps the same deterministic path). Without this guard, a normal
# pressure sweep evicting one twin deletes the file out from under the other,
# which then reads dishonestly 'available' until a later sweep self-heals it.
# --------------------------------------------------------------------------- #


async def test_normal_sweep_never_deletes_a_movie_path_another_available_row_still_claims(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Two 'available' movie rows sharing one library_path (the shared-breadcrumb
    twins shape) + a pressure sweep that would otherwise evict the stale twin:
    the sweep must NOT delete the shared path while the sibling row still claims
    it. The claimed row is restored 'available' (exactly like a purge-refused
    delete) rather than finalizing a delete that would orphan its sibling."""
    shared_path = _movie_file(tmp_path, "Shared Breadcrumb.mkv")
    stale_id = await _movie(
        sessionmaker_, tmdb_id=670, title="Shared Breadcrumb", library_path=shared_path
    )
    fresh_id = await _movie(
        sessionmaker_, tmdb_id=670, title="Shared Breadcrumb", library_path=shared_path
    )

    library = FakeLibrary(
        watch_states={(670, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
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

    # Nothing was actually evicted -- both candidates saw a live sibling claim
    # and stood down rather than deleting the shared file.
    assert outcomes == []
    assert Path(shared_path).exists()
    async with sessionmaker_() as session:
        stale_row = await session.get(MediaRequest, stale_id)
        fresh_row = await session.get(MediaRequest, fresh_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 670)))
            .scalars()
            .all()
        )
    assert stale_row is not None and stale_row.status is RequestStatus.available
    assert stale_row.library_path == shared_path
    assert fresh_row is not None and fresh_row.status is RequestStatus.available
    assert fresh_row.library_path == shared_path
    assert history == []  # nothing was evicted -- no eviction history recorded


async def test_normal_sweep_never_deletes_a_season_path_another_available_row_still_claims(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The TV twin: two 'available' season rows (under DIFFERENT MediaRequest
    parents, the wholly-evicted-then-reacquired shape) sharing one exact
    library_path. A pressure sweep must not delete the shared season file out
    from under the sibling that still claims it."""
    shared_path = _movie_file(tmp_path, "Shared Show S01.mkv")
    stale_show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=671, title="Shared Show", seasons={1: shared_path}
    )
    fresh_show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=671, title="Shared Show", seasons={1: shared_path}
    )

    library = FakeLibrary(
        watch_states={(671, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
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
    assert Path(shared_path).exists()
    async with sessionmaker_() as session:
        stale_seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == stale_show_id)
                )
            )
            .scalars()
            .all()
        )
        fresh_seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == fresh_show_id)
                )
            )
            .scalars()
            .all()
        )
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 671)))
            .scalars()
            .all()
        )
    assert len(stale_seasons) == 1 and stale_seasons[0].status is RequestStatus.available
    assert stale_seasons[0].library_path == shared_path
    assert len(fresh_seasons) == 1 and fresh_seasons[0].status is RequestStatus.available
    assert fresh_seasons[0].library_path == shared_path
    assert history == []


# --------------------------------------------------------------------------- #
# Codex round-4 finding 1: recovery covers EVERY pre-grab breadcrumb status.
# A crash-window re-arm lands on 'pending', but auto-grab can promote it to
# 'searching' and park it 'no_acceptable_release' before the next sweep; a
# pending-only enumeration missed those, stranding the file (invisible to
# candidate assembly) behind a dishonest "nothing found".
# --------------------------------------------------------------------------- #


async def test_sweep_recovers_a_rearmed_season_promoted_to_searching(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The re-arm was promoted 'pending' -> 'searching' by auto-grab before the
    sweep ran: recovery must still fold it back to 'available' off the
    breadcrumb (per-status CAS from 'searching')."""
    s1_path = _movie_file(tmp_path, "Promoted Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=660, title="Promoted Show", seasons={1: s1_path}
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
        season_row.status = RequestStatus.searching  # promoted by auto-grab
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.searching
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(s1_path).exists()
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        show = await session.get(MediaRequest, show_id)
    assert season_row is not None and season_row.status is RequestStatus.available
    assert season_row.library_path == s1_path
    assert show is not None and show.status is RequestStatus.available


async def test_sweep_recovers_a_rearmed_season_parked_no_acceptable_release(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Codex's exact round-4 scenario: the re-arm was searched and PARKED
    ('no_acceptable_release') before the sweep ran. Without breadth the parked
    breadcrumb-bearing row is never restored -- the file stays on disk forever
    (parked rows are not eviction candidates) behind a dishonest duplicate
    "nothing found". Recovery folds it back to 'available'."""
    s1_path = _movie_file(tmp_path, "Parked Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=661, title="Parked Show", seasons={1: s1_path}
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
        season_row.status = RequestStatus.no_acceptable_release  # searched + parked
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.no_acceptable_release
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(s1_path).exists()  # never deleted -- and now reclaimable again
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        show = await session.get(MediaRequest, show_id)
    assert season_row is not None and season_row.status is RequestStatus.available
    assert show is not None and show.status is RequestStatus.available


async def test_sweep_releases_the_breadcrumb_of_a_parked_rearmed_season_whose_file_is_gone(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """File-gone half for the broadened statuses: the parked search over a
    truly-gone file is legitimate -- the row stays parked, the stale breadcrumb
    is released, and the missing history/Plex refresh land."""
    missing_path = str(tmp_path / "tv" / "Parked Gone" / "Season 01")  # never created
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=662, title="Parked Gone", seasons={1: missing_path}
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
        season_row.status = RequestStatus.no_acceptable_release
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.no_acceptable_release
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 662)))
            .scalars()
            .all()
        )
    assert season_row is not None
    assert season_row.status is RequestStatus.no_acceptable_release  # the park stands
    assert season_row.library_path is None  # stale breadcrumb released
    assert [h.event_type for h in history] == [DownloadHistoryEvent.evicted]
    assert (missing_path, "tv") in library.scan_calls


# --------------------------------------------------------------------------- #
# Codex round-7 findings 2 + 3.
# --------------------------------------------------------------------------- #


async def test_rearmed_recovery_never_wipes_a_replacement_imports_fresh_breadcrumb(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 finding 2: the file-gone breadcrumb clear is VALUE-predicated on
    the exact stale path recovery observed. A replacement import can commit
    between recovery's stat and the clear, stamping a FRESH breadcrumb (and
    fresh content) onto the very same row -- an unconditional clear would wipe
    it, leaving a playing season with no eviction/report handle. Simulated by a
    stale enumeration read: the DB row already carries the import's fresh
    breadcrumb + 'completed', while recovery observed the pre-import
    'pending' + stale-path snapshot. The mismatch must leave the row entirely
    untouched (fresh path kept, status kept, no history)."""
    stale_path = str(tmp_path / "tv" / "Restamped" / "Season 01")  # gone (never created)
    fresh_path = _movie_file(tmp_path, "Restamped S01 fresh.mkv")  # the import's file
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=670, title="Restamped", seasons={1: fresh_path}
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
        season_row.status = RequestStatus.completed  # the replacement import landed
        await session.commit()
        season_id = season_row.id

    real_list_by_status = SqlSeasonRequestRepository.list_by_status

    async def stale_list_by_status(
        self: SqlSeasonRequestRepository, status: str | None = None
    ) -> list[SeasonRequestRecord]:
        # Serve recovery the PRE-IMPORT snapshot it would have read moments
        # before the import committed: 'pending' + the stale breadcrumb.
        if status == RequestStatus.pending.value:
            return [
                SeasonRequestRecord(
                    id=season_id,
                    media_request_id=show_id,
                    season_number=1,
                    status=RequestStatus.pending.value,
                    tmdb_id=670,
                    library_path=stale_path,
                )
            ]
        return await real_list_by_status(self, status)

    monkeypatch.setattr(SqlSeasonRequestRepository, "list_by_status", stale_list_by_status)

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 670)))
            .scalars()
            .all()
        )
    assert season_row is not None
    assert season_row.library_path == fresh_path  # the import's fresh handle SURVIVES
    assert season_row.status is RequestStatus.completed  # row untouched
    assert history == []  # no bogus finalize was recorded


async def test_finalize_flips_a_cancelled_rearm_to_evicted_and_the_guard_holds(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-7 finding 3 (disk-truth-over-intent): the claimed season is
    re-armed and CANCELLED while the purge is deleting, so the finalize finds
    the row 'cancelled' -- which evicted_seasons rightly ignores, so nothing
    would be subtracted while Plex is stale and a re-request could mint
    'available' over the just-deleted file. The finalize must flip
    cancelled -> evicted (the cancel only aborted the re-grab INTENT; the file
    is GENUINELY gone), after which a subsequent re-request mints 'pending',
    never 'available'."""
    library_path = str(tmp_path / "tv" / "Cancelled Mid Purge" / "Season 01")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=680, title="Cancelled Mid Purge", seasons={1: library_path}
    )
    async with sessionmaker_() as session_seed:
        season_row = (
            (
                await session_seed.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
                )
            )
            .scalars()
            .one()
        )
        season_id = season_row.id

    async with sessionmaker_() as session:
        # The purge fake deletes "successfully" while the re-arm + user cancel
        # land on the row -- compressed to the end state the finalize then sees.
        async def _cancelling_purge(
            _fs: object, _path: str, *, hold_purge_registration: bool = False
        ) -> PurgeResult:
            row = await session.get(SeasonRequest, season_id)
            assert row is not None
            row.status = RequestStatus.cancelled
            await session.commit()
            return PurgeResult(PurgeOutcome.deleted, 0)

        monkeypatch.setattr(eviction_service.purge_service, "purge_library_path", _cancelling_purge)

        stale = eviction_service.EvictionCandidate(
            request_id=season_id,
            media_type="tv",
            title="Cancelled Mid Purge",
            season=1,
            status="available",
            watched=True,
            last_viewed_at=_STALE,
            keep_forever=False,
            in_flight=False,
            library_path=library_path,
            size_percent=1.0,
        )
        pending = eviction_service._SeasonPending(  # pyright: ignore[reportPrivateUsage]
            media_request_id=show_id,
            season_request_id=season_id,
            season_number=1,
            tmdb_id=680,
            size_bytes=1024,
        )
        fs = LocalFileSystem(library_roots=[str(tmp_path)])
        outcome = await eviction_service._evict_one(  # pyright: ignore[reportPrivateUsage]
            session=session,
            fs=fs,
            library=FakeLibrary(
                watch_states={(680, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
            ),
            candidate=stale,
            pending=pending,
            grace_cutoff=_GRACE_CUTOFF,
        )

    assert outcome is not None  # the eviction itself completed
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 680)))
            .scalars()
            .all()
        )
    assert season_row is not None
    # Disk truth over intent: never left 'cancelled' (invisible to the guard).
    assert season_row.status is RequestStatus.evicted
    assert season_row.library_path is None  # finalized
    assert [h.event_type for h in history] == [DownloadHistoryEvent.evicted]

    # The full circle: a subsequent re-request during the stale-Plex window must
    # mint 'pending' (evicted_seasons subtracts the flipped season), never an
    # 'available' row over the just-deleted file.
    tmdb = FakeTmdb(
        shows={680: TvMetadata(tmdb_id=680, title="Cancelled Mid Purge", year=2020, season_count=1)}
    )
    plex_stale = FakeLibrary(available_tv_seasons={680: frozenset({1})})
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(
            session, tmdb, tmdb_id=680, media_type="tv", seasons=[1], library=plex_stale
        )
    assert fresh.id != show_id
    assert fresh.status == RequestStatus.pending.value  # re-grabs, never 'available'


# --------------------------------------------------------------------------- #
# Codex round-8 finding 1: recovery also covers breadcrumb-bearing CANCELLED
# seasons (re-arm -> cancel -> crash before the finalize). 'cancelled' is
# rightly invisible to evicted_seasons, so without this the stale Plex window
# could mint 'available' over a deleted file -- or strand a live file behind a
# cancelled row no sweep could ever reclaim.
# --------------------------------------------------------------------------- #


async def test_sweep_flips_a_cancelled_rearm_with_the_file_gone_to_evicted(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Cancelled + stale breadcrumb + file GONE (the purge completed, then the
    crash ate the finalize): recovery applies the disk-truth flip -- the file is
    genuinely gone, the cancel only aborted the re-grab intent -- so the row
    lands 'evicted', the guard subtracts it again, and a re-request in the
    stale-Plex window mints 'pending', never 'available'."""
    missing_path = str(tmp_path / "tv" / "Cancelled Gone" / "Season 01")  # never created
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=690, title="Cancelled Gone", seasons={1: missing_path}
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
        season_row.status = RequestStatus.cancelled  # re-arm -> cancel -> crash
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.cancelled
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,  # recovery never waits for pressure
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 690)))
            .scalars()
            .all()
        )
    assert season_row is not None
    assert season_row.status is RequestStatus.evicted  # disk truth over intent
    assert season_row.library_path is None  # finalized
    assert [h.event_type for h in history] == [DownloadHistoryEvent.evicted]
    assert (missing_path, "tv") in library.scan_calls

    # The guard holds: a re-request while Plex still lists the season re-grabs.
    tmdb = FakeTmdb(
        shows={690: TvMetadata(tmdb_id=690, title="Cancelled Gone", year=2020, season_count=1)}
    )
    plex_stale = FakeLibrary(available_tv_seasons={690: frozenset({1})})
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(
            session, tmdb, tmdb_id=690, media_type="tv", seasons=[1], library=plex_stale
        )
    assert fresh.status == RequestStatus.pending.value  # never 'available'


async def test_rearmed_file_gone_recovery_does_not_clear_same_path_replacement_import(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-armed pending-season recovery must not erase a replacement import's
    fresh breadcrumb when that import commits the same deterministic season path
    between the file-gone stat and breadcrumb clear."""
    missing_path = str(tmp_path / "tv" / "Rearmed Same Path Race" / "Season 01")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=692, title="Rearmed Same Path Race", seasons={1: missing_path}
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
        season_row.status = RequestStatus.pending
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.pending
        await session.commit()
        season_id = season_row.id

    original_clear = SqlSeasonRequestRepository.clear_library_path_if_set

    async def _complete_same_path_import_before_clear(
        self: SqlSeasonRequestRepository,
        season_request_id: int,
        *,
        expected_path: str | None = None,
        expected_statuses: frozenset[str] | None = None,
    ) -> bool:
        assert expected_statuses is not None
        assert RequestStatus.completed.value not in expected_statuses
        async with sessionmaker_() as race_session:
            row = await race_session.get(SeasonRequest, season_request_id)
            assert row is not None
            row.status = RequestStatus.completed
            row.library_path = expected_path
            parent = await race_session.get(MediaRequest, show_id)
            assert parent is not None
            parent.status = RequestStatus.completed
            await race_session.commit()
        return await original_clear(
            self,
            season_request_id,
            expected_path=expected_path,
            expected_statuses=expected_statuses,
        )

    monkeypatch.setattr(
        SqlSeasonRequestRepository,
        "clear_library_path_if_set",
        _complete_same_path_import_before_clear,
    )

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        history = (
            (await session.execute(select(DownloadHistory).where(DownloadHistory.tmdb_id == 692)))
            .scalars()
            .all()
        )
    assert season_row is not None
    assert season_row.status is RequestStatus.completed
    assert season_row.library_path == missing_path
    assert history == []
    assert library.scan_calls == []


async def test_sweep_folds_a_cancelled_rearm_with_the_file_present_to_available(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """Cancelled + breadcrumb + file PRESENT (the purge never actually deleted
    anything before the crash): the aborted re-grab left a LIVE file, so disk
    truth reads 'available' -- folded back, file intact, breadcrumb kept, and
    the season is evictable/re-reportable again instead of stranded behind a
    cancelled row no sweep could reclaim."""
    s1_path = _movie_file(tmp_path, "Cancelled Alive S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=691, title="Cancelled Alive", seasons={1: s1_path}
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
        season_row.status = RequestStatus.cancelled
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.status = RequestStatus.cancelled
        await session.commit()
        season_id = season_row.id

    library = FakeLibrary()
    fs = LocalFileSystem(library_roots=[str(tmp_path)])
    async with sessionmaker_() as session:
        outcomes = await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="tv",
            root_path=str(tmp_path),
            threshold_pct=101.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    assert outcomes == []
    assert Path(s1_path).exists()  # the live file is untouched
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        show = await session.get(MediaRequest, show_id)
    assert season_row is not None
    assert season_row.status is RequestStatus.available  # folded: disk truth
    assert season_row.library_path == s1_path  # handle kept for eviction/report
    assert show is not None and show.status is RequestStatus.available


# --------------------------------------------------------------------------- #
# Issues #207/#209: path-correlated watch state, re-read immediately before
# the claim. #207 lives mostly in the adapter (tests/adapters/plex/
# test_plex_library.py); the tests below cover the SERVICE-side wiring (the
# path is actually threaded through) plus the FakeLibrary-modeled behavior a
# path-correlated verdict produces, and #209's pre-claim re-read in full.
# --------------------------------------------------------------------------- #


async def test_eviction_threads_library_path_into_watch_state(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Threaded Movie.mkv")
    await _movie(sessionmaker_, tmdb_id=700, title="Threaded Movie", library_path=library_path)
    library = FakeLibrary(
        watch_states={(700, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(tmp_path)])

    async with sessionmaker_() as session:
        await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=fs,
            media_type="movie",
            root_path=str(tmp_path),
            threshold_pct=0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
        )

    # Assembly read + the #209 pre-claim re-read both carry the row's own
    # breadcrumb -- never an untargeted read.
    assert library.watch_state_path_calls == [library_path, library_path]


async def test_eviction_threads_season_library_path_into_watch_state(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    s1_path = _movie_file(tmp_path, "Threaded Show S01.mkv")
    await _show_with_seasons(
        sessionmaker_, tmdb_id=701, title="Threaded Show", seasons={1: s1_path}
    )
    library = FakeLibrary(
        watch_states={(701, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)}
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

    assert library.watch_state_path_calls == [s1_path, s1_path]


async def test_path_correlated_unwatched_verdict_prevents_deletion(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """#207: a tmdb-keyed 'watched' verdict (as if a DUPLICATE elsewhere were
    watched) must never authorize deleting THIS candidate's own file once the
    adapter's path-correlated read says otherwise."""
    library_path = _movie_file(tmp_path, "Duplicate Target.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=702, title="Duplicate Target", library_path=library_path
    )
    library = FakeLibrary(
        # The untargeted (legacy/tmdb-keyed) read says watched -- what a
        # first-match-across-sections bug would have used.
        watch_states={(702, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
        # The path-correlated read -- what the real adapter would resolve
        # for THIS exact candidate's file -- says unwatched.
        watch_states_by_path={library_path: WatchState(watched=False, last_viewed_at=None)},
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
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available


async def test_path_correlated_unwatched_verdict_prevents_season_deletion(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    s1_path = _movie_file(tmp_path, "Duplicate Show S01.mkv")
    show_id = await _show_with_seasons(
        sessionmaker_, tmdb_id=703, title="Duplicate Show", seasons={1: s1_path}
    )
    library = FakeLibrary(
        watch_states={(703, "tv", 1): WatchState(watched=True, last_viewed_at=_STALE)},
        watch_states_by_path={s1_path: WatchState(watched=False, last_viewed_at=None)},
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
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.status is RequestStatus.available


class _RewatchDuringSweepLibrary(FakeLibrary):
    """Watch state flips from stale-watched to just-watched AFTER the first
    (assembly) read for a given tmdb id -- models a rewatch landing during the
    sweep, between candidate assembly and the #209 pre-claim re-read."""

    def __init__(self, *, target_tmdb_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._target_tmdb_id = target_tmdb_id
        self.calls_for_target = 0

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: str,
        *,
        season: int | None = None,
        library_path: str | None = None,
    ) -> WatchState:
        result = await super().watch_state(
            tmdb_id,
            cast(Literal["movie", "tv"], media_type),
            season=season,
            library_path=library_path,
        )
        if tmdb_id == self._target_tmdb_id:
            self.calls_for_target += 1
            if self.calls_for_target > 1:
                return WatchState(watched=True, last_viewed_at=_RECENT)
        return result


async def test_rewatch_during_sweep_is_re_read_before_claim_and_survives(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Rewatched.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=704, title="Rewatched", library_path=library_path
    )
    library = _RewatchDuringSweepLibrary(
        target_tmdb_id=704,
        watch_states={(704, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
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
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available
    # Assembly + the pre-claim re-read: at least two reads for the target tmdb.
    assert library.calls_for_target >= 2


async def test_rewatch_of_one_candidate_does_not_spare_the_others(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The #209 re-read is per-candidate: one rewatch must never abort or
    spare the REST of the sweep."""
    a_path = _movie_file(tmp_path, "Rewatched A.mkv")
    b_path = _movie_file(tmp_path, "Stays Stale B.mkv")
    a_id = await _movie(sessionmaker_, tmdb_id=705, title="Rewatched A", library_path=a_path)
    await _movie(sessionmaker_, tmdb_id=706, title="Stays Stale B", library_path=b_path)
    library = _RewatchDuringSweepLibrary(
        target_tmdb_id=705,
        watch_states={
            (705, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
            (706, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
        },
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

    assert [o.title for o in outcomes] == ["Stays Stale B"]
    assert Path(a_path).exists()  # rewatched -- survives
    assert not Path(b_path).exists()  # never rewatched -- evicted normally
    async with sessionmaker_() as session:
        a_row = await session.get(MediaRequest, a_id)
        assert a_row is not None
        assert a_row.status is RequestStatus.available


class _RaisesOnSecondWatchStateCall(FakeLibrary):
    """The assembly read succeeds; the #209 pre-claim re-read (the SECOND call
    for the same tmdb id) raises ``PlexLibraryError``."""

    def __init__(self, *, target_tmdb_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._target_tmdb_id = target_tmdb_id
        self._calls_for_target = 0

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: str,
        *,
        season: int | None = None,
        library_path: str | None = None,
    ) -> WatchState:
        if tmdb_id == self._target_tmdb_id:
            self._calls_for_target += 1
            if self._calls_for_target > 1:
                raise PlexLibraryError("simulated Plex outage on the pre-claim re-read")
        return await super().watch_state(
            tmdb_id,
            cast(Literal["movie", "tv"], media_type),
            season=season,
            library_path=library_path,
        )


async def test_watch_state_error_before_claim_fails_closed(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    library_path = _movie_file(tmp_path, "Errors On Recheck.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=707, title="Errors On Recheck", library_path=library_path
    )
    library = _RaisesOnSecondWatchStateCall(
        target_tmdb_id=707,
        watch_states={(707, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
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
    assert Path(library_path).exists()
    assert "could not re-read Plex watch state" in caplog.text
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available


class _AbsentOnSecondWatchStateCall(FakeLibrary):
    """The assembly read succeeds; the #209 pre-claim re-read (the SECOND call
    for the same tmdb id) finds NO correlated item at all -- e.g. the
    duplicate that used to back the tmdb-keyed read is gone. This must fail
    closed exactly like an outright error, not merely skip re-checking."""

    def __init__(self, *, target_tmdb_id: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._target_tmdb_id = target_tmdb_id
        self._calls_for_target = 0

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: str,
        *,
        season: int | None = None,
        library_path: str | None = None,
    ) -> WatchState:
        if tmdb_id == self._target_tmdb_id:
            self._calls_for_target += 1
            if self._calls_for_target > 1:
                return WatchState(watched=False, last_viewed_at=None)
        return await super().watch_state(
            tmdb_id,
            cast(Literal["movie", "tv"], media_type),
            season=season,
            library_path=library_path,
        )


async def test_watch_state_absent_before_claim_fails_closed(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "No Longer Correlated.mkv")
    request_id = await _movie(
        sessionmaker_, tmdb_id=708, title="No Longer Correlated", library_path=library_path
    )
    library = _AbsentOnSecondWatchStateCall(
        target_tmdb_id=708,
        watch_states={(708, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
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
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available
