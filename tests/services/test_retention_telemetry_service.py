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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.domain.disk_usage import DiskUsage
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


@pytest.fixture(autouse=True)
def reset_watch_dedupe_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a fresh copy of the sweep's per-title dedupe cache
    (``retention_telemetry_service._last_emitted_watch``, a PROCESS-lifetime dict),
    restored on teardown, so one test's emitted titles can never suppress a
    per-title row another test expects. The dedupe-across-sweeps behaviour is
    exercised WITHIN a single test, which does not reset between its own sweeps."""
    monkeypatch.setattr(retention_telemetry_service, "_last_emitted_watch", {})


def _local_fs(tmp_path: Path) -> LocalFileSystem:
    """The real ``LocalFileSystem`` scoped to ``tmp_path`` -- the sweep only calls
    its read-only ``reclaimable_bytes`` (never ``delete``), which for the plain,
    single-link files these tests create returns each file's real size."""
    return LocalFileSystem(library_roots=[str(tmp_path)])


class _MappedReclaimFileSystem:
    """Minimal :class:`~plex_manager.ports.filesystem.FileSystemPort` for the
    would-evict simulation: only ``reclaimable_bytes`` is used (delete-nothing),
    returning a per-path fixed value (default ``0``). Lets a test drive the exact
    hardlink shortfall -- a candidate whose reclaimable bytes fall below its
    nominal size -- without laying down real hardlinks. Every other method is
    unused by the telemetry sweep and raises."""

    def __init__(self, reclaimable: dict[str, int]) -> None:
        self._reclaimable = reclaimable
        self.reclaimable_calls: list[str] = []

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
        raise NotImplementedError

    def delete_guard_refuses(self, path: str) -> bool:
        # These controlled would-evict tests drive the reclaimable-aware extension,
        # not the delete guard: never refuse, so every mapped candidate is counted.
        return False

    def reclaimable_bytes(self, path: str) -> int:
        self.reclaimable_calls.append(path)
        return self._reclaimable.get(path, 0)


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
# that stops at the target, plus the reclaimable-aware extension past it) is
# proven separately, with controlled size_percent and reclaimable bytes, in
# test_would_evict_reports_the_select_prefix_when_reclaimable_matches_nominal and
# test_would_evict_extends_past_the_select_prefix_when_reclaimable_falls_short.
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
            fs=_local_fs(tmp_path),
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
                fs=_local_fs(tmp_path),
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
                fs=_local_fs(tmp_path),
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
                fs=_local_fs(tmp_path),
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
                fs=_local_fs(tmp_path),
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
                fs=_local_fs(tmp_path),
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
                fs=_local_fs(tmp_path),
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
            fs=_local_fs(tmp_path),
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
                fs=_local_fs(tmp_path),
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


def _controlled_candidates(tmp_path: Path) -> list[EvictionCandidate]:
    """Three eligible movie candidates, each nominally 20% of a controlled 1000-byte
    root, stalest (90d) first -- the shared fixture behind the two would-evict
    selection tests below."""

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

    return [
        _mk(1, _NOW - timedelta(days=90)),  # stalest -> selected first
        _mk(2, _NOW - timedelta(days=80)),
        _mk(3, _NOW - timedelta(days=70)),  # last in rank order
    ]


def _fake_1000_byte_disk(_path: str) -> DiskUsage:
    """Monkeypatch stand-in for ``read_disk_usage``: a controlled 1000-byte root so
    each candidate's 20% ``size_percent`` maps to a meaningful 200 bytes."""
    return DiskUsage(root=_path, total_bytes=1000, available_bytes=100)


