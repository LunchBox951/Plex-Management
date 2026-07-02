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
  backoff) is kept strictly distinct from "the search RAISED" (Prowlarr down /
  rate-limited): the latter leaves the scope's state untouched and propagates so
  the loop records it on the ``AutograbStatus`` health signal -- a scope is never
  falsely marked ``no_acceptable_release`` just because Prowlarr was unreachable.
* **Protect the single Prowlarr.** At most :data:`AUTO_GRAB_MAX_SEARCHES_PER_CYCLE`
  actual searches run per cycle, processed sequentially; a scope that already has
  an active download is skipped BEFORE it costs a search (and never races
  ``grab_service``'s one-active guard).
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
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.ports.download_client import DownloadClientPort
    from plex_manager.ports.indexer import IndexerPort
    from plex_manager.ports.parser import ParserPort

__all__ = [
    "AUTO_GRAB_MAX_SEARCHES_PER_CYCLE",
    "BACKOFF_SCHEDULE",
    "DUE_SEARCH_STATUSES",
    "AutograbCycleResult",
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

# NULL ``next_search_at`` ("due now") sorts ahead of any real timestamp; this
# tz-aware minimum is its sort stand-in so a never-scheduled scope outranks a
# scheduled-but-overdue one when the per-cycle cap has to choose.
_NULL_DUE_SORT_KEY = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class AutograbCycleResult:
    """What one :func:`run_grab_cycle` pass actually did -- for logging and tests.

    ``searched`` counts ACTUAL Prowlarr searches (never more than
    ``max_searches``); ``skipped_active`` counts due scopes passed over because
    they already had an active download (those cost no search).
    """

    searched: int = 0
    grabbed: int = 0
    no_acceptable: int = 0
    skipped_active: int = 0


@dataclass(frozen=True)
class _PendingScope:
    """A due movie request or TV season, normalized for uniform processing.

    ``season``/``season_request_id`` are ``None`` for a movie and set for a TV
    season. ``title``/``year`` are resolved up front for a movie (off its own
    record) and lazily from the parent ``MediaRequest`` for a TV season (whose
    record carries only ``tmdb_id``).
    """

    request_id: int
    tmdb_id: int
    season: int | None
    season_request_id: int | None
    title: str | None
    year: int | None
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


async def _collect_due_scopes(
    request_repo: SqlRequestRepository,
    season_repo: SqlSeasonRequestRepository,
    now: datetime,
) -> list[_PendingScope]:
    """Gather every due movie request + TV season, ordered most-overdue-first.

    NULL ``next_search_at`` (due now) sorts ahead of any scheduled timestamp, then
    oldest-scheduled first -- so the per-cycle cap always spends its budget on the
    most-overdue scopes.
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
            search_attempts=s.search_attempts,
            next_search_at=s.next_search_at,
        )
        for s in seasons
    )
    scopes.sort(key=lambda sc: sc.next_search_at or _NULL_DUE_SORT_KEY)
    return scopes


async def _park(
    session: AsyncSession,
    request_repo: SqlRequestRepository,
    season_repo: SqlSeasonRequestRepository,
    scope: _PendingScope,
    now: datetime,
) -> None:
    """Record a nothing-acceptable result: schedule the backoff + mark the honest
    ``no_acceptable_release`` park state, then commit.

    The SAME honest dead-end the manual ``/queue/grab`` endpoint uses
    (``request_service`` / ``season_request_service.mark_no_acceptable_release``,
    both of which keep the never-un-terminate guard), plus the backoff bookkeeping
    the manual path has no need of. ``search_attempts`` is bumped so the ladder
    escalates; ``next_search_at`` gates the next search.
    """
    scheduled_attempts = scope.search_attempts + 1
    scheduled_at = next_search_at(now, scope.search_attempts)
    if scope.season is not None:  # TV season
        if scope.season_request_id is None:  # pragma: no cover - a tv scope always has one
            return
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


async def run_grab_cycle(
    session: AsyncSession,
    *,
    prowlarr: IndexerPort,
    parser: ParserPort,
    profile: QualityProfile,
    qbt: DownloadClientPort,
    max_searches: int = AUTO_GRAB_MAX_SEARCHES_PER_CYCLE,
    now: datetime | None = None,
) -> AutograbCycleResult:
    """Run one auto-grab pass: search the due scopes and grab / park each.

    For each due scope (most-overdue first), up to ``max_searches`` ACTUAL
    searches:

    * skip it (no search) if it already has an active download -- avoids a wasted
      Prowlarr hit and never races ``grab_service``'s one-active guard;
    * else search + decide via :func:`decision_service.preview`; if a release is
      accepted, grab the top pick via :func:`grab_service.grab` (which drives the
      scope to ``downloading`` and commits); if nothing is acceptable, park it at
      ``no_acceptable_release`` and schedule the escalating backoff.

    A search that RAISES (Prowlarr unreachable / rate-limited -- the ``IndexerPort``
    contract raises rather than returning ``[]``) is NOT caught here: it propagates
    so the caller (``web/app.py``'s ``_autograb_loop``) records it on the
    ``AutograbStatus`` health signal and backs the whole cycle off. The raising
    scope's state is left untouched (never falsely parked), and any scope already
    processed this cycle keeps its committed result.

    ``now`` is injectable for deterministic tests; it defaults to
    ``datetime.now(UTC)``.
    """
    now = now or datetime.now(UTC)
    request_repo = SqlRequestRepository(session)
    season_repo = SqlSeasonRequestRepository(session)
    download_repo = SqlDownloadRepository(session)

    scopes = await _collect_due_scopes(request_repo, season_repo, now)

    searched = grabbed = no_acceptable = skipped_active = 0
    for scope in scopes:
        if searched >= max_searches:
            break

        active = await download_repo.find_active_for_request(scope.request_id, season=scope.season)
        if active is not None:
            skipped_active += 1
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

        if result.accepted:
            try:
                await grab_service.grab(
                    qbt,
                    session,
                    scored=result.accepted[0],
                    request_id=scope.request_id,
                    tmdb_id=scope.tmdb_id,
                    year=year,
                    season=scope.season,
                    episodes=None,
                )
                grabbed += 1
            except (
                AlreadyDownloadingError,
                RequestNotActiveError,
                SeasonRequiredError,
            ) as exc:
                # Concurrency/shape cases where the scope will NOT be re-selected
                # next cycle, so leaving its schedule untouched cannot loop:
                # ``AlreadyDownloadingError`` -> the scope now has an active
                # download and is skipped BEFORE it costs a search; the two others
                # -> the scope is terminal / mis-shaped and out of
                # ``DUE_SEARCH_STATUSES`` (or rejected up front). Discard any
                # partial write and leave the scope as-is -- never a crash of the
                # whole cycle, never a secret in the log.
                await session.rollback()
                _logger.warning(
                    "auto-grab: grab refused (%s); leaving scope for a later cycle",
                    type(exc).__name__,
                    extra={"request_id": scope.request_id},
                )
            except (
                NoGrabSourceError,
                GrabError,
                DownloadScopeConflictError,
            ) as exc:
                # A release WAS accepted but cannot be grabbed right now: no usable
                # source (``NoGrabSourceError``), no derivable info-hash to track by
                # (``GrabError``), or the same physical torrent is already active for
                # a different scope (``DownloadScopeConflictError`` -- a multi-season
                # pack). Unlike the busy/terminal cases above, these scopes stay
                # selectable AND immediately due, and the offending release keeps
                # sorting first -- left untouched the worker would re-search Prowlarr
                # and re-attempt every cycle FOREVER, defeating the single-Prowlarr
                # load guard the backoff ladder exists for. Discard the partial write
                # and park on the SAME escalating backoff as a nothing-acceptable
                # search so the scope is not immediately due again.
                await session.rollback()
                _logger.warning(
                    "auto-grab: grab unusable (%s); parking on backoff",
                    type(exc).__name__,
                    extra={"request_id": scope.request_id},
                )
                await _park(session, request_repo, season_repo, scope, now)
                no_acceptable += 1
        else:
            await _park(session, request_repo, season_repo, scope, now)
            no_acceptable += 1

    _logger.info(
        "auto-grab cycle: searched=%d grabbed=%d no_acceptable=%d skipped_active=%d",
        searched,
        grabbed,
        no_acceptable,
        skipped_active,
    )
    return AutograbCycleResult(
        searched=searched,
        grabbed=grabbed,
        no_acceptable=no_acceptable,
        skipped_active=skipped_active,
    )
