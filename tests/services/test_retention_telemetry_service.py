"""Retention telemetry sweep (ADR-0012 follow-up): a DELETE-NOTHING periodic
observer. Every test here proves the sweep never mutates state (no status
flip, no ``download_history`` row, no ``fs.delete``) while still emitting the
expected structured log shapes -- candidate count / would-free bytes / idle-age
distribution per root, and a completed_at -> last_viewed_at interval per
candidate.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.domain.eviction import EvictionCandidate
from plex_manager.models import (
    DownloadHistory,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.library import WatchState
from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.services import eviction_service, log_capture_service, retention_telemetry_service
from tests.web.fakes import FakeLibrary

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime.now(UTC)
_GRACE_DAYS = 30
_STALE = _NOW - timedelta(days=_GRACE_DAYS + 10)  # 40 days idle -- past grace, a candidate
_RECENT = _NOW - timedelta(days=1)  # within grace -- never a candidate
_TELEMETRY_LOGGER = log_capture_service.TELEMETRY_LOGGER_NAME

# Disk-pressure percentages the sweep uses to simulate a would-evict selection.
# The candidate files here are a few bytes on a real (huge) test filesystem, so
# each candidate's size_percent rounds to ~0 -- select_evictions therefore never
# reaches the target by shedding them and picks EVERY eligible candidate. That is
# the intended below-pressure shape: with a roomy disk, "what a sweep would pick"
# equals the full eligible set. The strict would_evict SUBSET behaviour (a prefix
# that stops at the target) is proven separately, with controlled size_percent,
# in test_would_evict_reports_the_select_prefix_not_the_full_eligible_set.
_THRESHOLD = 80.0
_TARGET = 50.0


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
            threshold_pct=_THRESHOLD,
            target_pct=_TARGET,
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
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    assert len(records) == 2  # one aggregate event, one per-candidate event

    aggregate_message = records[0].getMessage()
    assert "1 eligible eviction candidate(s)" in aggregate_message
    # Below pressure on a roomy disk: the whole eligible set is what a sweep would
    # pick (each ~0% of the disk never crosses the target on its own).
    assert "1 would_evict now" in aggregate_message
    assert "100 byte(s)" in aggregate_message  # the whole (100b) file -- total_bytes >= size
    assert "1 title(s) with recorded watch activity" in aggregate_message
    # _STALE is 40 days idle -- lands in the 30-60d bucket, every other bucket is 0.
    assert "30-60d=1" in aggregate_message
    assert "<7d=0" in aggregate_message
    assert "90d+=0" in aggregate_message

    per_candidate = records[1]
    message = per_candidate.getMessage()
    assert "Old Movie" in message
    assert completed_at.isoformat() in message
    assert _STALE.isoformat() in message
    assert "completed_to_last_watch=" in message  # relabelled: last view, not first
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
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    assert "completed_to_last_watch=unknown" in records[1].getMessage()


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
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    # Aggregate + one per-title event: within grace, so NOT an eviction candidate,
    # but STILL a title with recorded watch activity tracked for time-to-watch.
    assert len(records) == 2
    aggregate_message = records[0].getMessage()
    assert "0 eligible eviction candidate(s)" in aggregate_message  # within grace
    assert "0 would_evict now" in aggregate_message  # nothing to evict
    assert "0 byte(s)" in aggregate_message  # nothing would free
    assert "1 title(s) with recorded watch activity" in aggregate_message
    # _RECENT is 1 day idle -- lands in the pre-grace <7d bucket (previously
    # unreachable, now populated -- the primary week-1 signal).
    assert "<7d=1" in aggregate_message
    assert "30-60d=0" in aggregate_message

    per_candidate = records[1].getMessage()
    assert "Recently Watched" in per_candidate
    assert completed_at.isoformat() in per_candidate
    assert _RECENT.isoformat() in per_candidate
    assert Path(library_path).exists()  # delete-nothing, even for a non-candidate


async def test_sweep_excludes_a_pinned_title_from_eviction_but_tracks_its_watch_activity(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A keep_forever pin is an EVICTION concern, not a watch-behaviour one: a
    pinned title is never an eviction candidate (eligible or would-evict), but if
    it has been watched it is still a valid time-to-watch data point. The two
    products are decoupled -- the eligibility filter must not corrupt the
    watch-activity dataset (the same principle as the partial-watch case)."""
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
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    # Aggregate (zeros for eviction) + one per-title watch-activity event.
    assert len(records) == 2
    aggregate_message = records[0].getMessage()
    assert "0 eligible eviction candidate(s)" in aggregate_message
    assert "0 would_evict now" in aggregate_message
    assert "1 title(s) with recorded watch activity" in aggregate_message
    assert "Pinned" in records[1].getMessage()
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
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    assert len(records) == 2  # one aggregate event, one per-candidate event

    aggregate_message = records[0].getMessage()
    assert "tv root" in aggregate_message
    assert "1 eligible eviction candidate(s)" in aggregate_message  # _STALE is past grace
    assert "1 title(s) with recorded watch activity" in aggregate_message

    per_candidate = records[1]
    message = per_candidate.getMessage()
    assert "season 5" in message
    assert completed_at.isoformat() in message  # the PARENT show's completed_at
    assert _STALE.isoformat() in message
    assert "completed_to_last_watch=259200s" in message  # last_viewed - parent completed
    # ids resolved by walking season -> parent, passed via extra={}, never in text.
    assert getattr(per_candidate, "request_id", None) == show_id
    assert getattr(per_candidate, "tmdb_id", None) == 55


