"""Auto-grab worker — turn approved requests into searches + grabs, unattended.

The background automation spine (ADR-0013, issue #27). A request created via
``POST /requests`` lands as ``pending`` (movies) or a set of ``pending``
``SeasonRequest`` rows (TV) and, before this worker, sat there forever: nothing
ever searched Prowlarr or grabbed. :func:`run_grab_cycle` closes that loop by
scanning the requests/seasons that are DUE for a search and, for each, reusing the
EXACT same brains the manual "Grab" button uses -- :func:`decision_service.preview`
(indexer search -> pure decision engine) then :func:`grab_service.grab` -- so the
manual and automatic paths can never diverge. This module writes NO new decision
logic of its own.

Design decisions (see ADR-0013):

* **Direct per-request search, not RSS sync.** At single-user beta scale a handful
  of pending scopes is cheap to search directly; RSS-feed matching exists to avoid
  per-title searches across hundreds of titles and many indexers, which we don't
  have. A per-scope backoff ladder keeps the actual Prowlarr calls bounded.
* **Never give up.** A search that finds nothing acceptable parks the scope at the
  honest, retryable ``no_acceptable_release`` state and schedules the next search
  on an escalating backoff (:data:`BACKOFF_SCHEDULE`, then 24h forever). A new
  release may appear at any time, so the worker keeps trying indefinitely rather
  than dead-ending like the prototype's 5-nights-then-stuck cron.
* **Honesty over silence.** "searched OK, nothing acceptable" (park +
  backoff) is kept strictly distinct from operational failures. A RAISED search
  (Prowlarr down / rate-limited) leaves the scope untouched and propagates so the
  loop records it on the ``AutograbStatus`` health signal and backs the whole
  cycle off. A ``GrabError`` (qBittorrent accepted the torrent but no info-hash
  could be derived, leaving a live untracked torrent) is likewise operational:
  the scope is left untouched and the error surfaced on ``AutograbStatus`` -- but
  the cycle CONTINUES (one bad grab is not a Prowlarr outage). A scope is never
  falsely marked ``no_acceptable_release`` just because a grab or search failed.
* **Protect the single Prowlarr.** At most :data:`AUTO_GRAB_MAX_SEARCHES_PER_CYCLE`
  actual searches run per cycle, processed sequentially; a scope that already has
  an active download is skipped BEFORE it costs a search (and never races
  ``grab_service``'s one-active guard).
* **Cool a scope whose GRAB keeps failing (never park it).** A scope that keeps
  raising ``GrabError`` (releases exist; the grab pipeline is what's broken) must
  not be parked ``no_acceptable_release`` -- that would LIE -- yet, being eager
  ``pending``/``searching``, it ignores the DB backoff and would consume the whole
  per-cycle budget every tick, starving other scopes. An IN-PROCESS per-scope
  cooldown (:data:`COOLDOWN_SCHEDULE`, fed ONLY by ``GrabError``, cleared on any
  other resolution) skips such a scope for the window so the budget flows to
  healthy scopes; the count of cooling scopes is surfaced on ``AutograbStatus``
  (honesty over silence). It is not persisted -- a restart clears it, exactly like
  the ``AutograbStatus`` health record.
* **Park from a fresh clock read.** The backoff for a nothing-acceptable park is
  scheduled from the ACTUAL park moment (a fresh clock read inside :func:`_park`),
  never a ``now`` captured at cycle start -- a slow cycle would otherwise schedule
  a late park from a stale base and make it due again on the very next tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import (
    decision_service,
    grab_service,
    request_service,
    season_request_service,
)
from plex_manager.services.grab_service import (
    AlreadyDownloadingError,
    DownloadScopeConflictError,
    GrabError,
    NoGrabSourceError,
    RequestNotActiveError,
    SeasonRequiredError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.indexer import IndexerPort
    from plex_manager.ports.parser import ParserPort

__all__ = [
    "AUTO_GRAB_MAX_SEARCHES_PER_CYCLE",
    "BACKOFF_SCHEDULE",
    "COOLDOWN_SCHEDULE",
    "DUE_SEARCH_STATUSES",
    "MAX_GRAB_ATTEMPTS_PER_SCOPE",
    "AutograbCycleResult",
    "CooldownRegistry",
    "ScopeCooldown",
    "ScopeKey",
    "cooldown_delay",
    "next_search_at",
    "run_grab_cycle",
]

_logger = logging.getLogger(__name__)

# Request/season statuses the worker re-searches. ``pending`` (never searched),
# ``no_acceptable_release`` (searched, nothing acceptable -> retry on backoff), and
# ``searching`` (re-armed after a download failed -- ``queue_service._handle_failed``).
# Terminal / in-flight statuses (``downloading``/``completed``/``available``/
# ``failed``/``import_blocked``/``partially_available``/``evicted``) are deliberately
# excluded so the worker never resurrects a finished scope or double-grabs one
# already downloading.
DUE_SEARCH_STATUSES: frozenset[str] = frozenset({"pending", "no_acceptable_release", "searching"})

# Escalating per-scope backoff after a nothing-acceptable search (borrowed from
# Radarr's indexer backoff ladder, coarsened for a single Prowlarr). The Nth
# nothing-acceptable search schedules the next one ``BACKOFF_SCHEDULE[min(N-1,
# last)]`` out; once the ladder is exhausted the last entry (24h) repeats FOREVER
# -- the worker never gives up (a new release may always appear). See
# :func:`next_search_at`.
BACKOFF_SCHEDULE: tuple[timedelta, ...] = (
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=3),
    timedelta(hours=6),
    timedelta(hours=12),
    timedelta(hours=24),
)

# At most this many ACTUAL Prowlarr searches per cycle -- the single-Prowlarr load
# guard. A scope skipped for an active download does NOT consume this budget (no
# search happened). A module constant for the beta (a web-config knob is a noted
# follow-up), mirroring ``web/app.py``'s interval constants.
AUTO_GRAB_MAX_SEARCHES_PER_CYCLE: int = 5

# At most this many GRAB attempts per scope in one cycle. When the top-ranked
# accepted release can't be grabbed for a PER-RELEASE reason (no usable source, or
# its hash is already active for a different scope), the worker falls through to the
# next-ranked accepted release rather than parking a still-grabbable scope behind
# backoff. Bounded so a scope whose every candidate is ungrabbable can't monopolise
# the grab loop (or hammer qBittorrent's add). This caps GRAB attempts ONLY -- the
# per-cycle Prowlarr SEARCH cap (:data:`AUTO_GRAB_MAX_SEARCHES_PER_CYCLE`) is
# unaffected: one scope still costs exactly one search no matter how many of its
# accepted releases are tried.
MAX_GRAB_ATTEMPTS_PER_SCOPE: int = 3

# --------------------------------------------------------------------------- #
# In-process grab-pipeline cooldown (Codex PR #31 round-3 #2)
# --------------------------------------------------------------------------- #
# A scope whose GRAB keeps raising ``GrabError`` (qBittorrent accepted the torrent
# but no info-hash could be derived) must NOT be parked ``no_acceptable_release`` --
# that would LIE (releases exist; the grab PIPELINE is what's broken) -- and it
# cannot lean on the per-scope DB backoff either, because ``pending``/``searching``
# are deliberately EAGER (they ignore ``next_search_at``; ADR-0013 §3). Left alone,
# such a scope stays due-now every tick and, because a ``GrabError`` still costs a
# search, consumes the whole per-cycle search budget forever, starving every other
# scope. The guard is an IN-PROCESS, per-scope cooldown: a registry mapping each
# failing scope to its consecutive-failure count + the instant it may be retried.
# The window escalates per consecutive ``GrabError`` (below, then its last rung
# forever) and is CLEARED the moment the scope resolves any OTHER way (a grab, a
# park, or a scope-level settle) -- ``GrabError`` is its only feeder, so it only ever
# holds actively-failing scopes and is bounded by process lifetime. Deliberately NOT
# persisted: a restart clears it, the same honesty as the ``AutograbStatus`` health
# record. See :func:`run_grab_cycle`.
COOLDOWN_SCHEDULE: tuple[timedelta, ...] = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=60),
)

# A scope's identity for the cooldown registry (and, incidentally, the active-download
# guard): ``(request_id, season)`` -- ``season`` is ``None`` for a movie and the
# season number for a TV season.
ScopeKey = tuple[int, int | None]


@dataclass(frozen=True)
class ScopeCooldown:
    """One scope's live grab-pipeline cooldown: how many consecutive ``GrabError``s
    it has hit and the earliest instant it may be searched again."""

    failures: int
    not_before: datetime


# The in-process registry the worker threads through each tick, OWNED by the caller
# (``app.state`` in production, a fresh dict in tests). A missing key = a healthy
# scope; :func:`run_grab_cycle` falls back to a throwaway dict when the caller wires
# none, so a one-shot call needs no cooldown plumbing.
CooldownRegistry = dict[ScopeKey, ScopeCooldown]


def cooldown_delay(prior_failures: int) -> timedelta:
    """The cooldown window after the (``prior_failures`` + 1)-th consecutive GrabError.

    ``prior_failures`` is the scope's failure count BEFORE this one, so the first
    failure (``0``) waits the first rung and an exhausted ladder repeats its last
    rung (60m) forever -- the same shape as :func:`next_search_at`."""
    return COOLDOWN_SCHEDULE[min(prior_failures, len(COOLDOWN_SCHEDULE) - 1)]


# NULL ``next_search_at`` ("due now") sorts ahead of any real timestamp; this
# tz-aware minimum is its sort stand-in so a never-scheduled scope outranks a
# scheduled-but-overdue one when the per-cycle cap has to choose.
_NULL_DUE_SORT_KEY = datetime.min.replace(tzinfo=UTC)

# The ONLY status whose ``next_search_at`` backoff gate actually applies. A parked
# scope earned its escalating backoff and must wait it out; ``pending`` and
# ``searching`` are EAGER -- always due immediately -- so a scope deliberately
# re-armed to ``searching`` (a failed download; ``queue_service._handle_failed``)
# during a stale 24h backoff window is picked up on the very next tick instead of
# staying suppressed until that stale timestamp expires (ADR-0013 §3). The
# raised-search global cycle abort still protects the single Prowlarr from a burst.
_BACKOFF_GATED_STATUS = "no_acceptable_release"


def _due_sort_key(status: str, next_search_at: datetime | None) -> datetime:
    """Effective due-time for ordering (most-overdue first, then oldest-scheduled).

    A parked (:data:`_BACKOFF_GATED_STATUS`) scope sorts by its scheduled backoff;
    an eager ``pending``/``searching`` scope always sorts due-now
    (:data:`_NULL_DUE_SORT_KEY`) so it is never starved behind parked scopes by a
    stale ``next_search_at`` left over from a prior backoff.
    """
    if status == _BACKOFF_GATED_STATUS and next_search_at is not None:
        return next_search_at
    return _NULL_DUE_SORT_KEY


@dataclass(frozen=True)
class AutograbCycleResult:
    """What one :func:`run_grab_cycle` pass actually did -- for logging and tests.

    ``searched`` counts ACTUAL Prowlarr searches (never more than
    ``max_searches``); ``skipped_active`` counts due scopes passed over because
    they already had an active download (those cost no search).

    ``grab_errors`` counts scopes whose grab hit an OPERATIONAL failure --
    :class:`~plex_manager.services.grab_service.GrabError`: qBittorrent ACCEPTED
    the torrent but no info-hash could be derived, so a LIVE, untracked torrent
    is left with no ``Download`` row. That is NOT "nothing acceptable found", so
    it is counted here (never in ``no_acceptable``) and the scope's state is left
    UNCHANGED (never parked). ``last_grab_error`` carries the representative
    exception so the caller can record it on the ``AutograbStatus`` health signal
    (TYPE name only) and refuse to mark the cycle clean -- the mirror of a raised
    indexer search, except a single bad grab continues the cycle rather than
    aborting it (the torrent reached a reachable qBittorrent; Prowlarr is fine).

    ``cooled_down`` is how many scopes are CURRENTLY inside a grab-pipeline cooldown
    at cycle end (:data:`COOLDOWN_SCHEDULE`) -- the count the caller surfaces on the
    ``AutograbStatus`` health record so the operator can SEE the grab pipeline
    failing (honesty over silence), rather than wondering why eager scopes never
    reach ``downloading``.
    """

    searched: int = 0
    grabbed: int = 0
    no_acceptable: int = 0
    skipped_active: int = 0
    grab_errors: int = 0
    last_grab_error: GrabError | None = None
    cooled_down: int = 0


@dataclass(frozen=True)
class _PendingScope:
    """A due movie request or TV season, normalized for uniform processing.

    ``season``/``season_request_id`` are ``None`` for a movie and set for a TV
    season. ``title``/``year`` are resolved up front for a movie (off its own
    record) and lazily from the parent ``MediaRequest`` for a TV season (whose
    record carries only ``tmdb_id``). ``status`` drives the effective-due sort
    (see :func:`_due_sort_key`): only a parked (``no_acceptable_release``) scope
    sorts by its scheduled backoff; an eager ``pending``/``searching`` scope
    always sorts due-now regardless of any stale ``next_search_at``.
    """

    request_id: int
    tmdb_id: int
    season: int | None
    season_request_id: int | None
    title: str | None
    year: int | None
    status: str
    search_attempts: int
    next_search_at: datetime | None


def next_search_at(now: datetime, prior_attempts: int) -> datetime:
    """The instant to schedule the next search after a nothing-acceptable result.

    ``prior_attempts`` is the scope's search_attempts BEFORE this failure; the
    delay is ``BACKOFF_SCHEDULE[min(prior_attempts, last)]`` so the first failure
    (``prior_attempts == 0``) waits the first rung and an exhausted ladder repeats
    its final rung (24h) forever.
    """
    index = min(prior_attempts, len(BACKOFF_SCHEDULE) - 1)
    return now + BACKOFF_SCHEDULE[index]


def _scope_key(scope: _PendingScope) -> ScopeKey:
    """The scope's cooldown-registry / active-download identity: ``(request_id, season)``."""
    return (scope.request_id, scope.season)