async def test_would_evict_reports_the_select_prefix_when_reclaimable_matches_nominal(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When every candidate reclaims its full nominal size (no hardlinks),
    ``would_evict`` is exactly the :func:`select_evictions` prefix -- never the
    full eligible ranking. Three candidates each 20% of a 1000-byte root; a
    90%->50% relief is met by the two stalest (90->70->50), so the third is
    eligible but NOT in would_evict. Regression for the overstated would-free
    finding."""
    candidates = _controlled_candidates(tmp_path)

    async def _fake_assemble(**_kwargs: object) -> list[EvictionCandidate]:
        return candidates

    monkeypatch.setattr(eviction_service, "assemble_candidates", _fake_assemble)
    monkeypatch.setattr(retention_telemetry_service, "read_disk_usage", _fake_1000_byte_disk)
    # Each of the two selected candidates reclaims its full 20% (200 bytes) -- no
    # hardlink shortfall, so the extension never fires.
    fs = _MappedReclaimFileSystem(
        {candidates[0].library_path or "": 200, candidates[1].library_path or "": 200}
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=FakeLibrary(),
                fs=fs,
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
    assert "400 byte(s) reclaimable" in aggregate_message  # 200 + 200, the two selected
    # The third (unselected) candidate's reclaimable bytes are never even measured.
    assert candidates[2].library_path not in fs.reclaimable_calls


async def test_would_evict_extends_past_the_select_prefix_when_reclaimable_falls_short(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The finding-#3 regression: a real pressure sweep extends PAST the
    ``select_evictions`` prefix when the measured reclaimable bytes fall short of
    the nominal estimate (a hardlinked import frees ~nothing). The telemetry
    would-evict simulation MUST do the same, reusing ``run_eviction_sweep``'s
    reclaimable-aware extension (the shared ``pressure_relieved`` predicate), or it
    understates what a real sweep would delete.

    Same three 20%-of-1000-byte candidates as the prefix test, but the STALEST
    (selected first) is hardlinked and reclaims 0 bytes. The nominal prefix is
    still [m1, m2], but after evicting them only 200 bytes are freed (m1 gave
    nothing) -- not enough to reach the target -- so the sweep draws m3 too."""
    candidates = _controlled_candidates(tmp_path)

    async def _fake_assemble(**_kwargs: object) -> list[EvictionCandidate]:
        return candidates

    monkeypatch.setattr(eviction_service, "assemble_candidates", _fake_assemble)
    monkeypatch.setattr(retention_telemetry_service, "read_disk_usage", _fake_1000_byte_disk)
    # m1 (stalest, selected first) is hardlinked -> reclaims 0; m2, m3 reclaim
    # their full 200 each. 0 + 200 = 200 leaves used at 90-20=70 > 50 target, so
    # the extension pulls m3 (the +200 that finally closes the gap).
    fs = _MappedReclaimFileSystem(
        {
            candidates[0].library_path or "": 0,
            candidates[1].library_path or "": 200,
            candidates[2].library_path or "": 200,
        }
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=FakeLibrary(),
                fs=fs,
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
    # Extended past the 2-candidate nominal prefix because m1 reclaimed nothing.
    assert "3 would_evict now" in aggregate_message
    assert "400 byte(s) reclaimable" in aggregate_message  # 0 + 200 + 200


async def test_no_path_row_is_excluded_from_metrics_and_counted(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Round-3 finding #1: a row with NO library_path breadcrumb (a movie found
    already in Plex, short-circuited straight to 'available' -- completed_at stamped
    at Plex-verification time, but no file of ours ever placed) can never be evicted
    and its completed_at is not an import time. It must be excluded from the
    eligible/would-evict metrics AND the time-to-watch dataset, and surfaced as
    no_path on the aggregate -- never silently counted, never silently dropped. A
    normal on-disk row alongside it proves the split is honest, not blanket."""
    on_disk = _movie_file(tmp_path, "On Disk.mkv", size=100)
    await _movie(
        sessionmaker_,
        tmdb_id=1,
        title="On Disk",
        library_path=on_disk,
        completed_at=_STALE - timedelta(days=2),
    )
    await _movie(
        sessionmaker_,
        tmdb_id=2,
        title="Already In Plex",
        library_path=None,  # in-Plex short-circuit: no breadcrumb, nothing of ours on disk
        completed_at=_STALE - timedelta(days=2),
    )
    library = FakeLibrary(
        watch_states={
            (1, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
            (2, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
        }
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                fs=_local_fs(tmp_path),
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    aggregate = records[0].getMessage()
    # Only the on-disk row is an eligible/would-evict candidate; the no-path row is
    # set aside and counted, not folded into the eviction metrics.
    assert "1 eligible eviction candidate(s)" in aggregate
    assert "no_path=1" in aggregate
    # ...and excluded from the time-to-watch dataset too (its completed_at is not an
    # import time), so only the on-disk row is watch activity.
    assert "1 title(s) with recorded watch activity" in aggregate
    per_title = [r for r in records if "completed_to_last_watch=" in r.getMessage()]
    assert len(per_title) == 1
    assert "On Disk" in per_title[0].getMessage()
    assert "Already In Plex" not in per_title[0].getMessage()


async def test_guard_refused_breadcrumb_is_excluded_from_would_evict_and_counted(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Round-3 finding #2: a breadcrumb LEXICALLY under root_path that resolves, via
    a symlinked path component, OUTSIDE every configured root. The real
    LocalFileSystem.delete refuses it (its realpath guard) and frees nothing, so the
    would-evict simulation must run each candidate through fs's OWN delete guard and
    exclude a refused row's bytes/count -- reporting it as guard_refused -- rather
    than counting bytes a real sweep could never free. Delete-nothing throughout."""
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    real_file = outside / "escapee.mkv"
    real_file.write_bytes(b"0" * 100)
    # A symlinked COMPONENT (not a symlink final entry): root/escaped -> outside, so
    # root/escaped/escapee.mkv is lexically under root but realpaths to outside.
    (root / "escaped").symlink_to(outside)
    breadcrumb = str(root / "escaped" / "escapee.mkv")
    await _movie(
        sessionmaker_,
        tmdb_id=1,
        title="Escapee",
        library_path=breadcrumb,
        completed_at=_STALE - timedelta(days=2),
    )
    library = FakeLibrary(
        watch_states={(1, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )
    fs = LocalFileSystem(library_roots=[str(root)])

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(root),
                grace_days=_GRACE_DAYS,
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    aggregate = records[0].getMessage()
    # Eligible by policy (watched, past grace, unpinned), but the delete guard would
    # refuse it, so it is NOT in would_evict and frees nothing.
    assert "1 eligible eviction candidate(s)" in aggregate
    assert "0 would_evict now" in aggregate
    assert "0 byte(s) reclaimable" in aggregate
    assert "guard_refused=1" in aggregate
    # Delete-nothing: the escaping target file is untouched.
    assert real_file.exists()


async def test_preexisting_watch_interval_is_dropped_and_counted(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Round-3 finding #3: a re-imported / previously-watched title keeps Plex's OLD
    last_viewed_at from BEFORE this import, so last_viewed_at < completed_at -- a
    negative completed_to_last_watch. The interval measures POST-import
    time-to-watch, which a pre-import view does not have: drop that per-title row and
    count it as preexisting_watch. A normal post-import view still emits its positive
    interval -- both dropped+counted and normal+emitted are proven here."""
    positive_path = _movie_file(tmp_path, "Watched After Import.mkv")
    await _movie(
        sessionmaker_,
        tmdb_id=1,
        title="Watched After Import",
        library_path=positive_path,
        completed_at=_NOW - timedelta(days=20),  # imported...
    )
    negative_path = _movie_file(tmp_path, "Rewatch Reimport.mkv")
    await _movie(
        sessionmaker_,
        tmdb_id=2,
        title="Rewatch Reimport",
        library_path=negative_path,
        completed_at=_NOW - timedelta(days=5),  # re-imported recently...
    )
    library = FakeLibrary(
        watch_states={
            # ...then watched 5 days ago: a positive, post-import interval.
            (1, "movie", None): WatchState(watched=True, last_viewed_at=_NOW - timedelta(days=5)),
            # ...but Plex's view is 20 days old, from BEFORE the re-import: negative.
            (2, "movie", None): WatchState(watched=True, last_viewed_at=_NOW - timedelta(days=20)),
        }
    )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                fs=_local_fs(tmp_path),
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    aggregate = records[0].getMessage()
    assert "2 title(s) with recorded watch activity" in aggregate  # both have views
    assert "preexisting_watch=1" in aggregate
    per_title = [r for r in records if "completed_to_last_watch=" in r.getMessage()]
    # Only the positive (post-import) interval is emitted; the negative one dropped.
    assert len(per_title) == 1
    message = per_title[0].getMessage()
    assert "Watched After Import" in message
    assert "Rewatch Reimport" not in message
    assert "completed_to_last_watch=-" not in message  # never a negative interval


async def test_per_title_row_is_deduped_until_last_viewed_advances(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The finding-#2 regression: per-title ``completed_to_last_watch`` rows must
    NOT re-emit on every below-pressure tick. A title's row is logged once, then
    suppressed while its ``last_viewed_at`` is unchanged, and logged again only
    when it advances (a fresh play). The per-root aggregate always emits (it is
    the time-series)."""
    library_path = _movie_file(tmp_path, "Rewatched.mkv")
    await _movie(
        sessionmaker_,
        tmdb_id=9,
        title="Rewatched",
        library_path=library_path,
        completed_at=_STALE - timedelta(days=2),
    )
    library = FakeLibrary(
        watch_states={(9, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async def _sweep() -> None:
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                fs=_local_fs(tmp_path),
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        await _sweep()  # sweep 1: aggregate + per-title
        await _sweep()  # sweep 2: aggregate only -- unchanged watch state is deduped
        # A fresh play advances last_viewed_at -> the per-title row emits again.
        library.watch_states[(9, "movie", None)] = WatchState(
            watched=True, last_viewed_at=_STALE + timedelta(days=1)
        )
        await _sweep()  # sweep 3: aggregate + per-title

    records = [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]
    aggregate = [r for r in records if "eligible eviction candidate(s)" in r.getMessage()]
    per_title = [r for r in records if "completed_to_last_watch=" in r.getMessage()]
    assert len(aggregate) == 3  # the aggregate time-series emits every sweep
    assert len(per_title) == 2  # emitted on sweep 1 and 3, deduped on sweep 2


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
                fs=_local_fs(tmp_path),
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


async def test_per_title_emission_budget_defers_overflow_and_loses_nothing(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 finding #1: a per-title burst larger than a sweep's emission budget
    must NOT be cached-as-emitted then silently dropped when the ``LogCaptureHandler``
    queue overruns (the logging API can't report a per-record enqueue failure). Each
    sweep emits at most the budget, reports the honest ``deferred_rows``, leaves the
    overflow UN-cached, and the next tick emits exactly the remainder -- every row
    eventually emitted, none lost, none duplicated."""
    # A tiny budget keeps the test fast: 5 watched titles, budget of 3 -> a 2-row
    # overflow the first sweep must defer rather than drop.
    monkeypatch.setattr(retention_telemetry_service, "_PER_SWEEP_EMISSION_BUDGET", 3)
    tmdb_ids = list(range(101, 106))
    for i, tmdb in enumerate(tmdb_ids):
        await _movie(
            sessionmaker_,
            tmdb_id=tmdb,
            title=f"Burst Movie {i}",
            library_path=_movie_file(tmp_path, f"Burst {i}.mkv"),
            completed_at=_STALE - timedelta(days=2),  # positive, post-import interval
        )
    library = FakeLibrary(
        watch_states={
            (tmdb, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)
            for tmdb in tmdb_ids
        }
    )

    async def _sweep() -> None:
        async with sessionmaker_() as session:
            await retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                fs=_local_fs(tmp_path),
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=_GRACE_DAYS,
                threshold_pct=_THRESHOLD,
                target_pct=_TARGET,
                now=_NOW,
            )

    def _telemetry_records() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]

    def _per_title(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
        return [r for r in records if "completed_to_last_watch=" in r.getMessage()]

    def _aggregates(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
        return [r for r in records if "eligible eviction candidate(s)" in r.getMessage()]

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        await _sweep()  # sweep 1: emits budget=3, defers 2
        first = _telemetry_records()
        assert len(_per_title(first)) == 3  # exactly the budget, not all five
        assert "deferred_rows=2" in _aggregates(first)[-1].getMessage()
        await _sweep()  # sweep 2: emits the deferred 2 (the first 3 are now deduped)

    records = _telemetry_records()
    per_title = _per_title(records)
    # Nothing lost, nothing duplicated: all five titles emitted exactly once across
    # the two sweeps (the deferred tail was un-cached, so the second sweep picked it
    # up -- and the already-emitted three were deduped, never re-sent).
    assert len(per_title) == 5
    emitted_tmdb: list[int] = []
    for r in per_title:
        value = getattr(r, "tmdb_id", None)  # set via extra={}, so not a static attr
        assert isinstance(value, int)
        emitted_tmdb.append(value)
    assert sorted(emitted_tmdb) == tmdb_ids
    # The second sweep drained the remainder with an honest deferred_rows=0.
    assert "deferred_rows=0" in _aggregates(records)[-1].getMessage()


async def _burst_setup(
    sm: SessionMaker, tmp_path: Path, *, base_tmdb: int, count: int = 5
) -> tuple[FakeLibrary, list[int]]:
    """Insert ``count`` stale, watched, POST-import movies and a matching
    ``FakeLibrary`` -- the per-title burst the emission-budget tests pace. Each
    has a positive completed_at -> last_viewed interval (a real, emittable row).
    Returns the library and the sorted tmdb ids."""
    tmdb_ids = list(range(base_tmdb, base_tmdb + count))
    for i, tmdb in enumerate(tmdb_ids):
        await _movie(
            sm,
            tmdb_id=tmdb,
            title=f"Budget Movie {i}",
            library_path=_movie_file(tmp_path, f"Budget {base_tmdb}-{i}.mkv"),
            completed_at=_STALE - timedelta(days=2),
        )
    library = FakeLibrary(
        watch_states={
            (tmdb, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)
            for tmdb in tmdb_ids
        }
    )
    return library, tmdb_ids


def _telemetry_only(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if r.name == _TELEMETRY_LOGGER]


def _per_title_rows(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if "completed_to_last_watch=" in r.getMessage()]


def _aggregate_rows(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if "eligible eviction candidate(s)" in r.getMessage()]


async def _run_budget_sweep(
    sm: SessionMaker,
    tmp_path: Path,
    library: FakeLibrary,
    *,
    free_slots: Callable[[], int] | None,
) -> None:
    # free_slots=None is identical to omitting the argument (the sweep's default),
    # so this drives the None-accessor fallback path directly.
    async with sm() as session:
        await retention_telemetry_service.run_retention_telemetry_sweep(
            session=session,
            library=library,
            fs=_local_fs(tmp_path),
            media_type="movie",
            root_path=str(tmp_path),
            grace_days=_GRACE_DAYS,
            threshold_pct=_THRESHOLD,
            target_pct=_TARGET,
            now=_NOW,
            free_slots=free_slots,
        )


async def test_occupied_queue_shrinks_the_emission_budget_and_defers_overflow(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-6 finding: the static per-sweep budget bounds a burst against an
    EMPTY queue, but an ambient backlog already occupying the ``LogCaptureHandler``
    queue leaves less live headroom -- a full-budget burst then overruns it and is
    dropped silently while the dedupe cache marks the newest rows emitted. A live
    ``free_slots`` accessor shrinks the effective budget to the queue's REAL
    headroom minus the safety margin: overflow is deferred UN-cached (retried next
    tick) and nothing is cached that was not actually emitted."""
    # Static budget large enough to emit all five; the LIVE headroom is what must
    # shrink it. margin 2, free_slots()==5 -> effective = max(0, 5-2) = 3.
    monkeypatch.setattr(retention_telemetry_service, "_PER_SWEEP_EMISSION_BUDGET", 10)
    monkeypatch.setattr(retention_telemetry_service, "_QUEUE_SAFETY_MARGIN", 2)
    library, tmdb_ids = await _burst_setup(sessionmaker_, tmp_path, base_tmdb=201)

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        # Occupied queue: only 5 slots free -> effective budget 3, below the static 10.
        await _run_budget_sweep(sessionmaker_, tmp_path, library, free_slots=lambda: 5)
        first = _telemetry_only(caplog.records)
        assert len(_per_title_rows(first)) == 3  # shrunk by live headroom, not the static 10
        assert "deferred_rows=2" in _aggregate_rows(first)[-1].getMessage()
        # Next tick, queue drained (generous headroom): the deferred remainder emits,
        # the already-cached three stay deduped -> nothing cached that wasn't emitted.
        await _run_budget_sweep(sessionmaker_, tmp_path, library, free_slots=lambda: 10_000)

    per_title = _per_title_rows(_telemetry_only(caplog.records))
    # All five emitted exactly once across the two sweeps: the deferred tail was
    # UN-cached (so sweep 2 picked it up) and the emitted three were cached (so
    # sweep 2 never re-sent them). No loss, no duplication.
    assert len(per_title) == 5
    emitted: list[int] = []
    for r in per_title:
        value = getattr(r, "tmdb_id", None)  # set via extra={}, not a static attr
        assert isinstance(value, int)
        emitted.append(value)
    assert sorted(emitted) == tmdb_ids
    assert "deferred_rows=0" in _aggregate_rows(_telemetry_only(caplog.records))[-1].getMessage()


async def test_empty_queue_leaves_the_static_budget_in_force(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty (or nearly-empty) queue must not shrink the budget below the static
    ``_PER_SWEEP_EMISSION_BUDGET``: with generous free_slots the effective budget is
    exactly the static budget (the ``min`` clamps the ample headroom back down)."""
    monkeypatch.setattr(retention_telemetry_service, "_PER_SWEEP_EMISSION_BUDGET", 3)
    library, _tmdb_ids = await _burst_setup(sessionmaker_, tmp_path, base_tmdb=301)
    # An empty handler queue reports QUEUE_MAXSIZE free; with the default 500 margin
    # that is 1500 headroom, far above the static budget of 3.
    empty_queue_free = log_capture_service.QUEUE_MAXSIZE

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        await _run_budget_sweep(
            sessionmaker_, tmp_path, library, free_slots=lambda: empty_queue_free
        )

    first = _telemetry_only(caplog.records)
    assert len(_per_title_rows(first)) == 3  # the static budget, not the 1500 headroom
    assert "deferred_rows=2" in _aggregate_rows(first)[-1].getMessage()


async def test_none_free_slots_accessor_falls_back_to_the_static_budget(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-shot / test call with no live handler (``free_slots=None``) uses the
    static ``_PER_SWEEP_EMISSION_BUDGET`` unchanged: the live-headroom shrink is
    opt-in via the accessor, never required. A deliberately huge safety margin --
    which WOULD force the effective budget to 0 if free_slots were consulted --
    proves the None path never touches the free-slot math at all."""
    monkeypatch.setattr(retention_telemetry_service, "_PER_SWEEP_EMISSION_BUDGET", 3)
    monkeypatch.setattr(retention_telemetry_service, "_QUEUE_SAFETY_MARGIN", 10_000)
    library, _tmdb_ids = await _burst_setup(sessionmaker_, tmp_path, base_tmdb=401)

    with caplog.at_level(logging.INFO, logger=_TELEMETRY_LOGGER):
        await _run_budget_sweep(sessionmaker_, tmp_path, library, free_slots=None)

    first = _telemetry_only(caplog.records)
    # Static budget 3 applies despite the huge margin, because free_slots is None
    # and never consulted (any consulted value minus 10_000 would floor to 0).
    assert len(_per_title_rows(first)) == 3
    assert "deferred_rows=2" in _aggregate_rows(first)[-1].getMessage()
