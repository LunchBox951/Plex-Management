"""Per-season TV request orchestration — season lifecycle + parent rollup.

A TV ``MediaRequest`` (the show) has no lifecycle of its own: its ``status`` is a
COMPUTED rollup of its per-season :class:`~plex_manager.ports.repositories.
SeasonRequestRecord` rows, re-derived via the pure
:func:`plex_manager.domain.season_rollup.rollup_status` and persisted onto the
parent after EVERY season transition (:func:`_recompute_parent`, always in the
SAME transaction as the transition that triggered it).

This module is the per-season analogue of the movie-only "phase" functions at the
top of :mod:`plex_manager.services.request_service` (``mark_completed`` /
``mark_available`` / ``mark_no_acceptable_release``) plus the repository-level
``set_status`` those movie call sites use directly. Every function here is
FLUSH-ONLY (never calls ``session.commit()``): every identified caller
(``grab_service``, ``queue_service``, ``import_service``, ``request_service``)
already owns its own commit boundary, exactly mirroring how
``SqlRequestRepository.set_status`` / ``.mark_completed`` / ``.mark_available``
compose inside the movie call sites today.

Callers here only ever hold a ``(media_request_id, season_number)`` tuple (off a
``Download`` row, or a fresh request), never a ``SeasonRequest`` id — every public
function resolves the row via :meth:`SeasonRequestRepository.ensure` (idempotent
get-or-create, defaulting to ``pending`` on first creation) rather than requiring
the caller to already know the id.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from sqlalchemy.exc import IntegrityError

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError
from plex_manager.domain.season_rollup import rollup_status
from plex_manager.logsafe import safe_int
from plex_manager.models import RequestStatus
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.repositories import RequestRecord, SeasonRequestRecord

__all__ = [
    "clear_library_path",
    "ensure_seasons",
    "mark_available",
    "mark_completed",
    "mark_completed_if_in",
    "mark_no_acceptable_release",
    "reset_for_research",
    "set_installed_quality",
    "set_library_path",
    "set_status",
    "set_status_if_in",
    "wake_waiting_for_air_date",
]

_logger = logging.getLogger(__name__)

# Season statuses (string values) at which a SEASON is FINISHED. Duplicated here
# rather than imported from ``request_service`` -- that module imports THIS one
# (to run ``ensure_seasons`` from ``create_request``), so importing back would be
# a circular module dependency. Mirrors ``request_service.
# TERMINAL_REQUEST_STATUS_VALUES`` at the season granularity: a finished season
# must never be re-armed to a non-terminal status by a stale
# ``mark_no_acceptable_release`` / a later, unrelated download's failure
# (``set_status(skip_if_terminal=True)``).
#
# ``evicted`` (ADR-0012) belongs here for the SAME reason as the movie-level set:
# there is nothing left on disk for THIS season to resume, so a stale signal must
# never drag it back to ``searching``/``no_acceptable_release``. This does NOT
# conflict with an evicted season being re-requestable -- :func:`ensure_seasons`
# (the ``POST /requests`` re-request path, see C3) re-arms an ``evicted`` season
# to ``pending``/``available`` through a SEPARATE, explicit code path that never
# consults this set, exactly mirroring how a re-requested evicted MOVIE gets a
# brand-new row rather than going through this guard.
_TERMINAL_SEASON_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value
    for s in (
        RequestStatus.completed,
        RequestStatus.available,
        RequestStatus.failed,
        RequestStatus.evicted,
        # ADR-0014: a cancelled season is terminal for the same reason as
        # evicted -- a stale later signal must never drag it back to searching.
        RequestStatus.cancelled,
    )
)

# Season statuses at which a season's content is imported and on disk -- watchable
# now (``available``, Plex-confirmed) or awaiting Plex confirmation (``completed``,
# "Finalizing"). These are exactly the moments a MOVIE stamps its parent's
# ``completed_at`` (``SqlRequestRepository.mark_completed``/``.mark_available``);
# ``_recompute_parent`` stamps the TV parent's ``completed_at`` the first time a
# tracked season reaches one of them via a GENUINE import/availability transition
# (``mark_completed``/``mark_available``, which pass ``stamp_completion=True``) --
# NOT via the ``ensure_seasons`` creation path (see there). ``evicted`` is
# deliberately EXCLUDED: its file is gone, so an eviction-time rollup must never be
# mistaken for a fresh completion -- and the eviction path never stamps anyway (it
# runs with ``tolerate_active_conflict=True`` AND the default
# ``stamp_completion=False``; see _recompute_parent).
_REAL_DONE_SEASON_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value for s in (RequestStatus.completed, RequestStatus.available)
)

# The single status :func:`wake_waiting_for_air_date` re-checks (issue #210). A
# season parked here by ``request_service._resolve_tv_season_plan`` (an explicit
# season above the show's TMDB ``season_count`` at request time, or the
# zero-season whole-show placeholder S1) is excluded from
# ``auto_grab_service.DUE_SEARCH_STATUSES`` and, before this function existed, only
# ever left that state via a fresh ``create_request``/dedup call re-running
# ``ensure_seasons``. A ``frozenset`` (not a bare string) so it composes directly
# with :meth:`SeasonRequestRepository.list_for_airing_refresh`'s ``statuses``
# parameter, mirroring ``season_episode_service._REARMABLE_DONE_STATUSES``.
_WAITING_FOR_AIR_DATE_STATUSES: Final[frozenset[str]] = frozenset(
    {RequestStatus.waiting_for_air_date.value}
)

# Minimum age between air-date wake re-checks of the SAME still-future season (P2
# finding, issue #210). :func:`wake_waiting_for_air_date` runs every auto-grab
# cycle (~60s), but a season TMDB does not yet report has nothing to gain from a
# per-minute ``get_tv_show`` re-check -- a brand-new season announcement is an
# hours-scale event, not a per-minute one. Without this, an install whose waiting
# rows fit inside ``AIR_DATE_WAKE_MAX_PER_CYCLE`` would re-select and re-query the
# exact same future rows every cycle (the rotation stamp alone can't throttle a
# set that fits in one window). The cutoff is applied as
# ``list_for_airing_refresh(checked_before=now - interval)`` so a row checked more
# recently than this is skipped outright; a never-checked row is always eligible,
# so a newly-parked season is still picked up on the very next cycle. Deliberately
# a flat age cutoff (not an air-date-proximity tier): the season air date is not
# persisted, so a finer near/far schedule would need its own next-check column
# (migration) -- out of scope here; the flat cutoff already delivers the intended
# "hours, not minutes" budget protection.
_AIR_DATE_WAKE_MIN_INTERVAL: Final[timedelta] = timedelta(hours=6)

# Season statuses from which a park to ``no_acceptable_release`` is a SAFE,
# honest search-exhausted verdict -- the CAS's ``allowed_from`` for
# :func:`mark_no_acceptable_release` below (issue #72). Mirrors
# ``request_service._PARKABLE_REQUEST_STATUS_VALUES`` at season granularity
# (duplicated, not imported, for the SAME circular-import reason as
# ``_TERMINAL_SEASON_STATUS_VALUES`` above). Excludes every TERMINAL season
# status (the never-un-terminate guard: writing ``no_acceptable_release`` --
# itself non-terminal and dedup-blocking -- over a finished season would
# resurrect it as a ghost) PLUS two additional non-terminal statuses a parking
# transition must never stomp:
#   - ``downloading`` -- closes issue #72's race: before this CAS existed,
#     ``mark_no_acceptable_release`` read the season's current status, checked
#     it was non-terminal, then wrote unconditionally; a concurrent grab (a
#     lower-ranked auto-grab candidate, a manual re-grab) moving the season to
#     ``downloading`` in that gap would be silently regressed back to a
#     dead-end. The CAS's ``WHERE status IN (...)`` is evaluated by the
#     DATABASE at write time, never against a stale read.
#   - ``import_blocked`` -- a season CAN reach this directly
#     (``import_service._import_tv_locked``'s failure path): a DIFFERENT
#     needs-attention dead-end that a stale nothing-acceptable signal must
#     never paper over.
_PARKABLE_SEASON_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value
    for s in (
        RequestStatus.pending,
        RequestStatus.searching,
        RequestStatus.no_acceptable_release,
    )
)


async def _recompute_parent(
    session: AsyncSession,
    media_request_id: int,
    *,
    tolerate_active_conflict: bool = False,
    stamp_completion: bool = False,
) -> None:
    """Re-read every tracked season's status, fold via ``rollup_status``, persist.

    Called after EVERY season-status transition, in the SAME transaction as the
    transition itself: the parent ``MediaRequest.status`` is a pure fold of its
    children, never independently authoritative for a TV request. A show with no
    tracked seasons yet (should not happen once ``ensure_seasons`` has run at
    least once) is a no-op rather than a crash -- ``rollup_status`` itself raises
    on an empty sequence, so guard before calling it.

    Besides the rollup ``status``, this also stamps the parent's ``completed_at``
    the first time a tracked season is imported/on disk (``completed``/
    ``available``) -- the TV analogue of the movie-level stamp in
    ``mark_completed``/``mark_available`` (which a computed TV rollup never runs),
    so ``MediaRequest.completed_at`` is no longer permanently ``None`` for a TV
    request. That stamp is gated on ``stamp_completion`` (default ``False``),
    passed ``True`` ONLY by :func:`mark_completed`/:func:`mark_available` -- a
    GENUINE import/availability transition -- and left ``False`` by every other
    caller, crucially :func:`ensure_seasons`: a request that MIXES an
    already-in-Plex season (which ``ensure_seasons`` creates ``available`` with NO
    ``library_path`` -- no import ever ran) with a still-missing one must NOT
    record a completion at REQUEST time, or the later-imported season's
    time-to-watch interval would start from request/Plex-verification time rather
    than its own import completion (retention_telemetry_service). See the inline
    comment on the stamp for the season-level (not rollup-level) check and why it
    is confined to the strict, non-eviction branch.

    ``tolerate_active_conflict`` (default ``False`` -- unchanged for every normal
    season transition, which must keep recomputing the rollup STRICTLY so dedup
    never silently weakens): an OLD, already-SETTLED parent (rollup ``available``,
    outside ``uq_media_requests_active``) can legitimately coexist with a NEWER,
    genuinely active ``MediaRequest`` for the SAME ``(tmdb_id, media_type)`` (e.g.
    a fresh request for a later season after the old one finished) -- see
    ``RequestStatus.evicted``'s docstring and ``models.uq_media_requests_active``.
    Evicting one of the old parent's remaining seasons folds its rollup back to
    the active ``partially_available`` (``[evicted, available] ->
    partially_available``, ``domain/season_rollup``), which would then collide
    with the newer active row's slot in that same partial unique index. Only
    ``eviction_service._evict_one`` opts into tolerating that: the season's own
    CAS to ``evicted`` (+ its history row) is the source of truth for "the file
    is gone" and MUST survive regardless of whether this coarser, best-effort
    parent-rollup write succeeds -- so it runs inside its own SAVEPOINT
    (``session.begin_nested()``) and an ``IntegrityError`` from JUST this write is
    caught, logged, and discarded: the parent is left at its PRIOR (still
    accurate -- the newer request is the authoritative active row for this show)
    status, and the season CAS/history in the OUTER transaction is untouched and
    still commits normally.
    """
    season_repo = SqlSeasonRequestRepository(session)
    seasons = await season_repo.list_for_request(media_request_id)
    if not seasons:
        return
    status = rollup_status([season.status for season in seasons])
    request_repo = SqlRequestRepository(session)
    if not tolerate_active_conflict:
        await request_repo.set_status(media_request_id, status)
        # Stamp the parent's FIRST-completion timestamp when a GENUINE import/
        # availability transition (``mark_completed``/``mark_available``, which pass
        # ``stamp_completion=True``) has landed a tracked season on disk
        # (``completed``/``available``). A TV ``MediaRequest.status`` is a pure fold
        # of its seasons and never goes through the movie-level ``mark_completed``/
        # ``mark_available`` that stamp ``completed_at`` directly, so without this the
        # parent's ``completed_at`` stays ``None`` forever and every TV time-to-watch
        # interval reads "unknown" (retention_telemetry_service). Gated on
        # ``stamp_completion`` so the ``ensure_seasons`` CREATION path never fires it:
        # an already-in-Plex season is created ``available`` with NO ``library_path``
        # (no import ran), and stamping at that request-time moment would start a
        # later-imported sibling's time-to-watch interval from verification time
        # rather than its own import. Checked at the SEASON level, not off ``status``:
        # rollup precedence (``downloading`` etc.) can mask a just-completed season
        # while a sibling is still in flight, but the show's first completion has
        # still happened. Idempotent via the ``is None`` guard in the repo -- a later
        # season completing never moves the first stamp. Only in this strict
        # (forward-transition) branch: the ``tolerate_active_conflict`` path below is
        # eviction's alone, and an eviction (file gone) is never a completion -- see
        # ``_REAL_DONE_SEASON_STATUS_VALUES``.
        if stamp_completion and any(
            season.status in _REAL_DONE_SEASON_STATUS_VALUES for season in seasons
        ):
            await request_repo.stamp_completed_at_if_unset(media_request_id)
        return
    try:
        async with session.begin_nested():
            await request_repo.set_status(media_request_id, status)
    except IntegrityError:
        # A NEWER active request for the same (tmdb_id, media_type) already holds
        # ``uq_media_requests_active``'s slot -- the parent rollup write collided,
        # not the season CAS that triggered it. Only THIS savepoint is undone (the
        # parent keeps its prior, still-honest status); the caller's season CAS +
        # history row are unaffected and still commit. Never silently swallowed --
        # logged so an operator can see the coarse rollup is momentarily stale.
        _logger.warning(
            "parent rollup write skipped: a newer active "
            "request already occupies the active-dedup slot for this show; the "
            "season's own status/history are unaffected",
            extra={"request_id": safe_int(media_request_id)},
        )


async def _present_seasons(library: LibraryPort, tmdb_id: int) -> frozenset[int]:
    """The show's already-in-Plex seasons in ONE crawl; an error is an explicit empty set.

    Mirrors ``request_service._already_in_library``'s best-effort posture, but for
    the WHOLE show at once: a transient Plex outage or the still-partial per-episode
    ``NotImplementedError`` must never block tracking, so a failure is logged and
    treated as "can't prove anything is already there" -- every season falls through
    to ``pending`` -- an explicit decision, not a swallowed error.

    ``present_seasons`` always reflects the library as it is NOW (like
    ``is_available(use_cache=False)`` -- a season just REMOVED reads absent on a fresh
    ``ensure_seasons`` call, never a stale cached "present"). Resolving all seasons
    from ONE crawl (vs ``is_available`` per season) keeps ``ensure_seasons`` from
    holding the request's SQLite write transaction open across N full library reads.
    """
    try:
        return await library.present_seasons(tmdb_id)
    except (PlexLibraryError, PlexAuthError, NotImplementedError) as exc:
        _logger.warning(
            "plex season-availability crawl failed (%s); proceeding with a request",
            type(exc).__name__,
            extra={"tmdb_id": safe_int(tmdb_id)},
        )
        return frozenset()


async def ensure_seasons(
    session: AsyncSession,
    library: LibraryPort | None,
    *,
    media_request_id: int,
    tmdb_id: int,
    seasons: list[int],
    force_pending: bool = False,
) -> list[SeasonRequestRecord]:
    """Idempotently create every season row in ``seasons``, then recompute the rollup.

    Per season: when ``library`` is supplied and Plex already has that season
    (it is in the single ``present_seasons`` snapshot taken up front), the row is
    created straight to ``available`` rather than ``pending`` -- a season already in
    the library skips search/grab, exactly mirroring ``create_request``'s
    already-in-library short-circuit for movies, but PER SEASON. An
    unconfigured/unreachable Plex (or the still-partial per-episode check) is treated
    as "not proven available", so the season is created ``pending`` and search
    proceeds normally.

    ``force_pending`` is used for explicit episode requests. Plex's current season
    presence API only proves "some episode in this season exists", not that the
    requested episode numbers are present, so those requests deliberately ignore
    season-level presence and create/re-arm the season as searchable ``pending``.

    ``SeasonRequestRepository.ensure`` never re-applies ``status`` to an
    already-established season, so calling this on EVERY ``create_request`` call
    for a tv media_type -- including the dedup path, with a possibly-DIFFERENT
    season list -- only ever ADDS newly-named seasons; it never regresses one
    already in flight or finished. The ONE exception (C3, ADR-0012) is an
    ``evicted`` season: see the re-arm note below.

    The parent rollup is recomputed ONCE after every season in ``seasons`` has
    been ensured, not once per season -- cheaper, and avoids persisting
    transient intermediate rollups.

    Plex is crawled ONCE per call (``_present_seasons``), not once per season: the
    per-season already-in-library decision is a membership test against that single
    fresh snapshot, so a whole-series request never re-pages the library N times
    inside the held write transaction.

    Re-requesting an EVICTED season (ADR-0012): when the show has a mix of
    seasons (some still ``available``/``completed``, one ``evicted``), the
    parent's rollup is the non-terminal, active ``partially_available`` -- so
    ``create_request``'s dedup finds the EXISTING ``MediaRequest`` and calls this
    function again for the evicted season number, rather than creating a fresh
    request (that fresh-row path is how a WHOLLY evicted show, rollup
    ``evicted``, gets re-requested instead -- see ``request_service.
    TERMINAL_REQUEST_STATUS_VALUES`` / ``_SETTLED_REQUEST_STATUSES``). Because
    the season row ALREADY EXISTS, ``ensure()``'s get-or-create returns it
    UNCHANGED -- without the re-arm below, "Request again" on that one season
    would be a silent no-op forever (the sweep already deleted the file; nothing
    would ever re-search/re-grab it). So: a season that comes back ``evicted``
    from ``ensure()`` is explicitly re-armed to exactly the status a FRESH row
    would have gotten just above -- and because ``evicted_seasons`` removes a
    just-reclaimed season from ``trusted_present`` (its Plex 'present' reading is
    STALE during the eviction delete window; see the inline comment above), that
    status is ``pending`` so search/grab re-fetches the file, NEVER ``available``
    over a file the sweep is about to (or just did) delete -- mirroring how
    re-requesting an evicted MOVIE creates a fresh grabbable row. Scoped
    EXCLUSIVELY to ``evicted``: ``ensure()`` never constructs a NEW row with that
    status, so reaching it here always means a pre-existing row, and every other
    terminal/in-flight season status (``available``/``completed``/``failed``/
    ``searching``/``downloading``/``import_blocked``/``no_acceptable_release``/
    ``pending``) is left completely untouched -- an in-flight or already-finished
    season is never regressed by a re-request.

    The re-arm itself is a CAS from exactly the ``evicted`` that was read, not
    an unconditional write: the eviction sweep's recovery pass can RESTORE the
    row to ``available`` (its file is present -- an interrupted purge that never
    actually deleted anything) between ``ensure()``'s read and the re-arm, and
    clobbering that back to ``pending`` would queue a duplicate download of
    on-disk content that recovery (already past) could not catch until a whole
    sweep later. A lost re-arm CAS takes the honest branch for the row's CURRENT
    status: for a recovery-restored ``available`` row that is the same no-op
    every other non-evicted status gets -- the re-request simply dedups onto the
    watchable season, exactly like an already-in-Plex one.

    That re-arm (#75) does not stop at ``set_status``: it also resets the search
    backoff ladder (:func:`reset_for_research`'s
    ``schedule_search(search_attempts=0, next_search_at=None)``, cited there as
    ADR-0013/ADR-0014) so a stale ``search_attempts``/``next_search_at`` from the
    run that led to eviction cannot throttle the operator's brand-new request the
    way it never would a fresh row.

    It deliberately does NOT clear the ``library_path`` eviction breadcrumb -- the
    one place it diverges from report-issue's ``reset_for_research`` (whose file is
    already purged by the time it re-arms). This re-arm runs while the eviction
    purge outcome is still UNKNOWN: the claim commits ``evicted`` + breadcrumb
    BEFORE the delete (ADR-0012 #67), so a same-row re-request landing in that
    window finds the file still on disk. The breadcrumb is owned end-to-end by the
    eviction lifecycle -- the finalize clears it ONLY after a successful delete
    (``_STALE_SEASON_BREADCRUMB_CLEAR_STATUSES`` covers the re-armed pre-grab
    statuses, so a successful purge still clears it exactly once); a failed-delete
    ``_restore_after_failed_delete`` fold KEEPS it (upholding the invariant that an
    ``available`` row over a live file ALWAYS carries its breadcrumb, or disk
    pressure / report-issue could never reclaim it); crash recovery finalizes or
    releases it. Clearing it here would fold a season back to ``available`` over a
    still-present file with no eviction/report handle whenever the in-flight purge
    then refuses/errors -- exactly the leak this keeps closed (#117).

    The re-arm ALSO heals the parent's ``completed_at``
    (:meth:`SqlRequestRepository.heal_completed_at` -- the same guarded verb
    :func:`reset_for_research` uses, sharing its invariant): an evicted season's
    OLD completion may be the only thing the parent's stamp records, and eviction
    itself deliberately never clears it (a reclaimed file is not an un-completion
    -- the show's first completion remains a historical fact while the show sits
    settled). But the moment the operator RE-REQUESTS that season, a stale stamp
    becomes a trap: ``stamp_completed_at_if_unset``'s ``IS NULL`` guard means the
    re-import could never re-stamp, exactly the #76 gap at the eviction re-arm
    entrance. The heal clears the stamp ONLY when no genuinely-imported
    ``completed``/``available`` sibling still backs it (e.g. S1 evicted + S2
    pending -> cleared, so S1's re-import re-stamps; S2 imported-and-done ->
    preserved, the first-completion fact is still true). Gated to the re-arm case
    -- plain creations never touch the stamp, keeping the R5 rule that request
    time NEVER records a completion.

    When Plex still reports an evicted season present, ``trusted_present`` below
    deliberately subtracts that just-evicted season, so the re-arm is ``pending``
    rather than a presence-derived ``available``. The season's PRE-EVICTION
    ``imported`` ``Download`` row survives eviction (eviction never mutates the
    downloads aggregate), but the heal's download-evidence arm discounts any import
    with a LATER ``evicted`` history event for the show -- so stale download
    evidence cannot resurrect a ``completed_at`` that no current import supports.
    """
    present: frozenset[int] = (
        await _present_seasons(library, tmdb_id)
        if library is not None and not force_pending
        else frozenset()
    )
    season_repo = SqlSeasonRequestRepository(session)
    # Never trust a fresh Plex 'present' reading for a season the disk-pressure
    # sweep most recently reclaimed (ADR-0012): its row is committed 'evicted'
    # BEFORE its file is unlinked and BEFORE the post-delete Plex refresh, so for
    # that whole window Plex still lists the doomed / just-removed file. Creating
    # or re-arming the season straight to 'available' off that stale reading would
    # leave it marked watchable over a file the sweep then deletes (the season-level
    # twin of the movie P1 closed in ``request_service``). Subtract those seasons so
    # they re-grab ('pending') instead -- queried UNCONDITIONALLY (not gated on
    # ``present`` being non-empty): an empty ``present`` already subtracts to
    # itself, and the eviction-regrab marker below needs the full set regardless of
    # what Plex reported (see the finding-2 note).
    # The season-level provenance marker (issue #156; hardened by the Codex
    # round-2 finding below): a season whose newest tracked history is ``evicted``
    # is exactly this function's OWN eviction-guard re-grab (a NEW season row
    # created 'pending' instead of a stale-Plex 'available' -- the
    # wholly-evicted-show shape, tracked under a fresh ``MediaRequest``) --
    # regardless of whether THIS call's own Plex crawl actually reported the
    # season present. Queried UNCONDITIONALLY (not only when ``present`` is
    # non-empty): during the eviction claim/delete window Plex can just as easily
    # ERROR (``_present_seasons``' best-effort empty set) or correctly report the
    # season already gone as it can still list it, and in every one of those
    # shapes a season in ``evicted_seasons`` that this call is about to create
    # fresh is STILL an in-window eviction regrab -- under-stamping it would leave
    # a genuine duplicate invisible to the restore's dedup
    # (``eviction_service._cancel_redundant_season_regrabs``). A season simply
    # never in ``evicted_seasons`` at all is an ordinary create, unrelated to
    # eviction, and must NOT carry the marker.
    evicted_seasons = await season_repo.evicted_seasons(tmdb_id)
    trusted_present = present - evicted_seasons
    evicted_regrab_seasons = evicted_seasons
    records: list[SeasonRequestRecord] = []
    needs_completed_at_heal = False
    for season_number in seasons:
        initial_status = (
            RequestStatus.available.value
            if season_number in trusted_present
            else RequestStatus.pending.value
        )
        record = await season_repo.ensure(
            media_request_id,
            season_number,
            status=initial_status,
            eviction_regrab=season_number in evicted_regrab_seasons,
        )
        if record.status == RequestStatus.evicted.value:
            # The re-arm is a CAS from EXACTLY the status ensure() just read --
            # the same write discipline as every other status move in the
            # eviction lifecycle. The eviction recovery can restore this row to
            # 'available' (its file is present -- an interrupted purge that
            # never actually deleted anything) between the ensure() read and
            # this write; an unconditional re-arm would clobber that live-file
            # row back to 'pending', and with the sweep's recovery pass already
            # past, auto-grab would download a duplicate until a later sweep
            # folded it again. A lost CAS is honored, never overwritten: the
            # re-read below returns the row at its CURRENT status, and the
            # honest branch for a recovery-restored 'available' row is exactly
            # the no-op every other non-evicted status already takes -- the
            # re-request dedups onto it, precisely like an already-in-Plex
            # season.
            rearmed = await season_repo.set_status_if_in(
                record.id, initial_status, frozenset({RequestStatus.evicted.value})
            )
            if rearmed:
                # Reset ONLY the search backoff ladder so the operator's fresh
                # request is not throttled by the evicted run's exhausted attempts.
                # The ``library_path`` eviction breadcrumb is deliberately LEFT
                # ALONE: this re-arm can land while the eviction purge is still
                # in flight (the claim commits 'evicted' + breadcrumb BEFORE the
                # delete), so the file may still be on disk. The breadcrumb is
                # owned by the eviction lifecycle -- the finalize clears it after a
                # successful delete, a failed-delete restore/fold keeps it, and
                # crash recovery finalizes/releases it -- so clearing it here would
                # strand a folded-back live season with no eviction/report handle
                # (#117). See this function's docstring for the full rationale.
                await season_repo.schedule_search(record.id, search_attempts=0, next_search_at=None)
                needs_completed_at_heal = True
            record = await season_repo.get(record.id) or record
        elif record.status == RequestStatus.waiting_for_air_date.value:
            rearmed = await season_repo.set_status_if_in(
                record.id,
                initial_status,
                frozenset({RequestStatus.waiting_for_air_date.value}),
            )
            if rearmed:
                await season_repo.schedule_search(record.id, search_attempts=0, next_search_at=None)
            record = await season_repo.get(record.id) or record
        elif force_pending and record.status in _REAL_DONE_SEASON_STATUS_VALUES:
            rearmed = await season_repo.set_status_if_in(
                record.id,
                RequestStatus.pending.value,
                _REAL_DONE_SEASON_STATUS_VALUES,
            )
            if rearmed:
                await season_repo.schedule_search(record.id, search_attempts=0, next_search_at=None)
                needs_completed_at_heal = True
            record = await season_repo.get(record.id) or record
        records.append(record)
    if needs_completed_at_heal:
        # After the loop (not per season) so the heal sees every re-armed season's
        # new status; see the docstring for why only a re-arm triggers it.
        await SqlRequestRepository(session).heal_completed_at(media_request_id)
    await _recompute_parent(session, media_request_id)
    return records


async def wake_waiting_for_air_date(
    session: AsyncSession,
    metadata: MetadataPort,
    library: LibraryPort | None,
    *,
    now: datetime,
    max_refresh: int,
) -> int:
    """Re-check a bounded, rotating slice of ``waiting_for_air_date`` seasons
    against TMDB and wake the ones it now reports (issue #210).

    A season is parked ``waiting_for_air_date`` by ``request_service.
    _resolve_tv_season_plan`` when it is either an EXPLICIT season requested
    above the show's TMDB ``season_count`` at request time, or the zero-season
    whole-show placeholder S1. That status is deliberately excluded from
    ``auto_grab_service.DUE_SEARCH_STATUSES``, so before this function existed the
    ONLY way out of it was another ``create_request``/dedup call re-running
    :func:`ensure_seasons`. This function is the periodic twin of that wake,
    called from the auto-grab cycle: the wake condition is the exact INVERSE of
    the parking rule -- ``season_number <= season_count`` -- re-derived from a
    FRESH ``metadata.get_tv_show`` call (never the stale count recorded at
    request time).

    Best-effort PER SHOW: a TMDB error (``TmdbApiError``/``TmdbAuthError``) or an
    unresolvable show (``get_tv_show`` returns ``None``) means "can't prove this
    season has aired yet" -- the row is left ``waiting_for_air_date`` (honest,
    retryable next rotation), never guessed awake. An error for one show never
    aborts the pass; ``get_tv_show`` is resolved AT MOST ONCE per distinct
    ``tmdb_id`` in this call (a whole-show placeholder or several waiting seasons
    of the same show share one lookup), and once a show has errored every other
    waiting season of that SAME show is skipped for the rest of THIS pass
    (stamped, not re-queried) rather than repeating a lookup already known to fail.

    Bounded and ROTATING via the same cursor :func:`plex_manager.services.
    season_episode_service.reconcile_airing` uses
    (``SeasonRequest.airing_refresh_checked_at``, :meth:`SeasonRequestRepository.
    list_for_airing_refresh` / :meth:`~SeasonRequestRepository.
    mark_airing_refresh_checked`): safe to share because ``waiting_for_air_date``
    and ``reconcile_airing``'s ``available``/``completed`` candidate set are
    DISJOINT, so the two passes can never starve or double-stamp each other.
    EVERY examined candidate is stamped -- woken, still-future, unresolvable, or
    errored -- so the bounded per-cycle window still eventually revisits every
    waiting row instead of permanently starving whichever ones don't fit in the
    first ``max_refresh``-sized slice.

    The set passed to :func:`ensure_seasons` mirrors the parent's ORIGINAL intent
    (looked up ONCE per distinct ``media_request_id`` and memoised for the pass):

    * An EXPLICIT-season / explicit-episode request wakes exactly its ONE parked
      ``season.season_number`` -- never widened, so it gains no season the operator
      did not ask for (issue #210 acceptance: "Unrequested later seasons are not
      added").
    * The ZERO-season WHOLE-SHOW placeholder (``tv_request_mode == "whole_show"``,
      parked as S1 by ``_resolve_tv_season_plan``'s ``default_waiting_first_season``
      because TMDB reported ``season_count == 0`` at request time) keeps "whole
      show" intent, so when TMDB later reports ``season_count >= 1`` it expands to
      the fresh ``1..season_count`` set -- the SAME resolution the fresh-create path
      would have produced -- instead of stranding S2.. untracked until a manual
      re-request (P2 finding).
    * When the parked season is EPISODE-scoped for the parent (the parent's
      ``requested_episodes`` names episodes for THIS season), the wake is forced
      ``pending`` (``force_pending=True``) exactly like the request-create path: a
      partial Plex presence for the season (e.g. S2E1 present, S2E10 requested)
      proves only "some episode exists", never that the requested episodes are, so
      the season must NOT wake straight to ``available`` and skip the search that
      still owes those episodes (P2 finding).

    The actual transition -- Plex-present straight to ``available`` (only when NOT
    episode-scoped), else ``pending``, plus the search backoff reset and the parent
    rollup recompute -- is entirely delegated to :func:`ensure_seasons` (the SAME
    CAS-based re-arm the request-create/dedup wake path already uses), so this
    function owns none of that logic itself.

    Each still-future season is re-checked at most once per
    :data:`_AIR_DATE_WAKE_MIN_INTERVAL` (a due cutoff threaded into
    :meth:`SeasonRequestRepository.list_for_airing_refresh` as ``checked_before``),
    so an idle install with a handful of parked seasons re-queries TMDB on an
    hours-scale cadence rather than once per ~60s cycle (P2 finding).

    FLUSH-ONLY (module convention): the caller (``auto_grab_service.
    run_grab_cycle``) owns the commit boundary, exactly like :func:`ensure_seasons`
    and ``season_episode_service.reconcile_airing``.

    Returns the count of seasons this call actually transitioned OUT OF
    ``waiting_for_air_date`` -- a CAS lost to a concurrent re-request (or a
    concurrent second wake) is not counted, mirroring ``reconcile_airing``'s
    ``rearmed`` return.
    """
    season_repo = SqlSeasonRequestRepository(session)
    request_repo = SqlRequestRepository(session)
    candidates = await season_repo.list_for_airing_refresh(
        _WAITING_FOR_AIR_DATE_STATUSES,
        limit=max_refresh,
        checked_before=now - _AIR_DATE_WAKE_MIN_INTERVAL,
    )

    woken = 0
    show_season_count: dict[int, int | None] = {}
    errored_shows: set[int] = set()
    parents: dict[int, RequestRecord | None] = {}
    for season in candidates:
        if season.tmdb_id in errored_shows:
            await season_repo.mark_airing_refresh_checked(season.id, now)
            continue

        if season.tmdb_id not in show_season_count:
            try:
                tv = await metadata.get_tv_show(season.tmdb_id)
            except (TmdbApiError, TmdbAuthError) as exc:
                _logger.warning(
                    "auto-grab: air-date wake tv-show lookup failed (%s); leaving season waiting",
                    type(exc).__name__,
                    extra={
                        "request_id": safe_int(season.media_request_id),
                        "tmdb_id": safe_int(season.tmdb_id),
                    },
                )
                errored_shows.add(season.tmdb_id)
                await season_repo.mark_airing_refresh_checked(season.id, now)
                continue
            show_season_count[season.tmdb_id] = tv.season_count if tv is not None else None

        season_count = show_season_count[season.tmdb_id]
        if season_count is not None and season.season_number <= season_count:
            if season.media_request_id not in parents:
                parents[season.media_request_id] = await request_repo.get(season.media_request_id)
            parent = parents[season.media_request_id]
            # A zero-season whole-show placeholder keeps "whole show" intent, so it
            # expands to the freshly-reported 1..season_count set; every other parked
            # row (explicit season or explicit episode) wakes ONLY itself. See the
            # docstring for why explicit rows must never be widened.
            if parent is not None and parent.tv_request_mode == "whole_show":
                wake_seasons = list(range(1, season_count + 1))
            else:
                wake_seasons = [season.season_number]
            # Episode-scoped for THIS season -> the same ``force_pending`` guard the
            # request-create path uses: partial Plex presence proves "some episode",
            # never the requested episodes, so the season must not wake straight to
            # ``available`` and skip the still-owed search (see the docstring). The
            # per-season gate mirrors ``season_episode_service.reconcile_airing``'s.
            episode_scoped = bool(
                parent is not None
                and parent.requested_episodes
                and parent.requested_episodes.get(season.season_number)
            )
            records = await ensure_seasons(
                session,
                library,
                media_request_id=season.media_request_id,
                tmdb_id=season.tmdb_id,
                seasons=wake_seasons,
                force_pending=episode_scoped,
            )
            if any(
                record.season_number == season.season_number
                and record.status != RequestStatus.waiting_for_air_date.value
                for record in records
            ):
                woken += 1
        # Stamped regardless of outcome (woken, still-future, or an unresolvable
        # show) so the bounded window rotates -- see the docstring.
        await season_repo.mark_airing_refresh_checked(season.id, now)

    return woken


async def set_status(
    session: AsyncSession,
    *,
    media_request_id: int,
    season_number: int,
    status: str,
    skip_if_terminal: bool = False,
) -> None:
    """Update ONE season's status, then recompute + persist the parent rollup.

    The season-aware counterpart of ``SqlRequestRepository.set_status`` calling
    directly: ``grab_service.grab`` and ``queue_service`` (``_handle_failed`` /
    ``mark_failed``) hold only a ``(media_request_id, season)`` tuple off a
    ``Download`` row, so the row is resolved via ``ensure()`` (idempotent
    get-or-create, defaulting to ``pending`` on first creation -- immediately
    overwritten by this call's own ``status`` write below) rather than requiring
    a pre-known ``SeasonRequest`` id.

    ``skip_if_terminal`` (default ``False`` -- unchanged for ``grab_service``'s
    forward-progress move to ``downloading``, which MUST be able to reopen an
    already-``available``/``completed`` season to chase one more missing
    episode): when ``True``, a season already FINISHED (``completed`` /
    ``available`` / ``failed``) is left completely untouched instead of being
    overwritten with ``status`` -- the SAME never-un-terminate posture
    :func:`mark_no_acceptable_release` applies. ``queue_service``'s failure
    re-arm call sites (``_handle_failed`` / ``mark_failed``) opt into this: a
    season a PRIOR download already finished must never be dragged back to
    ``searching`` by a LATER, unrelated download for that same season (e.g. a
    supplementary per-episode re-grab) failing. This is a deliberate choice, not
    a blind copy of that guard -- the FAILING download's own row still moves to
    ``Failed`` (and is optionally blocklisted) by the caller regardless of this
    skip, so that attempt's outcome remains fully visible in the queue/history;
    only the coarser season-level rollup is protected from regressing past a
    state Plex has already confirmed.
    """
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    if skip_if_terminal and row.status in _TERMINAL_SEASON_STATUS_VALUES:
        return
    await season_repo.set_status(row.id, status)
    await _recompute_parent(session, media_request_id)


async def set_status_if_in(
    session: AsyncSession,
    *,
    media_request_id: int,
    season_request_id: int,
    status: str,
    allowed_from: frozenset[str],
    require_parent_unpinned: bool = False,
    tolerate_active_conflict: bool = False,
) -> bool:
    """Compare-and-swap ONE season's status, recomputing the parent rollup ONLY when
    the swap actually happened. Returns whether it happened.

    The CAS-aware counterpart of :func:`set_status`, for a caller that already
    holds the season's own id (not just a ``(media_request_id, season_number)``
    tuple) and needs the database -- not this session's in-memory view -- to be
    the authority on whether the transition still applies:
    ``eviction_service._evict_one`` is the reason this exists (ADR-0012, C6) --
    see ``SqlSeasonRequestRepository.set_status_if_in``'s docstring for the full
    double-count race it closes. A losing CAS (``False``) must never recompute
    (and persist) a rollup derived from a row it did not actually get to move.

    ``require_parent_unpinned`` (opt-in for the eviction CLAIM, #67) is passed
    straight through to the repository CAS: it folds the PARENT show's
    ``keep_forever`` pin into the compared predicate (via a correlated subquery),
    so a pin landing on the show before the claim atomically refuses the swap --
    the DATABASE, not a read-then-act check, is what stops a freshly-pinned show's
    season from being evicted. ``eviction_service._evict_one`` opts in for its
    pre-delete claim.

    ``tolerate_active_conflict`` (default ``False``, strict for every ordinary
    caller) is passed straight through to :func:`_recompute_parent` -- see its
    docstring. ``eviction_service._evict_one`` is the ONLY caller that opts in
    (``True``): the season CAS above (and its history row, written by the
    caller after this returns) is the authoritative "the file is gone" record
    and must survive even when the coarser parent-rollup write below it
    collides with a newer active request for the same show.
    """
    changed = await SqlSeasonRequestRepository(session).set_status_if_in(
        season_request_id, status, allowed_from, require_parent_unpinned=require_parent_unpinned
    )
    if changed:
        await _recompute_parent(
            session, media_request_id, tolerate_active_conflict=tolerate_active_conflict
        )
    return changed


async def set_library_path(
    session: AsyncSession, *, media_request_id: int, season_number: int, library_path: str
) -> None:
    """Persist the final placed directory for one season (ADR-0012's eviction breadcrumb).

    The season-level analogue of ``SqlRequestRepository.set_library_path``: called
    once at import finalize (``import_service._import_tv_locked``, the SAME
    transaction as :func:`mark_completed`), this is what lets a later disk-pressure
    sweep discover an ``fs.delete()`` target for the season --
    ``eviction_service._season_candidates`` reads ``SeasonRequest.library_path``
    verbatim. Without this call the column stays ``None`` forever and the sweep
    skips the season, logged, never guessing a path (see
    ``eviction_service._evict_one``).

    Resolves the row via ``ensure()`` (idempotent get-or-create) rather than
    requiring a pre-known ``SeasonRequest`` id, mirroring every other function
    here. Does NOT recompute the parent rollup -- the library path never feeds
    ``rollup_status``, so touching it is not a status transition.
    """
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    await season_repo.set_library_path(row.id, library_path)


async def set_installed_quality(
    session: AsyncSession,
    *,
    media_request_id: int,
    season_number: int,
    quality_id: int,
    profile_index: int | None,
) -> None:
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    await season_repo.set_installed_quality(
        row.id,
        quality_id=quality_id,
        profile_index=profile_index,
    )


async def clear_library_path(
    session: AsyncSession, *, media_request_id: int, season_number: int
) -> None:
    """Drop one season's eviction/purge breadcrumb (ADR-0014's report-issue verb).

    The season-level analogue of ``SqlRequestRepository.clear_library_path`` and the
    counterpart of :func:`set_library_path`: report-issue re-arms the season (claiming
    the parent's active-dedup slot via :func:`reset_for_research` with
    ``clear_library_path=False``) BEFORE it knows whether the purge will succeed, then
    clears the breadcrumb HERE only once the file was actually removed. A failed/refused
    purge leaves the breadcrumb intact so the orphan stays reclaimable (honesty over
    silence).

    Resolves the row via ``ensure()`` (idempotent get-or-create) rather than requiring a
    pre-known ``SeasonRequest`` id. Does NOT recompute the parent rollup -- clearing
    ``library_path`` is not a status transition, so it never re-touches the parent's
    ``uq_media_requests_active`` slot.
    """
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    await season_repo.clear_library_path(row.id)


async def mark_completed(
    session: AsyncSession, *, media_request_id: int, season_number: int
) -> None:
    """Phase 1 of honest per-season availability: imported, scan triggered ("Finalizing").

    The season-level analogue of ``RequestRepository.mark_completed`` -- the
    season's file(s) are in the library and a Plex scan was triggered, but Plex
    has not yet confirmed the season is indexed. ``run_availability_cycle`` later
    confirms via ``is_available`` and promotes it (phase 2, :func:`mark_available`).
    """
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    await season_repo.mark_completed(row.id)
    await _recompute_parent(session, media_request_id, stamp_completion=True)


async def mark_completed_if_in(
    session: AsyncSession,
    *,
    media_request_id: int,
    season_number: int,
    allowed_from: frozenset[str],
) -> bool:
    """CAS counterpart of :func:`mark_completed`: move the season to
    ``completed`` only if its CURRENT persisted status is still in
    ``allowed_from`` (the DATABASE, not this session's snapshot, is the
    authority). Recomputes the parent rollup with ``stamp_completion=True``
    ONLY when the swap happened -- recomputing on a lost CAS would persist a
    rollup derived from a row this call did not actually move, the exact
    anti-pattern :func:`set_status_if_in` warns against. Returns whether the
    swap happened.

    The episode-fallback "aired target is already fully imported" completion
    shortcut (``auto_grab_service._attempt_episode_fallback``) uses this so a
    concurrent cancel/correction landing between its due-scope snapshot and
    this write can never be resurrected as ``completed`` (issue #229).
    """
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    changed = await season_repo.set_status_if_in(
        row.id, RequestStatus.completed.value, allowed_from
    )
    if changed:
        await _recompute_parent(session, media_request_id, stamp_completion=True)
    return changed


async def mark_available(
    session: AsyncSession, *, media_request_id: int, season_number: int
) -> None:
    """Phase 2 of honest per-season availability: Plex has confirmed the season is indexed."""
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    await season_repo.mark_available(row.id)
    await _recompute_parent(session, media_request_id, stamp_completion=True)


async def reset_for_research(
    session: AsyncSession,
    *,
    media_request_id: int,
    season_number: int,
    clear_library_path: bool = True,
) -> None:
    """Re-arm ONE reported season for a fresh search (ADR-0014's report-issue verb).

    The season-level analogue of ``SqlRequestRepository.reset_for_research``: sets
    the season back to the non-terminal ``searching`` then recomputes the parent
    rollup so the show reflects the re-armed season. Unlike :func:`set_status` this is
    UNCONDITIONAL (no ``skip_if_terminal``): report-issue deliberately re-opens an
    already-``available``/``completed`` season -- that is the whole point of "this
    imported file is bad, redo it".

    ``clear_library_path`` (default ``True``) also clears the season's ``library_path``
    purge breadcrumb -- correct when the file was actually deleted. report-issue passes
    ``False`` when the purge failed/was refused (the season directory may still be on
    disk): the breadcrumb is then PRESERVED so a later retry / eviction can still
    reclaim the orphan, never stranded with no handle (honesty over silence).

    Also HEALS the PARENT'S ``completed_at`` (#76) against its remaining seasons.
    Read against the movie path's own ``SqlRequestRepository.reset_for_research``,
    which unconditionally nulls ``completed_at`` because a movie is a single row:
    its completion claim is entirely invalidated the instant it is re-armed. A TV
    parent's ``completed_at`` is a DIFFERENT thing -- not "this row's own
    completion" but "the show's FIRST tracked season to complete" (a documented,
    known approximation; see ``retention_telemetry_service._candidate_context``
    and ``_recompute_parent``'s ``stamp_completion`` doc). Unconditionally
    clearing it here would make the honest case (another season is STILL
    genuinely complete/available, unaffected by this reset) regress to "unknown"
    for no reason. So the stamp goes through
    :meth:`SqlRequestRepository.heal_completed_at` (see its docstring for the
    full contract), whose INVARIANT this function shares: after the reset,
    ``completed_at`` is non-``NULL`` iff some tracked season GENUINELY completed
    an import AND is still ``completed``/``available``. "Genuinely imported"
    means the season has the ``library_path`` breadcrumb (written by
    ``import_service._import_tv_locked`` in the SAME transaction as
    ``mark_completed``) OR an ``imported`` ``Download`` row for its
    ``(media_request_id, season)`` -- the latter covering LEGACY seasons imported
    before the breadcrumb column existed (``models.SeasonRequest.library_path``),
    and invalidated by any LATER ``evicted`` history event for the show (a
    pre-eviction import is not current backing evidence -- see the heal's
    docstring for the ``download_history`` ordering, Codex round-3).
    A Plex-present-only season (``ensure_seasons``'s already-in-Plex creation:
    ``available``, NO breadcrumb, no grab ever ran) matches neither and never
    preserves a re-armed season's stale stamp (Codex P2 #1), so a redone season's
    re-import can always re-stamp via ``stamp_completed_at_if_unset``'s ``IS
    NULL`` guard. The predicate lives ONLY in the heal's own UPDATE ``WHERE``
    clauses -- re-asserted at UPDATE time, never read here into a Python snapshot
    (Codex P2 #2) -- and the heal's second (re-stamp) statement repairs the
    masked-sibling aftermath where a concurrently-committed sibling completion
    would otherwise be left stampless. The just-reset season is already flushed
    to ``searching`` above, so the heal correctly sees it as no longer done.
    """
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    await season_repo.set_status(row.id, RequestStatus.searching.value)
    # Fresh search, fresh backoff ladder (ADR-0013): the culprit's accrued
    # search_attempts must not throttle the operator's explicit redo.
    await season_repo.schedule_search(row.id, search_attempts=0, next_search_at=None)
    if clear_library_path:
        await season_repo.clear_library_path(row.id)
    # Issue #156 lifecycle fix (Codex round-2): the operator re-arming this row for
    # a BRAND-NEW search is the row leaving "some eviction's own in-flight regrab"
    # behind, whatever its provenance was before -- see
    # ``SqlSeasonRequestRepository.clear_eviction_regrab``'s docstring for the full
    # rationale (the movie twin of this clear lives directly in
    # ``SqlRequestRepository.reset_for_research``).
    await season_repo.clear_eviction_regrab(row.id)
    await SqlRequestRepository(session).heal_completed_at(media_request_id)
    await _recompute_parent(session, media_request_id)


async def mark_no_acceptable_release(
    session: AsyncSession, *, media_request_id: int, season_number: int
) -> bool:
    """Persist ``no_acceptable_release`` on one season when a grab finds nothing.

    The season-level analogue of ``request_service.mark_no_acceptable_release``:
    honesty over silence (a visible, retryable status, never a silent
    ``downloading``/``searching`` left lying), and now the SAME genuine
    compare-and-swap (issue #72) -- see that function's docstring for the full
    TOCTOU this closes (a concurrent grab winning the race between an old
    read-then-write's read and its write, and being silently regressed back to
    this dead-end). Delegates to :func:`set_status_if_in` (this module's CAS
    wrapper, which also recomputes the parent rollup, but ONLY when the swap
    actually happened) with ``_PARKABLE_SEASON_STATUS_VALUES`` as
    ``allowed_from`` -- see that constant's comment for exactly which statuses
    (every TERMINAL one, plus ``downloading`` / ``import_blocked``) a parking
    transition must never stomp.

    FLUSH-ONLY (module convention): the caller commits.

    Returns ``True`` if this call actually parked the season, ``False`` if a
    concurrent writer already moved it out of the parkable set -- the caller
    must treat ``False`` as "leave it alone, do not also write backoff
    metadata for a park that did not happen" (see
    ``auto_grab_service._park``).
    """
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    return await set_status_if_in(
        session,
        media_request_id=media_request_id,
        season_request_id=row.id,
        status=RequestStatus.no_acceptable_release.value,
        allowed_from=_PARKABLE_SEASON_STATUS_VALUES,
    )