async def _collect_due_scopes(
    request_repo: SqlRequestRepository,
    season_repo: SqlSeasonRequestRepository,
    now: datetime,
    cooldowns: CooldownRegistry,
) -> list[_PendingScope]:
    """Gather every due movie request + TV season, ordered most-overdue-first.

    An eager (``pending``/``searching``) scope sorts due-now ahead of any parked
    scope's scheduled timestamp, then oldest-scheduled first -- so the per-cycle
    cap always spends its budget on the most-overdue scopes AND a deliberately
    re-armed scope is never starved behind parked ones by a stale ``next_search_at``
    (see :func:`_due_sort_key`; the same rule the repositories' ``list_due_for_search``
    apply in SQL).

    A scope carrying a grab-pipeline cooldown entry (:data:`COOLDOWN_SCHEDULE`) sorts
    AFTER every healthy scope regardless of its due-time, so that when its window
    expires it is retried without leaping ahead of scopes that never failed -- it
    must not re-monopolise the budget the moment it becomes eligible again.
    """
    movies = await request_repo.list_due_for_search(DUE_SEARCH_STATUSES, now)
    seasons = await season_repo.list_due_for_search(DUE_SEARCH_STATUSES, now)
    scopes: list[_PendingScope] = [
        _PendingScope(
            request_id=r.id,
            tmdb_id=r.tmdb_id,
            season=None,
            season_request_id=None,
            title=r.title,
            year=r.year,
            status=r.status,
            search_attempts=r.search_attempts,
            next_search_at=r.next_search_at,
        )
        for r in movies
    ]
    scopes.extend(
        _PendingScope(
            request_id=s.media_request_id,
            tmdb_id=s.tmdb_id,
            season=s.season_number,
            season_request_id=s.id,
            title=None,
            year=None,
            status=s.status,
            search_attempts=s.search_attempts,
            next_search_at=s.next_search_at,
        )
        for s in seasons
    )
    scopes.sort(
        key=lambda sc: (_scope_key(sc) in cooldowns, _due_sort_key(sc.status, sc.next_search_at))
    )
    return scopes


