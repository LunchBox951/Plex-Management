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

from plex_manager.models import DownloadHistory, MediaRequest, MediaType, RequestStatus
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


async def test_sweep_reports_zero_candidates_for_a_within_grace_watched_title(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    library_path = _movie_file(tmp_path, "Recently Watched.mkv")
    await _movie(sessionmaker_, tmdb_id=2, title="Recently Watched", library_path=library_path)
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
    assert len(records) == 1  # only the (zero-candidate) aggregate event -- no per-title event
    assert "0 eviction candidate(s)" in records[0].getMessage()
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
    assert "0 eviction candidate(s)" in records[0].getMessage()  # pinned -- never a candidate
    assert Path(library_path).exists()


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
