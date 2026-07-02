"""Retention telemetry sweep (ADR-0012 follow-up): a DELETE-NOTHING periodic
observer. Every test here proves the sweep never mutates state (no status
flip, no ``download_history`` row, no ``fs.delete``) while still emitting the
expected structured log shapes -- candidate count / would-free bytes / idle-age
distribution per root, and a completed_at -> last_viewed_at interval per
candidate.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    DownloadHistory,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.library import WatchState
from plex_manager.services import log_capture_service, retention_telemetry_service
from tests.web.fakes import FakeLibrary

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime.now(UTC)
_GRACE_DAYS = 30
_STALE = _NOW - timedelta(days=_GRACE_DAYS + 10)  # 40 days idle -- past grace, a candidate
_RECENT = _NOW - timedelta(days=1)  # within grace -- never a candidate
_TELEMETRY_LOGGER = log_capture_service.TELEMETRY_LOGGER_NAME


def _movie_file(tmp_path: Path, name: str, size: int = 1024) -> str:
    path = tmp_path / "movies" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)
    return str(path)


async def _movie(
    sm: SessionMaker,
    *,
    tmdb_id: int,
    title: str,
    library_path: str | None,
    completed_at: datetime | None = None,
    keep_forever: bool = False,
) -> int:
    async with sm() as session:
        row = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.movie,
            title=title,
            status=RequestStatus.available,
            library_path=library_path,
            keep_forever=keep_forever,
            completed_at=completed_at,
        )
        session.add(row)
        await session.commit()
        return row.id


def _tv_file(tmp_path: Path, name: str, size: int = 1024) -> str:
    path = tmp_path / "tv" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)
    return str(path)


async def _show_with_season(
    sm: SessionMaker,
    *,
    tmdb_id: int,
    title: str,
    season_number: int,
    library_path: str | None,
    completed_at: datetime | None = None,
) -> int:
    """Insert a tv show ``MediaRequest`` plus one available ``SeasonRequest``;
    return the PARENT show's ``MediaRequest`` id (the id the TV branch of
    ``_candidate_context`` walks to from the season's ``request_id``)."""
    async with sm() as session:
        show = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title=title,
            status=RequestStatus.available,
            completed_at=completed_at,
        )
        session.add(show)
        await session.flush()
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