async def _park(
    session: AsyncSession,
    request_repo: SqlRequestRepository,
    season_repo: SqlSeasonRequestRepository,
    download_repo: SqlDownloadRepository,
    scope: _PendingScope,
    clock: Callable[[], datetime],
) -> bool:
    """Record a nothing-acceptable result: schedule the backoff + mark the honest
    ``no_acceptable_release`` park state, then commit. Returns ``True`` if it parked,
    ``False`` if a racing active download made parking wrong (see below).

    The SAME honest dead-end the manual ``/queue/grab`` endpoint uses
    (``request_service`` / ``season_request_service.mark_no_acceptable_release``,
    both of which keep the never-un-terminate guard), plus the backoff bookkeeping
    the manual path has no need of. ``search_attempts`` is bumped so the ladder
    escalates; ``next_search_at`` gates the next search.

    Codex round-3 #1 -- park race: a manual grab (or a lower-ranked auto grab) can
    create an active download between this cycle's pre-search active check and this
    park write; parking then would overwrite a LIVE ``downloading`` scope with the
    honest-but-now-wrong ``no_acceptable_release`` dead-end -- a momentary lie the
    reconciler would have to undo. The manual endpoint guards this exact race the
    same way, so mirror it: re-check ``find_active_for_request`` immediately before
    the write and, if a download appeared, skip the park entirely (no status write,
    no backoff bump) and report it un-parked.

    Codex round-3 #3 -- stale park base: the backoff is scheduled from a FRESH clock
    read at the actual park moment, never a ``now`` captured at cycle start. A slow
    cycle (several searches x up to ~120s each) would otherwise schedule a late park
    from a ~10-minute-old base and make it due again on the very next tick instead of
    honouring the first rung.
    """
    active = await download_repo.find_active_for_request(scope.request_id, season=scope.season)
    if active is not None:
        _logger.info(
            "auto-grab: active download appeared before park; leaving scope as-is",
            extra={"request_id": scope.request_id, "season": scope.season},
        )
        return False

    now = clock()
    scheduled_attempts = scope.search_attempts + 1
    scheduled_at = next_search_at(now, scope.search_attempts)
    if scope.season is not None:  # TV season
        if scope.season_request_id is None:  # pragma: no cover - a tv scope always has one
            return False
        await season_repo.schedule_search(
            scope.season_request_id,
            search_attempts=scheduled_attempts,
            next_search_at=scheduled_at,
        )
        # Flush-only; this function owns the commit boundary.
        await season_request_service.mark_no_acceptable_release(
            session, media_request_id=scope.request_id, season_number=scope.season
        )
    else:  # movie
        await request_repo.schedule_search(
            scope.request_id,
            search_attempts=scheduled_attempts,
            next_search_at=scheduled_at,
        )
        # Commits internally (movie path); the extra commit below is then a no-op.
        await request_service.mark_no_acceptable_release(session, scope.request_id)
    await session.commit()
    return True


