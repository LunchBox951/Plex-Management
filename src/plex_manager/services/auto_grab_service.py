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
* **Source-unresolvable telemetry (beta-week, issue #43).** A ``QbittorrentSourceError``
  per-release failure is logged with full release identity (source title, indexer,
  guid/info_hash, season, attempt context) so "how often did the same source
  persistently fail" is answerable from ``log_events`` after the beta week; a
  per-cycle ``source_failures`` count is folded into the closing summary INFO. Its
  sibling per-release failures ({``NoGrabSourceError``, ``TorrentAlreadyTrackedError``})
  keep the lighter "type name only" logging -- there is no beta-week data need for
  those.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from plex_manager.adapters.qbittorrent.adapter import QbittorrentSourceError
from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError
from plex_manager.domain.season_pack import (
    MultiSeasonRequestIntent,
    SeasonPackSeasonState,
    episode_numbers,
)
from plex_manager.logsafe import safe_guid, safe_int, safe_text
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import (
    decision_service,
    grab_service,
    request_service,
    season_episode_service,
    season_request_service,
)
from plex_manager.services.grab_service import (
    AlreadyDownloadingError,
    GrabError,
    NoGrabSourceError,
    RequestNotActiveError,
    SeasonRequiredError,
    TorrentAlreadyTrackedError,
)
from plex_manager.services.log_capture_service import AUTO_GRAB_TELEMETRY_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.indexer import IndexerPort
    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.parser import ParserPort

__all__ = [
    "AIRING_REFRESH_MAX_PER_CYCLE",
    "AIR_DATE_WAKE_MAX_PER_CYCLE",
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

# TWO loggers, deliberately split (wave-6 finding). ``_logger`` is the ordinary
# module logger for OPERATIONAL records (search-failure cooldowns, "accepted
# release unusable", the park-race INFO): ordinary level semantics, ordinary
# operator-controlled ``log_retention_days``. ``_telemetry_logger`` is a
# dedicated ``.telemetry`` CHILD used by the issue-#43 records ONLY -- the
# enriched source-failure WARNING and the per-cycle summary INFO -- constructed
# FROM the shared constant (retention precedent) so the emitter can never drift
# from the treatment ``log_capture_service`` keys on that exact name: the INFO
# pin (an operator WARNING/ERROR floor must not stop the dataset being CREATED)
# and the 30-day retention floor (the default 7-day prune must not delete it
# mid-beta-week). Scoping that treatment to the module logger instead would let
# every operational warning on a failing install dodge the operator's retention
# window for 30 days -- exactly what the split prevents.
_logger = logging.getLogger(__name__)
_telemetry_logger = logging.getLogger(AUTO_GRAB_TELEMETRY_LOGGER_NAME)

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
# accepted release can't be grabbed for a PER-RELEASE reason (no usable source, an
# unresolvable/vetoed source, or its hash is already active for a different scope
# or a different request), the worker falls through to the
# next-ranked accepted release rather than parking a still-grabbable scope behind
# backoff. Bounded so a scope whose every candidate is ungrabbable can't monopolise
# the grab loop (or hammer qBittorrent's add). This caps GRAB attempts ONLY -- the
# per-cycle Prowlarr SEARCH cap (:data:`AUTO_GRAB_MAX_SEARCHES_PER_CYCLE`) is
# unaffected: one scope still costs exactly one search no matter how many of its
# accepted releases are tried.
MAX_GRAB_ATTEMPTS_PER_SCOPE: int = 3

# At most this many ``available``/``completed`` TV seasons get their aired target
# refreshed from TMDB per cycle by the airing pre-pass (ADR-0020 §6,
# :func:`plex_manager.services.season_episode_service.reconcile_airing`) -- a
# module constant mirroring :data:`AUTO_GRAB_MAX_SEARCHES_PER_CYCLE`, protecting
# the single TMDB budget from a large install with many finished shows.
AIRING_REFRESH_MAX_PER_CYCLE: int = 5

# At most this many ``waiting_for_air_date`` seasons get re-checked against TMDB
# per cycle by the air-date wake pass (issue #210,
# :func:`plex_manager.services.season_request_service.wake_waiting_for_air_date`)
# -- a module constant mirroring :data:`AIRING_REFRESH_MAX_PER_CYCLE`, protecting
# the single TMDB budget. The waiting-season candidate set is tiny relative to an
# install's whole library and shrinks permanently as rows wake, so this bound is
# generous, not tight.
AIR_DATE_WAKE_MAX_PER_CYCLE: int = 5

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
    ``max_searches``) -- INCLUDING each Pass-2 episode-fallback search
    (``_attempt_episode_fallback``'s own call to ``decision_service.
    preview_episode_fallback``), which shares this SAME budget rather than
    getting an uncounted second search per scope (P2 fix, issue #178 review);
    ``skipped_active`` counts due scopes passed over because they already had an
    active download (those cost no search).

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

    ``season_episode_fallback_grabs`` (ADR-0020, issue #178) counts scopes settled
    by the Pass-2 episode-level fallback rather than a Pass-1 season-pack grab --
    included in ``grabbed`` too, broken out here for observability into how often
    the fallback path (vs. a clean pack grab) is what actually completes a season.

    ``air_date_woken`` (issue #210) counts ``waiting_for_air_date`` seasons THIS
    cycle's air-date wake pass (:func:`plex_manager.services.
    season_request_service.wake_waiting_for_air_date`) transitioned into the
    searchable pipeline after TMDB began reporting them. A woken season is
    collected by the SAME cycle's due-scope scan (it runs before
    :func:`_collect_due_scopes`), so a season woken to ``pending`` can be
    searched/grabbed in this very cycle; one woken straight to ``available``
    (already in Plex) never enters the search loop at all -- this counter is the
    caller's only signal that the transition happened, hence the realtime
    invalidation gate in ``web/app.py``'s ``_autograb_once`` also checks it.
    """

    searched: int = 0
    grabbed: int = 0
    no_acceptable: int = 0
    skipped_active: int = 0
    grab_errors: int = 0
    last_grab_error: GrabError | None = None
    cooled_down: int = 0
    season_episode_fallback_grabs: int = 0
    air_date_woken: int = 0


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


@dataclass(frozen=True)
class _EpisodeFallbackOutcome:
    """What one Pass-2 episode-level-fallback attempt (:func:`_attempt_episode_fallback`)
    did for a single whole-season scope.

    ``settled`` mirrors the Pass-1 grab loop's ``park_scope`` convention inverted:
    ``True`` means the scope must NOT be parked this cycle (a grab happened, or a
    per-scope refusal/operational failure settled it some other way); ``False``
    means "found nothing, fall through to the normal park" (target unknown, an
    empty aired target, nothing accepted, or every accepted release was
    ungrabbable). A fully-imported NON-empty target is ``settled=True`` instead:
    the season is completed, not parked (round-4 P2).

    ``searched`` (P2 fix, issue #178 review) is whether this call actually issued
    a Prowlarr search (:func:`decision_service.preview_episode_fallback`) -- the
    caller charges it against the SAME per-cycle ``max_searches`` budget Pass 1
    spends from, so a scope's fallback attempt skipped because the budget was
    already exhausted this cycle reports ``searched=False`` and never inflates
    the count.

    ``budget_skipped`` (P2 fix, issue #178 review round 4) is the third verdict:
    the fallback was NEVER SEARCHED because the per-cycle budget was already
    exhausted. The caller must neither park (a "no acceptable release" verdict
    would be a lie about a search that never ran, and its backoff would delay a
    genuinely missing episode by at least the first rung) nor treat the scope as
    settled -- it is left EXACTLY as it was (due, backoff untouched) and simply
    retried next cycle.
    """

    settled: bool
    grabbed: bool = False
    grab_error: GrabError | None = None
    source_failures: int = 0
    searched: bool = False
    budget_skipped: bool = False


async def _attempt_episode_fallback(
    session: AsyncSession,
    metadata: MetadataPort,
    prowlarr: IndexerPort,
    parser: ParserPort,
    profile: QualityProfile,
    qbt: DownloadClientPort,
    download_repo: SqlDownloadRepository,
    scope: _PendingScope,
    title: str,
    year: int | None,
    today: date,
    save_path: str,
    cooldowns: CooldownRegistry,
    clock: Callable[[], datetime],
    *,
    searched: int,
    max_searches: int,
) -> _EpisodeFallbackOutcome:
    """Pass-2 of the whole-season scope's cycle (ADR-0020, issue #178): only called
    when Pass 1 (the season-pack-only search) accepted nothing this cycle.

    1. Refresh the aired-episode target from TMDB. A raise (``TmdbApiError``/
       ``TmdbAuthError``) means "target unknown this cycle" -- logged, no grab,
       fall through to the normal park (never a guessed target).
    2. Compute the still-missing aired episodes (target minus imported minus any
       in-flight download). Empty with a NON-empty target and no racing active
       download -> the aired target is fully imported (e.g. the refresh just
       retired a retracted episode's pending row): COMPLETE the season via the
       same rollup path an import-completion uses (round-4 P2) -- never park a
       season that is actually done. Empty with an EMPTY target -> nothing to
       fetch, fall through to the normal park.
    3. If the per-cycle Prowlarr search budget (``searched``/``max_searches``,
       the SAME counters Pass 1 spends from) is already exhausted, skip the
       search entirely and report ``budget_skipped`` (round-4 P2): the caller
       leaves the scope due with its backoff untouched -- never a park verdict
       about a search that never ran. (P2 fix, issue #178 review: previously
       this fallback search was uncounted, letting a cycle issue up to 2x
       ``max_searches`` Prowlarr searches.)
    4. Search + decide via :func:`decision_service.preview_episode_fallback`
       (``prefer_season_pack=False``, the engine's ``episode_subset`` gate set to
       the missing set). Try the accepted releases in rank order exactly like the
       Pass-1 grab loop -- the SAME per-release-failure fall-through, bounded by
       :data:`MAX_GRAB_ATTEMPTS_PER_SCOPE` -- until one grabs or the list is
       exhausted.

    On a successful grab, the covered episodes are marked ``grabbed`` in
    ``season_episode_states`` (crash-visibility / observability; ``compute_missing``
    itself trusts the live active download, not this breadcrumb) and the season is
    now ``downloading`` (via :func:`grab_service.grab`), so it must NOT be parked.
    """
    # Narrows ``scope.season``/``scope.season_request_id`` from ``int | None`` to
    # ``int`` for the type checker: the caller only invokes this for a due TV
    # season scope (``scope.season is not None``), which -- by ``_PendingScope``'s
    # own construction in ``_collect_due_scopes`` -- always carries a
    # ``season_request_id`` too. The ``None`` branch is unreachable in practice;
    # returning "target unknown, fall through to the normal park" is the honest,
    # crash-free response if it were ever reached.
    season = scope.season
    season_request_id = scope.season_request_id
    if season is None or season_request_id is None:  # pragma: no cover - a tv scope always has both
        return _EpisodeFallbackOutcome(settled=False)
    scope_key = _scope_key(scope)

    try:
        target = await season_episode_service.refresh_target(
            session,
            metadata,
            media_request_id=scope.request_id,
            season_number=season,
            tmdb_id=scope.tmdb_id,
            today=today,
        )
    except (TmdbApiError, TmdbAuthError) as exc:
        _logger.info(
            "auto-grab: episode-fallback target lookup failed (%s); target unknown "
            "this cycle, falling through to the normal park",
            type(exc).__name__,
            extra={
                "request_id": safe_int(scope.request_id),
                "season": safe_int(season),
                "tmdb_id": safe_int(scope.tmdb_id),
            },
        )
        return _EpisodeFallbackOutcome(settled=False)

    # Commit the aired-target baseline BEFORE entering the candidate loop below.
    # ``refresh_target`` above upserts a ``pending`` row for EVERY aired episode --
    # the WHOLE-season tracking baseline. The loop's per-release failure handlers
    # (``QbittorrentSourceError`` / ``NoGrabSourceError`` / ``TorrentAlreadyTracked
    # Error``) each ``session.rollback()`` and fall through to the next candidate;
    # left uncommitted, those rollbacks would DISCARD this baseline, and a later
    # lower-ranked candidate's ``mark_grabbed`` (+ its commit) would then recreate
    # rows for ONLY its covered episodes -- letting import see the target as just
    # those episodes and mark the whole season complete after a single episode.
    # Committing the baseline here makes it durable across every per-release
    # rollback. INVARIANT: at ``mark_grabbed`` time the full aired-target rows
    # already exist. (Safe: on entry to Pass 2 the session carries no uncommitted
    # writes -- Pass 1 either committed its grab and skipped Pass 2, or rolled back
    # every failed attempt -- so this commits the target rows and nothing else.)
    await session.commit()

    missing = await season_episode_service.compute_missing(
        session,
        download_repo,
        media_request_id=scope.request_id,
        season_number=season,
        season_request_id=season_request_id,
        target=target,
    )
    if not missing:
        # P2 fix (issue #178 review round 4): an EMPTY missing set with a
        # non-empty target means every currently-aired target episode is already
        # imported -- most notably after ``refresh_target`` above just RETIRED
        # the only outstanding pending row (a TMDB retraction/delay). No future
        # import is coming to complete the season, so falling through to the
        # normal park would freeze a genuinely-complete season in
        # ``no_acceptable_release`` forever. Complete it instead, via the SAME
        # rollup path an import-completion uses (``mark_completed`` -> the
        # availability cycle later confirms/promotes), after re-checking no
        # download raced in (``compute_missing`` folds an active download into
        # the "not missing" arithmetic, and completing an in-flight season would
        # be premature -- the import that lands it completes it honestly).
        # Consistent with the import-side stale-grabbed exclusion (round 3):
        # ``compute_missing`` counts only IMPORTED rows, so a stale grabbed
        # breadcrumb inside the aired target keeps the episode in ``missing``
        # and the fallback keeps searching for it -- this branch fires only
        # when the aired target is truly covered by imports.
        # An EMPTY target (nothing aired yet) keeps the pre-existing park
        # fall-through: there is nothing to complete.
        if (
            target
            and await download_repo.find_active_for_request(scope.request_id, season=season) is None
        ):
            # CAS-guarded (issue #229): a concurrent cancel/correction landing
            # between ``_collect_due_scopes``' snapshot and this branch must
            # never be resurrected as ``completed`` -- ``allowed_from`` is
            # exactly the set of statuses this scope was legitimately due from.
            completed = await season_request_service.mark_completed_if_in(
                session,
                media_request_id=scope.request_id,
                season_number=season,
                allowed_from=DUE_SEARCH_STATUSES,
            )
            await session.commit()
            if completed:
                cooldowns.pop(scope_key, None)
                _logger.info(
                    "auto-grab: episode-fallback found the aired target fully imported "
                    "(target shrank to the imported set); completing the season instead "
                    "of parking it",
                    extra={"request_id": safe_int(scope.request_id), "season": safe_int(season)},
                )
                return _EpisodeFallbackOutcome(settled=True)
            # Lost the CAS: a concurrent cancel/correction moved the season out
            # of a due status. Don't resurrect it as completed; leave it as the
            # other actor left it -- settled=True so the caller does not also
            # park it (there is nothing due left to park).
            _logger.info(
                "auto-grab: episode-fallback completion skipped -- season no longer in a "
                "due status (concurrent cancel/correction won); leaving as-is",
                extra={"request_id": safe_int(scope.request_id), "season": safe_int(season)},
            )
            return _EpisodeFallbackOutcome(settled=True)
        return _EpisodeFallbackOutcome(settled=False)

    if searched >= max_searches:
        # Budget already exhausted by Pass 1 (and/or earlier scopes' Pass-2
        # fallbacks) this cycle -- P2 fix (issue #178 review): skip the search
        # rather than issuing an uncounted extra Prowlarr hit. Reported as
        # ``budget_skipped`` (round-4 P2), NOT ``settled=False``: the fallback
        # was never searched, so the caller must not park the season as
        # ``no_acceptable_release`` (a verdict about a search that never ran)
        # or advance its backoff ladder -- the scope is left exactly as it was,
        # still due, and retried next cycle.
        _logger.info(
            "auto-grab: episode-fallback search skipped (per-cycle Prowlarr "
            "search budget exhausted); leaving scope for a later cycle",
            extra={"request_id": safe_int(scope.request_id), "season": safe_int(season)},
        )
        return _EpisodeFallbackOutcome(settled=False, budget_skipped=True)

    fb_result = await decision_service.preview_episode_fallback(
        prowlarr,
        parser,
        profile,
        SqlBlocklistRepository(session),
        tmdb_id=scope.tmdb_id,
        title=title,
        season=season,
        missing_episodes=missing,
    )
    fb_candidates = fb_result.accepted[:MAX_GRAB_ATTEMPTS_PER_SCOPE]
    if not fb_candidates:
        return _EpisodeFallbackOutcome(settled=False, searched=True)

    source_failures = 0
    for attempt, scored in enumerate(fb_candidates, start=1):
        covered = sorted(episode_numbers(scored.parsed.episode))
        try:
            record = await grab_service.grab(
                qbt,
                session,
                scored=scored,
                request_id=scope.request_id,
                tmdb_id=scope.tmdb_id,
                year=year,
                season=season,
                episodes=covered,
                save_path=save_path,
                expected_season_status=scope.status,
            )
            await SqlSeasonEpisodeStateRepository(session).mark_grabbed(
                season_request_id, covered, record.id
            )
            await session.commit()
            cooldowns.pop(scope_key, None)
            return _EpisodeFallbackOutcome(settled=True, grabbed=True, searched=True)
        except (AlreadyDownloadingError, RequestNotActiveError, SeasonRequiredError) as exc:
            # Same posture as the Pass-1 loop: a scope-level concurrency/shape
            # refusal, not a per-release one -- trying another candidate cannot
            # help, so settle without parking.
            await session.rollback()
            cooldowns.pop(scope_key, None)
            _logger.warning(
                "auto-grab: episode-fallback grab refused (%s); leaving scope for a later cycle",
                type(exc).__name__,
                extra={"request_id": safe_int(scope.request_id), "season": safe_int(season)},
            )
            return _EpisodeFallbackOutcome(settled=True, searched=True)
        except GrabError as exc:
            # Operational failure (qBittorrent accepted the torrent but no
            # info-hash could be derived): same cooldown treatment as the Pass-1
            # loop -- never park (that would lie), and cool the scope so it
            # doesn't monopolise the per-cycle budget.
            await session.rollback()
            cooling = cooldowns.get(scope_key)
            prior_failures = cooling.failures if cooling is not None else 0
            delay = cooldown_delay(prior_failures)
            cooldowns[scope_key] = ScopeCooldown(
                failures=prior_failures + 1, not_before=clock() + delay
            )
            _logger.warning(
                "auto-grab: episode-fallback grab operational failure (%s); cooling "
                "scope (consecutive failure #%d, %s window)",
                type(exc).__name__,
                prior_failures + 1,
                delay,
                extra={"request_id": safe_int(scope.request_id), "season": safe_int(season)},
            )
            return _EpisodeFallbackOutcome(settled=True, grab_error=exc, searched=True)
        except QbittorrentSourceError as exc:
            # A LOWER-ranked accepted release may still be grabbable -- fall
            # through to the next candidate (do NOT settle).
            await session.rollback()
            source_failures += 1
            _telemetry_logger.warning(
                "auto-grab: episode-fallback accepted release source-unresolvable "
                "(%s): title=%r indexer=%r guid=%r info_hash=%s season=%s, "
                "attempt %d/%d; trying next accepted release",
                type(exc).__name__,
                safe_text(scored.candidate.title),
                safe_text(scored.candidate.indexer_name),
                safe_guid(scored.candidate.guid),
                safe_text(scored.candidate.info_hash) if scored.candidate.info_hash else "-",
                safe_int(season),
                attempt,
                len(fb_candidates),
                extra={
                    "request_id": safe_int(scope.request_id),
                    "tmdb_id": safe_int(scope.tmdb_id),
                    "season": safe_int(season),
                    "source_title": safe_text(scored.candidate.title),
                    "indexer": safe_text(scored.candidate.indexer_name),
                    "guid": safe_guid(scored.candidate.guid),
                    "info_hash": (
                        safe_text(scored.candidate.info_hash)
                        if scored.candidate.info_hash
                        else None
                    ),
                    "attempt": attempt,
                    "attempts_total": len(fb_candidates),
                },
            )
        except (NoGrabSourceError, TorrentAlreadyTrackedError) as exc:
            # Same fall-through posture as the Pass-1 loop's sibling handler.
            await session.rollback()
            _logger.warning(
                "auto-grab: episode-fallback accepted release unusable (%s), "
                "attempt %d/%d; trying next accepted release",
                type(exc).__name__,
                attempt,
                len(fb_candidates),
                extra={"request_id": safe_int(scope.request_id)},
            )
    return _EpisodeFallbackOutcome(settled=False, source_failures=source_failures, searched=True)


async def _park(
    session: AsyncSession,
    request_repo: SqlRequestRepository,
    season_repo: SqlSeasonRequestRepository,
    download_repo: SqlDownloadRepository,
    scope: _PendingScope,
    clock: Callable[[], datetime],
) -> bool:
    """Record a nothing-acceptable result: mark the honest ``no_acceptable_release``
    park state via a compare-and-swap FIRST, then -- ONLY if it actually won --
    schedule the backoff and commit both together. Returns ``True`` if it parked,
    ``False`` if a racing writer made parking wrong (see below).

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

    Issue #72 -- the CAS itself, not just the pre-check above: that re-check only
    closes the gap up to THIS instant -- a concurrent writer can still win the race
    in the (tiny) window between it and the actual status write below.
    ``mark_no_acceptable_release`` now performs a genuine ``set_status_if_in``
    compare-and-swap (the database's ``WHERE status IN (...)`` decides at write
    time, never this function's stale read), so that window is closed too, and its
    boolean return lets this function tell a real park apart from a lost race. The
    backoff write (``schedule_search``) only happens -- and only gets committed --
    AFTER a WON CAS: a lost race is rolled back and reported un-parked WITHOUT
    ever touching ``search_attempts`` / ``next_search_at``, so a park that did not
    happen can never advance the backoff ladder either.

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
        parked = await season_request_service.mark_no_acceptable_release(
            session, media_request_id=scope.request_id, season_number=scope.season
        )
        if not parked:
            await session.rollback()
            _logger.info(
                "auto-grab: lost the park race to a concurrent writer; leaving scope "
                "as-is (no backoff written)",
                extra={"request_id": scope.request_id, "season": scope.season},
            )
            return False
        await season_repo.schedule_search(
            scope.season_request_id,
            search_attempts=scheduled_attempts,
            next_search_at=scheduled_at,
        )
        # Flush-only; this function owns the commit boundary.
    else:  # movie
        parked = await request_service.mark_no_acceptable_release(session, scope.request_id)
        if not parked:
            await session.rollback()
            _logger.info(
                "auto-grab: lost the park race to a concurrent writer; leaving scope "
                "as-is (no backoff written)",
                extra={"request_id": scope.request_id},
            )
            return False
        await request_repo.schedule_search(
            scope.request_id,
            search_attempts=scheduled_attempts,
            next_search_at=scheduled_at,
        )
    await session.commit()
    return True


async def run_grab_cycle(
    session: AsyncSession,
    *,
    prowlarr: IndexerPort,
    parser: ParserPort,
    profile: QualityProfile,
    qbt: DownloadClientPort,
    metadata: MetadataPort | None = None,
    library: LibraryPort | None = None,
    max_searches: int = AUTO_GRAB_MAX_SEARCHES_PER_CYCLE,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    cooldowns: CooldownRegistry | None = None,
    save_path: str = "",
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
      ``QbittorrentSourceError``, ``TorrentAlreadyTrackedError``}); if nothing is
      acceptable, or every accepted release is ungrabbable, park it at
      ``no_acceptable_release`` and schedule the escalating backoff.

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

    ``save_path`` (issues #133/#157) is threaded verbatim into every
    :func:`grab_service.grab` call: the caller (``web/app.py``'s ``_autograb_once``)
    resolves the HOST-namespace downloads root once per cycle
    (``path_visibility.resolve_downloads_host_root``) and passes it here, so an
    auto-grabbed torrent lands under the mounted ``/downloads`` bind exactly like a
    manual grab, rather than qBittorrent's own (possibly invisible) default.
    ``""`` (the default) leaves qBittorrent's own default in charge, unchanged
    prior behaviour.

    ``metadata`` (ADR-0020, issue #178) is the optional :class:`MetadataPort` (TMDB)
    that powers the episode-level fallback for whole-season TV scopes: an airing
    pre-pass (bounded, :data:`AIRING_REFRESH_MAX_PER_CYCLE`) re-arms any
    ``available``/``completed`` season whose aired target has grown, THEN, for each
    whole-season scope where Pass 1 (the season-pack-only search) accepted nothing
    this cycle, a Pass-2 episode-level fallback search is attempted (see
    :func:`_attempt_episode_fallback`). ``None`` (the default -- an unconfigured
    TMDB) disables both cleanly: Pass 1 behaves exactly as before this feature
    existed, and a scope with nothing acceptable still parks honestly.

    ``metadata`` also powers a THIRD pre-pass (issue #210): a bounded, rotated
    re-check of ``waiting_for_air_date`` seasons against TMDB
    (:func:`plex_manager.services.season_request_service.
    wake_waiting_for_air_date`, :data:`AIR_DATE_WAKE_MAX_PER_CYCLE`), run
    immediately AFTER the airing pre-pass above and before due-scope collection --
    so a season TMDB now reports is woken and searched/grabbed in this SAME
    cycle. ``library`` (optional, mirrors ``metadata``) powers that wake's
    Plex-present->``available`` short-circuit; ``None`` (unconfigured Plex) wakes
    a season straight to the honest ``pending`` fallback instead.
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

    # Airing pre-pass (ADR-0020 §6): re-arm any available/completed season whose
    # aired target has grown BEFORE due-scope collection, so a newly-aired episode
    # re-enters ``DUE_SEARCH_STATUSES`` in time to be collected THIS cycle. Bounded
    # and best-effort per season (a TMDB error for one season is logged and
    # skipped, never aborting the pass); a no-op when TMDB is unconfigured.
    #
    # The air-date wake pass (issue #210) runs SECOND, sharing the same
    # ``metadata is not None`` guard: it must follow ``reconcile_airing``, not
    # precede it, so a season this wake transitions to ``available`` this cycle is
    # never ALSO picked up by ``reconcile_airing``'s own candidate query in the
    # same pass (``reconcile_airing`` already selected its candidates before the
    # wake runs) -- a benign but confusing double-touch this ordering avoids. Both
    # writes land in the ONE commit below so the pre-pass stays a single unit.
    air_date_woken = 0
    if metadata is not None:
        await season_episode_service.reconcile_airing(
            session, metadata, parser=parser, now=now, max_refresh=AIRING_REFRESH_MAX_PER_CYCLE
        )
        air_date_woken = await season_request_service.wake_waiting_for_air_date(
            session, metadata, library, now=now, max_refresh=AIR_DATE_WAKE_MAX_PER_CYCLE
        )
        await session.commit()

    scopes = await _collect_due_scopes(request_repo, season_repo, now, cooldowns)

    searched = grabbed = no_acceptable = skipped_active = grab_errors = 0
    season_episode_fallback_grabs = 0
    # Beta-week telemetry (issue #43): count of ``QbittorrentSourceError`` per-release
    # failures this cycle -- folded into the closing summary INFO below. Each
    # individual occurrence is ALSO logged with full release identity (see the
    # dedicated except clause below); this is just the per-cycle rollup so an
    # operator/analyst can see the trend without counting WARNINGs by hand.
    source_failures = 0
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
            stored_episodes = (
                parent.requested_episodes.get(scope.season) if parent.requested_episodes else None
            )
            scope_episodes = list(stored_episodes) if stored_episodes is not None else None
            scope_episodes_by_season = (
                {season: list(values) for season, values in parent.requested_episodes.items()}
                if parent.requested_episodes
                else None
            )
            sibling_seasons = await season_repo.list_for_request(parent.id)
            requested = (
                parent.requested_seasons
                or tuple(sorted(parent.requested_episodes or {}))
                or tuple(row.season_number for row in sibling_seasons)
            )
            multi_season_intent = MultiSeasonRequestIntent(
                mode=(
                    "whole_show" if parent.tv_request_mode == "whole_show" else "explicit_seasons"
                ),
                requested_seasons=tuple(requested),
                seasons=tuple(
                    SeasonPackSeasonState(
                        season_number=row.season_number,
                        status=row.status,
                        installed_quality_id=row.installed_quality_id,
                        installed_profile_index=row.installed_profile_index,
                    )
                    for row in sibling_seasons
                ),
            )
        else:  # movie
            title, year, media_type = scope.title or "", scope.year, "movie"
            scope_episodes = None
            scope_episodes_by_season = None
            multi_season_intent = None

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
            episodes=scope_episodes,
            multi_season_intent=multi_season_intent,
        )

        # Try the accepted releases in rank order until one grabs. Only the four
        # PER-RELEASE failures {NoGrabSourceError, QbittorrentSourceError,
        # TorrentAlreadyTrackedError} -- none of which leaves anything live to track
        # for this scope -- fall through to the next-ranked candidate;
        # every OTHER outcome settles the scope on the spot
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
        # P1 fix (issue #178 review): whether Pass 1's ``decide()`` accepted an
        # acceptable season pack THIS cycle, independent of whether any candidate
        # actually grabbed. Gates Pass 2 below -- an accepted-but-ungrabbable pack
        # (every candidate hit a transient per-release error) must park/cool and be
        # retried next cycle, never fall through to the single-episode fallback:
        # that would create the season's one active download, so next cycle the
        # scope reads ``skipped_active`` and the pack -- typically grabbable once
        # the transient error clears -- is delayed behind piecemeal singles, which
        # is exactly the #167 "singles while a pack was viable" pattern.
        pass1_found_pack = bool(result.accepted)
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
                    episodes=scope_episodes,
                    scope_episodes_by_season=scope_episodes_by_season,
                    save_path=save_path,
                    # The decision's premise rides with the action: this scope
                    # was selected because the season read as DUE at selection
                    # time. If the eviction recovery folds it to 'available'
                    # (file never left disk) before grab()'s own fresh read,
                    # grab refuses up front instead of mistaking the fold for
                    # an intentional reopen (see grab()'s docstring). Movies
                    # need no premise: every non-due movie status is terminal,
                    # so grab's up-front gate already refuses it.
                    expected_season_status=scope.status if scope.season is not None else None,
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
            except QbittorrentSourceError as exc:
                # A release WAS accepted but its HTTP source was vetoed or resolved
                # to neither a magnet nor a locally-hashable ``.torrent``.
                # qBittorrent is HEALTHY -- the SOURCE is the problem; raised inside
                # ``qbt.add`` BEFORE the add POST, so nothing was handed to the
                # client and nothing is orphaned -- exactly a per-release "unusable
                # source", the auto-grab twin of the manual grab's 422
                # ``torrent_source_unresolvable``, NOT a client outage. Unlike the
                # settling cases above, a LOWER-ranked accepted release may still be
                # grabbable, so discard the partial write and fall through to the
                # next candidate (do NOT ``break``). ``park_scope`` stays True, so an
                # EXHAUSTED list (or the attempt cap) parks the scope on the SAME
                # escalating backoff a nothing-acceptable search uses -- left
                # un-parked with no grabbable alternative the worker would re-search
                # Prowlarr and re-attempt every cycle FOREVER, defeating the
                # single-Prowlarr load guard the backoff ladder exists for.
                #
                # Beta-week telemetry (issue #43): unlike the sibling per-release
                # failures below, this is the one the beta needs source-failure DATA
                # for -- "how often did the same source persistently fail" is
                # otherwise unanswerable from a generic parked state. So this WARNING
                # is deliberately enriched with release identity (source title,
                # indexer, guid/info_hash, season, attempt context), plus a per-cycle
                # ``source_failures`` count folded into the closing summary INFO.
                #
                # ``request_id``/``tmdb_id`` go in ``extra=`` only, matching every
                # other handler in this module -- ``log_capture_service._extract_
                # context`` picks exactly ``LOG_EVENT_CORRELATION_KEYS`` (request_id/
                # download_id/tmdb_id) out of ``extra=`` into the durable row's
                # structured, filterable ``context_json`` (``GET /ops/logs/export?
                # correlation_id=``), so putting them in the TEXT too would be inert
                # duplication. ``season``/the release identity fields are NOT
                # correlation keys, though, and ``log_capture_service`` only persists
                # a record's rendered ``message`` text (``record.getMessage()``) plus
                # that restricted context -- an ``extra=``-only field never reaches
                # ``log_events`` at all. So, unlike the sibling handlers' "type name
                # only" text, THIS message deliberately interpolates them so the data
                # this telemetry exists for actually survives the beta week, not just
                # this process's lifetime. ``title``/``indexer``/``info_hash`` are
                # external Prowlarr text run through ``safe_text`` (log-hygiene
                # convention, #35): CodeQL's py/log-injection taints message args and
                # ``extra=`` fields alike, and CR/LF must not be able to forge a
                # second log record. ``guid`` gets the stronger ``safe_guid`` barrier
                # (Codex P1): a Prowlarr private-indexer GUID can be a URI of ANY
                # shape embedding a tracker passkey/session token (http(s) path/
                # query, a magnet's percent-encoded ``tr=`` announce URLs, a
                # schemeless ``host/path?passkey=``), so it is NOT safe to log
                # verbatim (north star #3). ``safe_guid`` passes through ONLY a
                # provably plain id (strict allowlist) and redacts everything else
                # to ``<label>#<sha256-prefix>`` -- the label kept for
                # diagnosability, the credential-bearing remainder never persisted
                # to ``log_events``/``/ops/logs``, and the stable hash still lets
                # the beta-week analysis correlate repeated failures of the SAME
                # release.
                await session.rollback()
                source_failures += 1
                release = scored.candidate
                _telemetry_logger.warning(
                    "auto-grab: accepted release source-unresolvable (%s): "
                    "title=%r indexer=%r guid=%r info_hash=%s season=%s, "
                    "attempt %d/%d; trying next accepted release",
                    type(exc).__name__,
                    safe_text(release.title),
                    safe_text(release.indexer_name),
                    safe_guid(release.guid),
                    safe_text(release.info_hash) if release.info_hash else "-",
                    scope.season if scope.season is not None else "-",
                    attempt,
                    len(candidates),
                    extra={
                        "request_id": scope.request_id,
                        "tmdb_id": scope.tmdb_id,
                        "season": scope.season,
                        "source_title": safe_text(release.title),
                        "indexer": safe_text(release.indexer_name),
                        "guid": safe_guid(release.guid),
                        "info_hash": safe_text(release.info_hash) if release.info_hash else None,
                        "attempt": attempt,
                        "attempts_total": len(candidates),
                    },
                )
            except (
                NoGrabSourceError,
                TorrentAlreadyTrackedError,
            ) as exc:
                # A release WAS accepted but cannot be grabbed right now, and NOTHING
                # is left live to track: no usable source (``NoGrabSourceError`` --
                # raised BEFORE anything is handed to the client); or the torrent's
                # hash is already tracked by a DIFFERENT request entirely
                # (``TorrentAlreadyTrackedError`` -- that request's download owns the
                # physical torrent, so any add was an idempotent no-op on an
                # already-present torrent and this scope owns nothing live; the
                # manual endpoint's 409 ``torrent_already_tracked`` twin). Unlike the
                # settling cases above, a LOWER-ranked accepted release may still be
                # grabbable, so discard the partial write and fall through to the
                # next candidate (do NOT ``break``). ``park_scope`` stays True, so an
                # EXHAUSTED list (or the attempt cap) parks the scope on the SAME
                # escalating backoff a nothing-acceptable search uses -- left
                # un-parked with no grabbable alternative the worker would re-search
                # Prowlarr and re-attempt every cycle FOREVER, defeating the
                # single-Prowlarr load guard the backoff ladder exists for. Release
                # titles are external Prowlarr text; the log deliberately carries
                # only the exception TYPE + attempt counter (never the title),
                # matching this module's other grab handlers -- unlike the sibling
                # ``QbittorrentSourceError`` branch above, there is no beta-week data
                # need for these three, so the lighter-weight logging stays as-is.
                await session.rollback()
                _logger.warning(
                    "auto-grab: accepted release unusable (%s), attempt %d/%d; "
                    "trying next accepted release",
                    type(exc).__name__,
                    attempt,
                    len(candidates),
                    extra={"request_id": scope.request_id},
                )

        # Pass 2 (ADR-0020, issue #178): ONLY when Pass 1 accepted NOTHING this
        # cycle (``not pass1_found_pack`` -- an accepted-but-ungrabbable pack must
        # NOT fall through here, see the P1 fix above; ``park_scope`` still True
        # covers the "nothing accepted at all" AND "every grab attempt hit a
        # settling failure" cases), the scope is a WHOLE-season TV scope (no
        # specific episodes named), and TMDB is configured. The issue #167 hard
        # gate (Pass 1, above) cannot be bypassed by this: Pass 2 never runs
        # unless Pass 1 already accepted nothing.
        if (
            park_scope
            and not pass1_found_pack
            and metadata is not None
            and scope.season is not None
            and not scope_episodes
        ):
            fb_outcome = await _attempt_episode_fallback(
                session,
                metadata,
                prowlarr,
                parser,
                profile,
                qbt,
                download_repo,
                scope,
                title,
                year,
                now.date(),
                save_path,
                cooldowns,
                park_clock,
                searched=searched,
                max_searches=max_searches,
            )
            source_failures += fb_outcome.source_failures
            if fb_outcome.searched:
                # The fallback issued its OWN Prowlarr search (``decision_service.
                # preview_episode_fallback``) -- P2 fix (issue #178 review): count
                # it against the SAME per-cycle budget Pass 1's search above spent
                # from, so a cycle can never issue more than ``max_searches``
                # ACTUAL Prowlarr searches in total (previously Pass 2 was
                # uncounted, letting a cycle double the intended load on a
                # rate-limited indexer).
                searched += 1
            if fb_outcome.budget_skipped:
                # Round-4 P2: the fallback was never searched (per-cycle budget
                # exhausted), so a park would write a "no acceptable release"
                # verdict about a search that never ran AND advance the backoff
                # ladder, delaying a genuinely-missing episode by at least the
                # first rung. Leave the scope exactly as it was -- still due,
                # backoff untouched -- and retry next cycle.
                park_scope = False
            if fb_outcome.settled:
                park_scope = False
                if fb_outcome.grabbed:
                    grabbed += 1
                    season_episode_fallback_grabs += 1
                if fb_outcome.grab_error is not None:
                    grab_errors += 1
                    last_grab_error = fb_outcome.grab_error

        if park_scope:
            # Parking resolves the scope one way or the other, so clear any stale
            # cooldown (GrabError is its only feeder). ``_park`` re-checks for a
            # racing active download and, if one appeared, skips the park entirely
            # (round-3 #1): only a real park counts toward ``no_acceptable``.
            cooldowns.pop(scope_key, None)
            if await _park(session, request_repo, season_repo, download_repo, scope, park_clock):
                no_acceptable += 1

    cooled_down = sum(1 for cd in cooldowns.values() if cd.not_before > now)
    _telemetry_logger.info(
        "auto-grab cycle: searched=%d grabbed=%d no_acceptable=%d skipped_active=%d "
        "grab_errors=%d cooled_down=%d source_failures=%d season_episode_fallback_grabs=%d "
        "air_date_woken=%d",
        searched,
        grabbed,
        no_acceptable,
        skipped_active,
        grab_errors,
        cooled_down,
        source_failures,
        season_episode_fallback_grabs,
        air_date_woken,
        extra={
            "source_failures": source_failures,
            "season_episode_fallback_grabs": season_episode_fallback_grabs,
            "air_date_woken": air_date_woken,
        },
    )
    return AutograbCycleResult(
        searched=searched,
        grabbed=grabbed,
        no_acceptable=no_acceptable,
        skipped_active=skipped_active,
        grab_errors=grab_errors,
        last_grab_error=last_grab_error,
        cooled_down=cooled_down,
        season_episode_fallback_grabs=season_episode_fallback_grabs,
        air_date_woken=air_date_woken,
    )
