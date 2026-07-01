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

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
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