async def run_grab_cycle(
    session: AsyncSession,
    *,
    prowlarr: IndexerPort,
    parser: ParserPort,
    profile: QualityProfile,
    qbt: DownloadClientPort,
    max_searches: int = AUTO_GRAB_MAX_SEARCHES_PER_CYCLE,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    cooldowns: CooldownRegistry | None = None,
) -> AutograbCycleResult:
    """Run one auto-grab pass: search the due scopes and grab / park each.

    For each due scope (most-overdue first), up to ``max_searches`` ACTUAL
    searches:

    * skip it (no search, no budget) if it is inside a grab-pipeline cooldown --
      it keeps raising ``GrabError`` and would otherwise consume the whole budget
      every tick (see below);
    * skip it (no search) if it already has an active download -- avoids a wasted
      Prowlarr hit and never races ``grab_service``'s one-active guard;
    * else search + decide via :func:`decision_service.preview`; if any release is
      accepted, grab the top pick via :func:`grab_service.grab` (which drives the
      scope to ``downloading`` and commits), falling through to the next-ranked
      accepted release -- bounded by :data:`MAX_GRAB_ATTEMPTS_PER_SCOPE` -- when the
      top pick hits a PER-RELEASE grab failure ({``NoGrabSourceError``,
      ``DownloadScopeConflictError``}); if nothing is acceptable, or every accepted
      release is ungrabbable, park it at ``no_acceptable_release`` and schedule the
      escalating backoff.

    A search that RAISES (Prowlarr unreachable / rate-limited -- the ``IndexerPort``
    contract raises rather than returning ``[]``) is NOT caught here: it propagates
    so the caller (``web/app.py``'s ``_autograb_loop``) records it on the
    ``AutograbStatus`` health signal and backs the whole cycle off. The raising
    scope's state is left untouched (never falsely parked), and any scope already
    processed this cycle keeps its committed result.

    An operational GRAB failure (:class:`~plex_manager.services.grab_service.
    GrabError` -- qBittorrent accepted the torrent but no info-hash could be
    derived) is caught but NOT parked: the scope's state is left untouched and the
    error is returned on :class:`AutograbCycleResult` (``grab_errors`` /
    ``last_grab_error``) so the caller records it on ``AutograbStatus`` and refuses
    to mark the cycle clean. Unlike a raised search it does NOT abort the cycle --
    the remaining due scopes are still processed. The failing scope ALSO enters an
    in-process cooldown (:data:`COOLDOWN_SCHEDULE`, escalating per consecutive
    ``GrabError``, cleared on any other resolution): while cooling it is skipped
    before it costs a search, so it can never monopolise the per-cycle budget and
    starve every other scope (Codex round-3 #2). ``cooldowns`` is that registry,
    OWNED by the caller so it survives across ticks (``app.state`` in production);
    it defaults to a throwaway dict for a one-shot call.

    ``now`` is the cycle-start instant used for DUE SELECTION only (cycle-consistent);
    it defaults to ``datetime.now(UTC)``. ``clock`` is read FRESH at each park /
    cooldown event so a slow cycle schedules from the real event time, not a stale
    cycle-start base (Codex round-3 #3); it defaults to the wall clock. Both are
    injectable for deterministic tests.
    """
    now = now or datetime.now(UTC)
    # Park + cooldown scheduling read the clock FRESH at each event (round-3 #3); the
    # cycle-start ``now`` above stays the single, cycle-consistent basis for DUE
    # selection only. Default: the wall clock; tests inject a controllable clock.
    park_clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
    # In-process grab cooldown registry (round-3 #2); a throwaway dict when the caller
    # wires none, so a one-shot call keeps working with no cooldown state.
    if cooldowns is None:
        cooldowns = {}
    request_repo = SqlRequestRepository(session)
    season_repo = SqlSeasonRequestRepository(session)
    download_repo = SqlDownloadRepository(session)

    scopes = await _collect_due_scopes(request_repo, season_repo, now, cooldowns)

    searched = grabbed = no_acceptable = skipped_active = grab_errors = 0
    last_grab_error: GrabError | None = None
    for scope in scopes:
        if searched >= max_searches:
            break

        scope_key = _scope_key(scope)
        cooling = cooldowns.get(scope_key)
        if cooling is not None and cooling.not_before > now:
            # Inside its grab-pipeline cooldown window: skip WITHOUT searching and
            # WITHOUT charging the budget, so the budget flows to healthy scopes
            # instead of a scope whose grab keeps failing (round-3 #2).
            continue

        active = await download_repo.find_active_for_request(scope.request_id, season=scope.season)
        if active is not None:
            skipped_active += 1
            # Resolved elsewhere (a manual/other grab is downloading it): drop any
            # stale cooldown so a later failure-rearm starts fresh, not escalated.
            cooldowns.pop(scope_key, None)
            continue

        # Resolve the search descriptor. A TV season's own record carries only the
        # denormalized tmdb_id, so title/year come from the parent MediaRequest.
        if scope.season is not None:  # TV season
            parent = await request_repo.get(scope.request_id)
            if parent is None:  # pragma: no cover - the FK guarantees the parent row
                continue
            title, year, media_type = parent.title, parent.year, "tv"
        else:  # movie
            title, year, media_type = scope.title or "", scope.year, "movie"

        searched += 1
        # NOTE: deliberately NOT wrapped -- a raised indexer error must propagate
        # (honesty over silence: never park a scope just because Prowlarr was down).
        result = await decision_service.preview(
            prowlarr,
            parser,
            profile,
            SqlBlocklistRepository(session),
            tmdb_id=scope.tmdb_id,
            title=title,
            media_type=media_type,
            year=year,
            season=scope.season,
            episodes=None,
        )

        # Try the accepted releases in rank order until one grabs. Only the two
        # PER-RELEASE failures {NoGrabSourceError, DownloadScopeConflictError} --
        # neither of which leaves anything live to track -- fall through to the
        # next-ranked candidate; every OTHER outcome settles the scope on the spot
        # (a grab, an operational GrabError, or a concurrency/shape refusal), so a
        # single top-pick hiccup never hides a grabbable lower-ranked release behind
        # backoff. ``park_scope`` starts True and is cleared by any settling outcome;
        # if it SURVIVES the loop -- because every attempted candidate hit a
        # per-release failure (list exhausted or the attempt cap reached), or because
        # ``accepted`` was empty (nothing found) -- the scope parks on the escalating
        # backoff exactly as a nothing-acceptable search does. Bounded by
        # ``MAX_GRAB_ATTEMPTS_PER_SCOPE`` so a scope whose every candidate is
        # ungrabbable can't monopolise the grab loop; this caps GRAB attempts only --
        # the per-cycle Prowlarr SEARCH cap already spent its one search above,
        # regardless of how many candidates are tried here.
        candidates = result.accepted[:MAX_GRAB_ATTEMPTS_PER_SCOPE]
        park_scope = True
        for attempt, scored in enumerate(candidates, start=1):
            try:
                await grab_service.grab(
                    qbt,
                    session,
                    scored=scored,
                    request_id=scope.request_id,
                    tmdb_id=scope.tmdb_id,
                    year=year,
                    season=scope.season,
                    episodes=None,
                )
                grabbed += 1
                park_scope = False
                cooldowns.pop(scope_key, None)  # grabbed: the pipeline recovered -- clear cooldown
                break
            except (
                AlreadyDownloadingError,
                RequestNotActiveError,
                SeasonRequiredError,
            ) as exc:
                # Concurrency/shape cases that apply to the SCOPE, not this one
                # release, so trying another candidate cannot help and the scope will
                # NOT be re-selected next cycle: ``AlreadyDownloadingError`` -> the
                # scope now has an active download and is skipped BEFORE it costs a
                # search; the two others -> the scope is terminal / mis-shaped and out
                # of ``DUE_SEARCH_STATUSES`` (or rejected up front). Discard any
                # partial write, settle the scope (no park, no further candidates,
                # no error), and leave its state as-is -- never a crash of the whole
                # cycle, never a secret in the log.
                await session.rollback()
                park_scope = False
                # Resolved some other way (now downloading, terminal, or mis-shaped):
                # GrabError is the cooldown's only feeder, so clear any stale entry.
                cooldowns.pop(scope_key, None)
                _logger.warning(
                    "auto-grab: grab refused (%s); leaving scope for a later cycle",
                    type(exc).__name__,
                    extra={"request_id": scope.request_id},
                )
                break
            except GrabError as exc:
                # OPERATIONAL failure, NOT "nothing acceptable found": qBittorrent
                # ACCEPTED the torrent but no info-hash could be derived (opaque URL,
                # and the indexer supplied none either), so there is now a LIVE,
                # untracked torrent and NO ``Download`` row. Settle the scope WITHOUT
                # parking -- parking would both LIE about the state and mark the cycle
                # clean while an orphan torrent silently consumes disk -- and WITHOUT
                # trying another candidate: a live orphan PLUS a second grab would
                # double-download. Discard the partial write, leave the scope's
                # request/season state COMPLETELY untouched, and hand the error up on
                # the result so the caller records it on the ``AutograbStatus`` health
                # signal (TYPE only) and refuses to mark the cycle clean. Unlike a
                # raised indexer search this does NOT abort the cycle -- the torrent
                # reached a reachable qBittorrent (Prowlarr is fine), so a single bad
                # grab must not starve every other due SCOPE; continue to the next
                # scope. The scope stays due, so it IS re-attempted next tick -- the
                # same retry cadence a raised search has, which is the honest cost of
                # not lying about state; it is NOT the tight every-cycle loop the
                # backoff ladder guards a nothing-acceptable scope against.
                #
                # NOTE: grab-side orphan cleanup -- removing the untracked torrent from
                # qBittorrent when its info-hash cannot be derived -- is a deeper
                # follow-up (grab_service would need to remove-by-name/best-effort) and
                # is deliberately OUT OF SCOPE here; this handler only stops the false
                # park + false-clean cycle.
                #
                # Round-3 #2: a scope whose grab keeps failing must not be re-searched
                # every tick (it would consume the whole per-cycle budget and starve
                # other scopes) yet must not be parked (a LIE -- releases exist). Enter
                # an escalating in-process cooldown fed ONLY by GrabError; while it
                # cools, the scope is skipped BEFORE it costs a search. The window is
                # scheduled from a FRESH clock read (round-3 #3), like a park.
                await session.rollback()
                park_scope = False
                prior_failures = cooling.failures if cooling is not None else 0
                delay = cooldown_delay(prior_failures)
                cooldowns[scope_key] = ScopeCooldown(
                    failures=prior_failures + 1,
                    not_before=park_clock() + delay,
                )
                _logger.warning(
                    "auto-grab: grab operational failure (%s); cooling scope "
                    "(consecutive failure #%d, %s window), recording on health, "
                    "leaving scope unchanged",
                    type(exc).__name__,
                    prior_failures + 1,
                    delay,
                    extra={"request_id": scope.request_id, "season": scope.season},
                )
                grab_errors += 1
                last_grab_error = exc
                break
            except (
                NoGrabSourceError,
                DownloadScopeConflictError,
            ) as exc:
                # A release WAS accepted but cannot be grabbed right now, and NOTHING
                # is left live to track: no usable source (``NoGrabSourceError`` --
                # raised BEFORE anything is handed to the client) or the same physical
                # torrent is already active for a DIFFERENT scope
                # (``DownloadScopeConflictError`` -- a multi-season pack; the re-add is
                # a qBittorrent no-op, so nothing is orphaned). Unlike the settling
                # cases above, a LOWER-ranked accepted release may still be grabbable,
                # so discard the partial write and fall through to the next candidate
                # (do NOT ``break``). ``park_scope`` stays True, so an EXHAUSTED list
                # (or the attempt cap) parks the scope on the SAME escalating backoff
                # a nothing-acceptable search uses -- left un-parked with no grabbable
                # alternative the worker would re-search Prowlarr and re-attempt every
                # cycle FOREVER, defeating the single-Prowlarr load guard the backoff
                # ladder exists for. Release titles are external Prowlarr text; the log
                # deliberately carries only the exception TYPE + attempt counter (never
                # the title), matching this module's other grab handlers.
                await session.rollback()
                _logger.warning(
                    "auto-grab: accepted release unusable (%s), attempt %d/%d; "
                    "trying next accepted release",
                    type(exc).__name__,
                    attempt,
                    len(candidates),
                    extra={"request_id": scope.request_id},
                )
        if park_scope:
            # Parking resolves the scope one way or the other, so clear any stale
            # cooldown (GrabError is its only feeder). ``_park`` re-checks for a
            # racing active download and, if one appeared, skips the park entirely
            # (round-3 #1): only a real park counts toward ``no_acceptable``.
            cooldowns.pop(scope_key, None)
            if await _park(session, request_repo, season_repo, download_repo, scope, park_clock):
                no_acceptable += 1

    cooled_down = sum(1 for cd in cooldowns.values() if cd.not_before > now)
    _logger.info(
        "auto-grab cycle: searched=%d grabbed=%d no_acceptable=%d skipped_active=%d "
        "grab_errors=%d cooled_down=%d",
        searched,
        grabbed,
        no_acceptable,
        skipped_active,
        grab_errors,
        cooled_down,
    )
    return AutograbCycleResult(
        searched=searched,
        grabbed=grabbed,
        no_acceptable=no_acceptable,
        skipped_active=skipped_active,
        grab_errors=grab_errors,
        last_grab_error=last_grab_error,
        cooled_down=cooled_down,
    )