async def test_sweep_leaves_the_tv_interval_unknown_when_the_parent_has_no_completed_at(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The TV 'unknown' fallback: a season whose PARENT show carries no
    ``completed_at`` (a row predating the stamp) still resolves its parent
    ids, but the completed_at -> last-watch interval is honestly 'unknown'
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
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    per_candidate = records[1]
    message = per_candidate.getMessage()
    assert "season 2" in message
    assert "completed_at=unknown" in message
    assert "completed_to_last_watch=unknown" in message
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
            threshold_pct=_THRESHOLD,
            target_pct=_TARGET,
            now=_NOW,
        )


async def test_partial_watch_is_captured_for_time_to_watch_but_never_evictable(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A started-but-unfinished season -- ``watched=False`` but a Plex
    ``lastViewedAt`` exists (``viewedLeafCount < leafCount``) -- is exactly the
    'began watching' signal. The eligibility filter behind the would-evict counts
    drops it (never fully watched, so never an eviction candidate), but the
    time-to-watch dataset is built from the RAW watch-state rows (any recorded
    view) and MUST still capture it. Regression for the dropped-partial-watch
    finding."""
    library_path = _tv_file(tmp_path, "Half Watched S01.mkv")
    completed_at = _RECENT - timedelta(days=2)
    show_id = await _show_with_season(
        sessionmaker_,
        tmdb_id=77,
        title="Half Watched",
        season_number=1,
        library_path=library_path,
        completed_at=completed_at,
    )
    library = FakeLibrary(
        available_tv_seasons={77: frozenset({1})},
        # A real Plex partial-watch state: some episodes unseen (watched=False)
        # yet a season lastViewedAt exists -- someone began the season.
        watch_states={(77, "tv", 1): WatchState(watched=False, last_viewed_at=_RECENT)},
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                media_type="tv",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    assert len(records) == 2  # aggregate + the partial-watch per-title event
    aggregate_message = records[0].getMessage()
    # Not fully watched -> never an eviction candidate (eligible OR would-evict)...
    assert "0 eligible eviction candidate(s)" in aggregate_message
    assert "0 would_evict now" in aggregate_message
    # ...but the 'began watching' signal is still counted and distributed.
    assert "1 title(s) with recorded watch activity" in aggregate_message
    assert "<7d=1" in aggregate_message  # _RECENT is 1 day idle

    per_candidate = records[1]
    message = per_candidate.getMessage()
    assert "Half Watched" in message
    assert "season 1" in message
    assert completed_at.isoformat() in message
    assert _RECENT.isoformat() in message
    assert getattr(per_candidate, "request_id", None) == show_id
    assert Path(library_path).exists()  # delete-nothing


async def test_would_evict_reports_the_select_prefix_not_the_full_eligible_set(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``would_evict`` must be the :func:`select_evictions` prefix (what a
    pressure sweep would ACTUALLY delete to reach the target), never the full
    eligible ranking. With controlled sizes the two diverge: three eligible
    candidates each ~20% of the root, and a 90%->50% relief needs only the two
    stalest (90->70->50). Regression for the overstated would-free finding."""

    def _mk(request_id: int, last_viewed: datetime) -> EvictionCandidate:
        return EvictionCandidate(
            request_id=request_id,
            media_type="movie",
            title=f"M{request_id}",
            season=None,
            status="available",
            watched=True,
            last_viewed_at=last_viewed,
            keep_forever=False,
            in_flight=False,
            library_path=str(tmp_path / f"m{request_id}.mkv"),
            size_percent=20.0,
        )

    candidates = [
        _mk(1, _NOW - timedelta(days=90)),  # stalest -> selected first
        _mk(2, _NOW - timedelta(days=80)),
        _mk(3, _NOW - timedelta(days=70)),  # eligible, but the target is met before it
    ]

    async def _fake_assemble(**_kwargs: object) -> list[EvictionCandidate]:
        return candidates

    monkeypatch.setattr(eviction_service, "assemble_candidates", _fake_assemble)

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=FakeLibrary(),
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                threshold_pct=90.0,
                target_pct=50.0,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    aggregate_message = records[0].getMessage()
    assert "3 eligible eviction candidate(s)" in aggregate_message
    assert "2 would_evict now" in aggregate_message  # the select prefix, not all 3
    assert "90.0%->50.0%" in aggregate_message


async def test_telemetry_records_reach_the_db_sink_at_a_warning_operator_floor(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """At an operator ``log_level`` of WARNING/ERROR, the INFO telemetry records
    used to be filtered by the (inherited) effective level at the ``_logger.info``
    call BEFORE the durable-log handler ever saw them -- the beta dataset silently
    never persisted. ``configure_logging`` pins the telemetry logger to INFO so
    its records still reach the ``LogCaptureHandler`` (and thus ``log_events``) at
    any operator floor, while ordinary INFO chatter stays suppressed. Regression
    for the log-floor finding."""
    library_path = _movie_file(tmp_path, "Floor Test.mkv")
    await _movie(
        sessionmaker_,
        tmdb_id=8,
        title="Floor Test",
        library_path=library_path,
        completed_at=_STALE - timedelta(days=1),
    )
    library = FakeLibrary(
        watch_states={(8, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    root = logging.getLogger()
    telemetry_logger = logging.getLogger(_TELEMETRY_LOGGER)
    saved_root_level = root.level
    saved_telemetry_level = telemetry_logger.level
    # WARNING is the exact operator floor that used to drop INFO telemetry.
    handler = log_capture_service.configure_logging("WARNING")
    try:
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )
        # emit() hands INFO records to the queue via call_soon_threadsafe -- let
        # the loop run those callbacks before draining them.
        await asyncio.sleep(0)
        async with sessionmaker_() as session:
            repo = SqlLogEventRepository(session)
            inserted = await log_capture_service.drain_once(handler.queue, repo)
            await session.commit()
            page = await repo.list_events(logger=_TELEMETRY_LOGGER, limit=50)
    finally:
        log_capture_service.stop_logging(handler)
        root.setLevel(saved_root_level)
        telemetry_logger.setLevel(saved_telemetry_level)

    # The aggregate + per-candidate telemetry rows reached the durable sink
    # despite the WARNING floor.
    assert inserted >= 2
    assert page.total >= 2
    assert all(r.logger == _TELEMETRY_LOGGER for r in page.results)
    assert any("retention telemetry" in r.message for r in page.results)