async def test_sweep_never_deletes_the_file_or_touches_status_or_history(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library_path = _movie_file(tmp_path, "Old Movie.mkv")
    request_id = await _movie(
        sessionmaker_,
        tmdb_id=1,
        title="Old Movie",
        library_path=library_path,
        completed_at=_STALE - timedelta(days=1),
    )
    library = FakeLibrary(
        watch_states={(1, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async with sessionmaker_() as session:
        await retention_telemetry_service.run_retention_telemetry_sweep(
            session=session,
            library=library,
            media_type="movie",
            root_path=str(tmp_path),
            grace_days=_GRACE_DAYS,
            now=_NOW,
        )

    # Delete-nothing: the file, the status, and the history table are untouched.
    assert Path(library_path).exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available
        history = (await session.execute(select(DownloadHistory))).scalars().all()
    assert history == []


async def test_sweep_emits_the_expected_aggregate_and_per_candidate_events(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    library_path = _movie_file(tmp_path, "Old Movie.mkv", size=100)
    completed_at = _STALE - timedelta(days=2)
    await _movie(
        sessionmaker_,
        tmdb_id=1,
        title="Old Movie",
        library_path=library_path,
        completed_at=completed_at,
    )
    library = FakeLibrary(
        watch_states={(1, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    assert len(records) == 2  # one aggregate event, one per-candidate event

    aggregate_message = records[0].getMessage()
    assert "1 eviction candidate(s)" in aggregate_message
    assert "100 byte(s)" in aggregate_message  # the whole (100b) file -- total_bytes >= size
    assert "1 available watched title(s)" in aggregate_message
    # _STALE is 40 days idle -- lands in the 30-60d bucket, every other bucket is 0.
    assert "30-60d=1" in aggregate_message
    assert "<7d=0" in aggregate_message
    assert "90d+=0" in aggregate_message

    per_candidate = records[1]
    message = per_candidate.getMessage()
    assert "Old Movie" in message
    assert completed_at.isoformat() in message
    assert _STALE.isoformat() in message
    # Every id goes through extra={}, never interpolated into the message text.
    assert getattr(per_candidate, "request_id", None) is not None
    assert getattr(per_candidate, "tmdb_id", None) == 1


async def test_sweep_leaves_the_interval_unknown_without_a_completed_at(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    library_path = _movie_file(tmp_path, "No Completed At.mkv")
    await _movie(
        sessionmaker_,
        tmdb_id=4,
        title="No Completed At",
        library_path=library_path,
        completed_at=None,  # a row predating the completed_at stamp, or never set
    )
    library = FakeLibrary(
        watch_states={(4, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    assert "completed_to_first_watch=unknown" in records[1].getMessage()


async def test_within_grace_watched_title_is_tracked_for_time_to_watch_but_not_evictable(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A title watched within grace is NOT a would-evict candidate, but IS still
    captured in the time-to-watch dataset (idle-age distribution + per-title
    interval) -- this is the whole point during a sub-30-day beta, where every
    watched title is by definition within a 30-day grace."""
    library_path = _movie_file(tmp_path, "Recently Watched.mkv")
    completed_at = _RECENT - timedelta(days=2)
    await _movie(
        sessionmaker_,
        tmdb_id=2,
        title="Recently Watched",
        library_path=library_path,
        completed_at=completed_at,
    )
    library = FakeLibrary(
        watch_states={(2, "movie", None): WatchState(watched=True, last_viewed_at=_RECENT)}
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    # Aggregate + one per-title event: within grace, so NOT an eviction candidate,
    # but STILL an available watched title tracked for time-to-watch.
    assert len(records) == 2
    aggregate_message = records[0].getMessage()
    assert "0 eviction candidate(s)" in aggregate_message  # within grace -- not evictable
    assert "0 byte(s)" in aggregate_message  # nothing would free
    assert "1 available watched title(s)" in aggregate_message
    # _RECENT is 1 day idle -- lands in the pre-grace <7d bucket (previously
    # unreachable, now populated -- the primary week-1 signal).
    assert "<7d=1" in aggregate_message
    assert "30-60d=0" in aggregate_message

    per_candidate = records[1].getMessage()
    assert "Recently Watched" in per_candidate
    assert completed_at.isoformat() in per_candidate
    assert _RECENT.isoformat() in per_candidate
    assert Path(library_path).exists()  # delete-nothing, even for a non-candidate


async def test_sweep_reports_zero_candidates_for_a_keep_forever_pinned_title(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    library_path = _movie_file(tmp_path, "Pinned.mkv")
    await _movie(
        sessionmaker_,
        tmdb_id=3,
        title="Pinned",
        library_path=library_path,
        keep_forever=True,
    )
    library = FakeLibrary(
        watch_states={(3, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    # Pinned (keep_forever) is excluded from BOTH products -- not a would-evict
    # candidate AND not counted as an available watched title -- so only the
    # aggregate event fires, with zeros across the board.
    assert len(records) == 1
    aggregate_message = records[0].getMessage()
    assert "0 eviction candidate(s)" in aggregate_message
    assert "0 available watched title(s)" in aggregate_message
    assert Path(library_path).exists()


async def test_sweep_resolves_a_tv_season_context_from_the_parent_show(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The TV branch of ``_candidate_context`` (movie-only tests never touch it):
    ``candidate.request_id`` is a ``SeasonRequest`` id, resolved to the PARENT
    ``MediaRequest`` for ``request_id``/``tmdb_id`` and its ``completed_at`` as
    the (documented-approximation) time-to-watch reference for the season. The
    per-candidate event must carry ``season N`` and compute the parent
    completed_at -> last_viewed_at interval."""
    library_path = _tv_file(tmp_path, "Some Show S05.mkv", size=100)
    completed_at = _STALE - timedelta(days=3)  # exact 3d -> 259200s interval to _STALE
    show_id = await _show_with_season(
        sessionmaker_,
        tmdb_id=55,
        title="Some Show",
        season_number=5,
        library_path=library_path,
        completed_at=completed_at,
    )
    library = FakeLibrary(
        available_tv_seasons={55: frozenset({5})},
        watch_states={(55, "tv", 5): WatchState(watched=True, last_viewed_at=_STALE)},
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                media_type="tv",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    assert len(records) == 2  # one aggregate event, one per-candidate event

    aggregate_message = records[0].getMessage()
    assert "tv root" in aggregate_message
    assert "1 eviction candidate(s)" in aggregate_message  # _STALE is past grace
    assert "1 available watched title(s)" in aggregate_message

    per_candidate = records[1]
    message = per_candidate.getMessage()
    assert "season 5" in message
    assert completed_at.isoformat() in message  # the PARENT show's completed_at
    assert _STALE.isoformat() in message
    assert "completed_to_first_watch=259200s" in message  # last_viewed - parent completed
    # ids resolved by walking season -> parent, passed via extra={}, never in text.
    assert getattr(per_candidate, "request_id", None) == show_id
    assert getattr(per_candidate, "tmdb_id", None) == 55


async def test_sweep_leaves_the_tv_interval_unknown_when_the_parent_has_no_completed_at(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The TV 'unknown' fallback: a season whose PARENT show carries no
    ``completed_at`` (a row predating the stamp) still resolves its parent
    ids, but the completed_at -> first-watch interval is honestly 'unknown'
    rather than guessed."""
    library_path = _tv_file(tmp_path, "No Completed Show S02.mkv")
    show_id = await _show_with_season(
        sessionmaker_,
        tmdb_id=56,
        title="No Completed Show",
        season_number=2,
        library_path=library_path,
        completed_at=None,  # parent never stamped a completion time
    )
    library = FakeLibrary(
        available_tv_seasons={56: frozenset({2})},
        watch_states={(56, "tv", 2): WatchState(watched=True, last_viewed_at=_STALE)},
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                media_type="tv",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    per_candidate = records[1]
    message = per_candidate.getMessage()
    assert "season 2" in message
    assert "completed_at=unknown" in message
    assert "completed_to_first_watch=unknown" in message
    # Parent ids are still resolved even when the timestamp is missing.
    assert getattr(per_candidate, "request_id", None) == show_id
    assert getattr(per_candidate, "tmdb_id", None) == 56


async def test_unreadable_root_is_skipped_without_crashing(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    library = FakeLibrary()
    missing_root = str(tmp_path / "does" / "not" / "exist")

    async with sessionmaker_() as session:
        # Must not raise.
        await retention_telemetry_service.run_retention_telemetry_sweep(
            session=session,
            library=library,
            media_type="movie",
            root_path=missing_root,
            grace_days=_GRACE_DAYS,
            now=_NOW,
        )
