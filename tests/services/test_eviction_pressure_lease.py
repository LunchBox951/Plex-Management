"""Per-root pressure-exclusion lease (issue #526): operator corrections and
disk-pressure eviction sweeps genuinely serialize.

The family this closes is NOT "a correction claim already exists when the sweep
starts" -- that one was closed by a snapshot check and is pinned over in
``test_eviction_service.py``. It is the START RACE: a correction that BEGINS
inside one of the sweep's await windows (the batched Plex crawl in candidate
assembly, the per-candidate watch-state re-read, the purge primitive's preflight
probes and its unbounded wait for a delete permit) while the sweep is still
selecting and deleting victims against a free-space reading taken BEFORE that
correction existed. Every snapshot-style recheck leaves the next await window
open, so the fix is a reservation with a defeat channel:

* the sweep holds a root-scoped lease from before its first disk probe;
* a correction never queues behind it -- it DEFEATS the lease as it claims its
  path, in the same await-free step;
* the sweep stands down before drawing its next victim, and -- the actual barrier
  -- re-reads the defeat latch at the delete boundary, under the held delete
  permit, with nothing but a synchronous thread start left before the first
  unlinked byte.

Uses the REAL ``LocalFileSystem`` against ``tmp_path`` and the REAL
``report_issue`` verb for the wiring test, so nothing here proves the lease works
only against a hand-rolled imitation of a correction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.domain.disk_usage import DiskUsage
from plex_manager.domain.quality_profile import default_profile
from plex_manager.models import (
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
)
from plex_manager.ports.library import WatchState, WatchStateQuery
from plex_manager.services import correction_service, eviction_service, purge_service
from plex_manager.services.library_roots import LibraryRoots
from plex_manager.services.log_capture_service import EVICTION_TELEMETRY_LOGGER_NAME
from tests.web.fakes import FakeLibrary, FakeProwlarr, FakeQbittorrent, candidate

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime.now(UTC)
_GRACE_DAYS = 30
_STALE = _NOW - timedelta(days=_GRACE_DAYS + 10)

# The static literal ``report_issue`` supplies to ``revoke_pressure_exclusions``.
# Re-stated here (rather than imported) so a silent reword of the operator-facing
# reason has to be made deliberately in both places.
_CORRECTION_REASON = "an operator report-issue correction claimed a path under it"


@pytest.fixture(autouse=True)
def no_leaked_lease_or_claim() -> Iterator[None]:
    """Fail loudly on process-global state a failing test left behind.

    Both registries outlive the event loop, so a leaked lease would absorb the
    next test's revocation and a leaked purge claim would deny its acquisition --
    the exact cross-test coupling that makes a serialization bug look flaky.

    The lease half asserts on the REGISTRY, not on a probe revocation: leases own
    arbitrary ownership PREDICATES, and no single probe path can match every shape
    one might have (``"/"`` matches neither a ``startswith("/media/tv/")`` closure
    nor ``_owned_by_root`` against a ``tmp_path`` root), so a probe-based check
    would pass while a leak sat in the list.
    """
    yield
    leaked_leases = list(
        purge_service._PRESSURE_EXCLUSION_LEASES  # pyright: ignore[reportPrivateUsage]
    )
    # Clear before asserting, so ONE leaking test fails alone instead of poisoning
    # every test that follows it.
    purge_service._PRESSURE_EXCLUSION_LEASES.clear()  # pyright: ignore[reportPrivateUsage]
    assert leaked_leases == [], "a test leaked a pressure-exclusion lease"
    assert purge_service.active_purge_paths() == (), "a test leaked a purge claim"


def _start_correction_purge(library_path: str) -> None:
    """Do exactly what ``report_issue``'s claim site does, verbatim and in order.

    Claim the path, then defeat every lease covering it -- both synchronous, with
    no ``await`` between them, which is what makes the pair atomic on the single
    event loop. ``test_report_issue_defeats_a_held_pressure_exclusion_lease``
    below proves the real verb still does this, so this helper can never drift
    into testing an imitation.
    """
    assert purge_service.begin_purge(library_path)
    purge_service.revoke_pressure_exclusions(library_path, reason=_CORRECTION_REASON)


async def _available_movie(sm: SessionMaker, *, tmdb_id: int, title: str, library_path: str) -> int:
    async with sm() as session:
        row = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.movie,
            title=title,
            status=RequestStatus.available,
            library_path=library_path,
        )
        session.add(row)
        await session.commit()
        return row.id


def _movie_file(root: Path, name: str, size: int = 1024) -> str:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0" * size)
    return str(path)


class _CorrectionStartingLibrary(FakeLibrary):
    """A :class:`FakeLibrary` that starts an operator correction INSIDE one of the
    sweep's Plex awaits, exactly once.

    ``during`` picks the window: ``"assembly"`` fires on the batched
    ``resolve_watch_states`` crawl (candidate assembly -- before any row is
    claimed), ``"reclaim"`` on the per-candidate ``watch_state`` re-read that
    ``_evict_one`` performs immediately before its claim CAS (so the sweep is
    already committed to this victim and only the delete boundary is left).
    """

    def __init__(
        self,
        *,
        during: Literal["assembly", "reclaim"],
        on_window: Callable[[], None],
        watch_states: dict[tuple[int, str, int | None], WatchState] | None = None,
    ) -> None:
        super().__init__(watch_states=watch_states)
        self._during = during
        self._on_window = on_window
        self.fired = False

    def _maybe_fire(self, window: Literal["assembly", "reclaim"]) -> None:
        if window == self._during and not self.fired:
            self.fired = True
            self._on_window()

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        season: int | None = None,
        library_path: str | None = None,
    ) -> WatchState:
        # ``resolve_watch_states`` funnels through here too, so only the calls
        # NOT made by the batch (i.e. the per-candidate re-read) count as the
        # "reclaim" window -- the flag below is set for the batch's duration.
        if not self._in_batch:
            self._maybe_fire("reclaim")
        return await super().watch_state(
            tmdb_id, media_type, season=season, library_path=library_path
        )

    _in_batch = False

    async def resolve_watch_states(self, queries: Sequence[WatchStateQuery]) -> list[WatchState]:
        self._maybe_fire("assembly")
        self._in_batch = True
        try:
            return await super().resolve_watch_states(queries)
        finally:
            self._in_batch = False


async def _sweep_one_movie_root(
    sm: SessionMaker,
    *,
    root: Path,
    library: FakeLibrary,
    proactive: bool = False,
    on_stood_down: Callable[[str], None] | None = None,
) -> list[eviction_service.EvictionOutcome]:
    """One pressure-triggered movie sweep over ``root`` with the gate wide open
    (``threshold_pct=0.0``), so only a serialization decision can spare anything."""
    async with sm() as session:
        return await eviction_service.run_eviction_sweep(
            session=session,
            library=library,
            fs=LocalFileSystem(library_roots=[str(root)]),
            media_type="movie",
            root_path=str(root),
            threshold_pct=101.0 if proactive else 0.0,
            target_pct=0.0,
            grace_days=_GRACE_DAYS,
            proactive=proactive,
            on_stood_down=on_stood_down,
        )


# --------------------------------------------------------------------------- #
# THE START RACE (issue #526) -- a correction beginning inside a sweep await
# --------------------------------------------------------------------------- #


async def test_the_sweep_evicts_the_victim_when_no_correction_starts(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """CONTROL for the two race tests below: with nothing to serialize against,
    this exact fixture really does delete the victim.

    Without it, "the victim survived" would be worthless evidence -- a broken
    sweep that evicts nothing passes every serialization assertion trivially."""
    root = tmp_path / "movies"
    victim = _movie_file(root, "Otherwise Evictable (2020).mkv")
    request_id = await _available_movie(
        sessionmaker_, tmdb_id=7001, title="Otherwise Evictable", library_path=victim
    )
    library = FakeLibrary(
        watch_states={(7001, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    outcomes = await _sweep_one_movie_root(sessionmaker_, root=root, library=library)

    assert [o.title for o in outcomes] == ["Otherwise Evictable"]
    assert not Path(victim).exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.status is RequestStatus.evicted


async def test_a_correction_starting_inside_the_assembly_await_spares_the_victim(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """THE RACE, reproduced at its original shape (issue #526).

    The sweep probes disk, then AWAITS Plex to assemble candidates. A report-issue
    correction begins inside that await and claims a path under the SAME root. The
    pre-#526 guard had already read ``active_purge_paths()`` and moved on, so the
    sweep went right on ranking and deleting victims chosen to satisfy a
    free-space reading the correction was about to change -- watched titles the
    operator need never have lost.

    The lease makes that impossible: the correction defeats it as it claims, and
    the sweep stands down before drawing its first victim. Nothing is silently
    skipped -- the stand-down is logged and the next tick re-reads pressure."""
    root = tmp_path / "movies"
    victim = _movie_file(root, "Otherwise Evictable (2020).mkv")
    correction_path = _movie_file(root, "Correction In Progress (2019).mkv")
    request_id = await _available_movie(
        sessionmaker_, tmdb_id=7001, title="Otherwise Evictable", library_path=victim
    )
    library = _CorrectionStartingLibrary(
        during="assembly",
        on_window=lambda: _start_correction_purge(correction_path),
        watch_states={(7001, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
    )

    with caplog.at_level(logging.INFO, logger=EVICTION_TELEMETRY_LOGGER_NAME):
        try:
            outcomes = await _sweep_one_movie_root(sessionmaker_, root=root, library=library)
        finally:
            purge_service.end_purge(correction_path)

    assert library.fired, "the correction never landed inside the assembly await"
    assert outcomes == []
    assert Path(victim).exists(), "evicted against a reading the correction invalidated"
    assert Path(correction_path).exists()
    # Stood down BEFORE drawing the victim, not merely at the delete boundary: the
    # only watch-state call is candidate assembly's batch, so ``_evict_one`` -- whose
    # first act is a per-candidate re-read -- was never entered and no row was ever
    # claimed and restored for nothing.
    assert len(library.watch_state_calls) == 1
    # Honesty over silence: a stand-down is a state the operator can see, never a
    # quiet no-op tick.
    assert any(
        "standing down" in record.message and _CORRECTION_REASON in record.getMessage()
        for record in caplog.records
        if record.name == EVICTION_TELEMETRY_LOGGER_NAME
    )
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.status is RequestStatus.available


async def test_a_correction_starting_before_assembly_defers_without_crawling_plex(
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A defeat landing between the lease and candidate assembly must DEFER, not
    assemble-then-stand-down.

    The lease is taken before the sweep's very first disk probe, so a correction
    can begin inside that probe (or the crash-recovery pass right after it) --
    after the point where an "is a claim registered?" question was ever asked. The
    sweep then already knows its reading is dead, and paying for a Plex crawl plus
    an ``os.walk`` per row before discarding the result at the first victim would
    be pure waste on the very tick the operator is waiting on."""
    root = tmp_path / "movies"
    victim = _movie_file(root, "Otherwise Evictable (2020).mkv")
    correction_path = _movie_file(root, "Correction In Progress (2019).mkv")
    await _available_movie(
        sessionmaker_, tmdb_id=7006, title="Otherwise Evictable", library_path=victim
    )
    library = FakeLibrary(
        watch_states={(7006, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    real_read_disk_usage = eviction_service.read_disk_usage
    fired = False

    def _probe_that_starts_a_correction(path: str) -> DiskUsage:
        nonlocal fired
        if not fired:
            fired = True
            _start_correction_purge(correction_path)
        return real_read_disk_usage(path)

    monkeypatch.setattr(eviction_service, "read_disk_usage", _probe_that_starts_a_correction)

    with caplog.at_level(logging.INFO, logger=EVICTION_TELEMETRY_LOGGER_NAME):
        try:
            outcomes = await _sweep_one_movie_root(sessionmaker_, root=root, library=library)
        finally:
            purge_service.end_purge(correction_path)

    assert fired
    assert outcomes == []
    assert Path(victim).exists()
    assert library.watch_state_calls == [], "candidate assembly ran for a dead reading"
    assert any(
        "deferred" in record.message and _CORRECTION_REASON in record.getMessage()
        for record in caplog.records
        if record.name == EVICTION_TELEMETRY_LOGGER_NAME
    )


async def test_a_correction_starting_after_the_claim_is_revoked_at_the_delete_boundary(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """THE BARRIER: the deepest await window, past every cheap recheck.

    Here the correction starts during ``_evict_one``'s per-candidate watch-state
    re-read -- AFTER the sweep's per-victim lease check and immediately before its
    claim CAS. The row is genuinely claimed ``evicted`` and the purge primitive
    runs its containment and reclaimable-bytes preflight; only the delete-boundary
    hook, under the held delete permit, is left to notice. It does: no delete
    starts, the incomplete-delete marker is never armed (so recovery has nothing
    to converge), and the claim is released back to ``available`` the same way a
    refused delete releases it -- never stranded ``evicted`` over a live file."""
    root = tmp_path / "movies"
    victim = _movie_file(root, "Claimed Then Spared (2020).mkv")
    correction_path = _movie_file(root, "Correction In Progress (2019).mkv")
    request_id = await _available_movie(
        sessionmaker_, tmdb_id=7002, title="Claimed Then Spared", library_path=victim
    )
    library = _CorrectionStartingLibrary(
        during="reclaim",
        on_window=lambda: _start_correction_purge(correction_path),
        watch_states={(7002, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
    )

    try:
        outcomes = await _sweep_one_movie_root(sessionmaker_, root=root, library=library)
    finally:
        purge_service.end_purge(correction_path)

    assert library.fired, "the correction never landed inside the pre-claim re-read"
    assert outcomes == []
    assert Path(victim).exists(), "bytes left disk after a correction had already claimed"
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        evicted_history = (
            (
                await session.execute(
                    select(DownloadHistory).where(
                        DownloadHistory.event_type == DownloadHistoryEvent.evicted
                    )
                )
            )
            .scalars()
            .all()
        )
    assert row is not None
    assert row.status is RequestStatus.available
    assert row.library_path == victim
    assert row.partial_delete_path is None, "a delete that never started must arm no marker"
    assert evicted_history == []


async def test_a_defeat_at_the_final_victim_is_still_reported_by_the_sweep(
    sessionmaker_: SessionMaker, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The edge the per-victim checks structurally cannot see.

    Both eviction loops consult the lease BEFORE drawing a victim, so a defeat
    that lands during the LAST candidate's delete boundary ends them by
    EXHAUSTION -- and with an empty extra pool the second loop never runs at all.
    Nothing in the loops ever observes the defeat, so without the trailing
    recorder the sweep's summary would say "0 evicted" as though nothing had been
    eligible, and the operator who pressed the button would be told nothing at
    all. One candidate + ``target_pct=0`` is exactly that shape: everything is
    selected, so the extra pool is empty."""
    root = tmp_path / "movies"
    victim = _movie_file(root, "Last Victim (2020).mkv")
    correction_path = _movie_file(root, "Correction In Progress (2019).mkv")
    await _available_movie(sessionmaker_, tmdb_id=7007, title="Last Victim", library_path=victim)
    library = _CorrectionStartingLibrary(
        during="reclaim",
        on_window=lambda: _start_correction_purge(correction_path),
        watch_states={(7007, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
    )
    reported: list[str] = []

    with caplog.at_level(logging.INFO, logger=EVICTION_TELEMETRY_LOGGER_NAME):
        try:
            outcomes = await _sweep_one_movie_root(
                sessionmaker_, root=root, library=library, on_stood_down=reported.append
            )
        finally:
            purge_service.end_purge(correction_path)

    assert library.fired
    assert outcomes == []
    assert Path(victim).exists()
    # Reported exactly once, to the caller AND to telemetry -- never inferred by
    # the operator from a bare "0 evicted".
    assert reported == [_CORRECTION_REASON]
    assert (
        sum(
            "standing down" in record.message
            for record in caplog.records
            if record.name == EVICTION_TELEMETRY_LOGGER_NAME
        )
        == 1
    )


async def test_the_stand_down_is_reported_to_the_caller_at_most_once(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """``on_stood_down`` feeds one ``EvictResponse.stood_down`` entry per root, so
    a sweep that both stops a loop AND falls through the trailing recorder must
    not report the same defeat twice -- two victims make both paths reachable in
    the one run."""
    root = tmp_path / "movies"
    first = _movie_file(root, "First Victim (2020).mkv", size=2048)
    second = _movie_file(root, "Second Victim (2021).mkv")
    correction_path = _movie_file(root, "Correction In Progress (2019).mkv")
    await _available_movie(sessionmaker_, tmdb_id=7008, title="First Victim", library_path=first)
    await _available_movie(sessionmaker_, tmdb_id=7009, title="Second Victim", library_path=second)
    library = _CorrectionStartingLibrary(
        during="reclaim",
        on_window=lambda: _start_correction_purge(correction_path),
        watch_states={
            (7008, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
            (7009, "movie", None): WatchState(watched=True, last_viewed_at=_STALE),
        },
    )
    reported: list[str] = []

    try:
        outcomes = await _sweep_one_movie_root(
            sessionmaker_, root=root, library=library, on_stood_down=reported.append
        )
    finally:
        purge_service.end_purge(correction_path)

    assert library.fired
    assert outcomes == []
    assert Path(first).exists()
    assert Path(second).exists(), "the second victim was drawn after the defeat"
    assert reported == [_CORRECTION_REASON]


# --------------------------------------------------------------------------- #
# The bounds: root scope, and pressure-triggered sweeps only
# --------------------------------------------------------------------------- #


async def test_a_correction_under_another_root_never_defeats_this_roots_lease(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The lease is ROOT-SCOPED, decided by the same deepest-containing-root rule
    candidate assembly uses. A correction on the anime root must not stall
    reclamation on a genuinely full movies root -- pressure nothing in flight is
    ever going to relieve."""
    root = tmp_path / "movies"
    other_root = tmp_path / "movies-anime"
    victim = _movie_file(root, "Pressured Root Victim (2020).mkv")
    correction_path = _movie_file(other_root, "Corrected Elsewhere (2019).mkv")
    request_id = await _available_movie(
        sessionmaker_, tmdb_id=7003, title="Pressured Root Victim", library_path=victim
    )
    library = _CorrectionStartingLibrary(
        during="assembly",
        on_window=lambda: _start_correction_purge(correction_path),
        watch_states={(7003, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
    )

    try:
        async with sessionmaker_() as session:
            outcomes = await eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=LocalFileSystem(library_roots=[str(root), str(other_root)]),
                media_type="movie",
                root_path=str(root),
                all_roots=[str(root), str(other_root)],
                threshold_pct=0.0,
                target_pct=0.0,
                grace_days=_GRACE_DAYS,
            )
    finally:
        purge_service.end_purge(correction_path)

    assert library.fired
    assert [o.title for o in outcomes] == ["Pressured Root Victim"]
    assert not Path(victim).exists()
    assert Path(correction_path).exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.status is RequestStatus.evicted


async def test_a_proactive_sweep_takes_no_lease_and_a_correction_start_cannot_stop_it(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The other bound: the lease guards pressure ARITHMETIC, and a proactive
    sweep has none.

    It clears every past-grace, watched, un-pinned candidate on principle -- no
    threshold, no target to overshoot -- so a correction freeing space cannot make
    any of them ineligible and there is no reading to invalidate. The correction's
    OWN path stays protected in both modes by the per-path purge claim."""
    root = tmp_path / "movies"
    victim = _movie_file(root, "Proactive Victim (2020).mkv")
    correction_path = _movie_file(root, "Correction In Progress (2019).mkv")
    request_id = await _available_movie(
        sessionmaker_, tmdb_id=7004, title="Proactive Victim", library_path=victim
    )
    library = _CorrectionStartingLibrary(
        during="assembly",
        on_window=lambda: _start_correction_purge(correction_path),
        watch_states={(7004, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)},
    )

    try:
        outcomes = await _sweep_one_movie_root(
            sessionmaker_, root=root, library=library, proactive=True
        )
    finally:
        purge_service.end_purge(correction_path)

    assert library.fired
    assert [o.title for o in outcomes] == ["Proactive Victim"]
    assert not Path(victim).exists()
    assert Path(correction_path).exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.status is RequestStatus.evicted


async def test_a_sweep_releases_its_lease_so_a_later_correction_finds_nothing_to_defeat(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """A leaked lease would silently absorb every later correction's revocation
    for the life of the process, so the release has to happen on the ORDINARY exit
    too, not just the interesting ones."""
    root = tmp_path / "movies"
    _movie_file(root, "Unwatched (2020).mkv")
    await _available_movie(
        sessionmaker_,
        tmdb_id=7005,
        title="Unwatched",
        library_path=str(root / "Unwatched (2020).mkv"),
    )

    await _sweep_one_movie_root(sessionmaker_, root=root, library=FakeLibrary())

    assert purge_service.revoke_pressure_exclusions(str(root), reason="post-sweep check") == ()


# --------------------------------------------------------------------------- #
# Wiring: the REAL report-issue verb defeats a held lease
# --------------------------------------------------------------------------- #


async def test_report_issue_defeats_a_held_pressure_exclusion_lease(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """The lease is only worth anything if the real correction verb actually
    defeats it as it claims its path.

    Runs the whole ``report_issue`` flow (blocklist -> remove torrent -> purge ->
    scan -> re-arm -> audit -> inline re-grab) against a lease held over the same
    root, and asserts the lease came back defeated -- with the correction NOT
    having waited for it, which is the point: an operator pressing a button must
    never queue behind a Plex crawl and several multi-GB rmtrees."""
    root = tmp_path / "movies"
    root.mkdir()
    movie_file = root / "Some Movie (2020).mkv"
    movie_file.write_bytes(b"x" * 4096)
    culprit = "3" * 40
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.available,
            library_path=str(movie_file),
        )
        session.add(request)
        await session.flush()
        session.add(
            Download(
                torrent_hash=culprit,
                status="imported",
                media_request_id=request.id,
                tmdb_id=603,
                year=2020,
            )
        )
        await session.commit()
        request_id = request.id

    all_roots = (str(root),)
    lease = purge_service.acquire_pressure_exclusion(
        label=str(root),
        owns_path=lambda path: eviction_service._owned_by_root(  # pyright: ignore[reportPrivateUsage]
            path, str(root), all_roots
        ),
    )
    assert lease is not None
    assert lease.revoked_reason is None

    try:
        async with sessionmaker_() as session:
            await correction_service.report_issue(
                session,
                FakeQbittorrent(),
                LocalFileSystem(library_roots=[str(root)]),
                FakeLibrary(),
                FakeProwlarr(
                    [candidate("Some.Movie.2020.1080p.WEB-DL.x264-OTHER", info_hash="a" * 40)]
                ),
                GuessitParser(),
                default_profile(),
                request_id=request_id,
                reason="bad_quality",
                season=None,
                roots=LibraryRoots(movies=str(root)),
            )
    finally:
        purge_service.release_pressure_exclusion(lease)

    assert lease.revoked_reason == _CORRECTION_REASON
    assert not movie_file.exists()


# --------------------------------------------------------------------------- #
# The primitive itself
# --------------------------------------------------------------------------- #


def test_acquire_is_denied_while_a_covered_purge_claim_is_already_registered() -> None:
    """The acquisition half: an existing claim is an honest refusal up front, not
    a lease that would have to be revoked a microsecond later."""
    purge_service.begin_purge("/media/movies/Already Correcting (2019)")
    try:
        denied = purge_service.acquire_pressure_exclusion(
            label="/media/movies",
            owns_path=lambda path: path.startswith("/media/movies/"),
        )
        assert denied is None
    finally:
        purge_service.end_purge("/media/movies/Already Correcting (2019)")

    granted = purge_service.acquire_pressure_exclusion(
        label="/media/movies", owns_path=lambda path: path.startswith("/media/movies/")
    )
    assert granted is not None
    purge_service.release_pressure_exclusion(granted)


def test_revocation_is_a_latch_that_keeps_the_reason_that_ended_the_authority() -> None:
    """A second correction must not overwrite the reason the holder is going to
    log: the FIRST defeat is the one that actually ended its authority."""
    lease = purge_service.acquire_pressure_exclusion(
        label="/media/tv", owns_path=lambda path: path.startswith("/media/tv/")
    )
    assert lease is not None
    try:
        assert purge_service.revoke_pressure_exclusions("/media/tv/A", reason="first") == (
            "/media/tv",
        )
        assert purge_service.revoke_pressure_exclusions("/media/tv/B", reason="second") == ()
        assert lease.revoked_reason == "first"
    finally:
        purge_service.release_pressure_exclusion(lease)


def test_release_is_idempotent_and_leaves_identical_leases_independent() -> None:
    """Leases are identity-keyed, not value-keyed: two leases over the same root
    with the same predicate are still two distinct reservations, and releasing one
    twice must not silently drop the other."""

    def owns(path: str) -> bool:
        return path.startswith("/media/tv/")

    first = purge_service.acquire_pressure_exclusion(label="/media/tv", owns_path=owns)
    second = purge_service.acquire_pressure_exclusion(label="/media/tv", owns_path=owns)
    assert first is not None
    assert second is not None

    purge_service.release_pressure_exclusion(first)
    purge_service.release_pressure_exclusion(first)  # idempotent, not a crash
    try:
        assert purge_service.revoke_pressure_exclusions("/media/tv/A", reason="only") == (
            "/media/tv",
        )
        assert first.revoked_reason is None
        assert second.revoked_reason == "only"
    finally:
        purge_service.release_pressure_exclusion(second)
