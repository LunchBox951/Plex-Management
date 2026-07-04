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
from typing import TYPE_CHECKING, Final

from sqlalchemy.exc import IntegrityError

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.domain.season_rollup import rollup_status
from plex_manager.logsafe import safe_int
from plex_manager.models import RequestStatus
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.repositories import SeasonRequestRecord

__all__ = [
    "clear_library_path",
    "ensure_seasons",
    "mark_available",
    "mark_completed",
    "mark_no_acceptable_release",
    "reset_for_research",
    "set_installed_quality",
    "set_library_path",
    "set_status",
    "set_status_if_in",
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
    would have gotten just above (``available`` if Plex already has it again,
    otherwise ``pending`` so search/grab picks it up) -- mirroring how
    re-requesting an evicted MOVIE creates a fresh grabbable row. Scoped
    EXCLUSIVELY to ``evicted``: ``ensure()`` never constructs a NEW row with that
    status, so reaching it here always means a pre-existing row, and every other
    terminal/in-flight season status (``available``/``completed``/``failed``/
    ``searching``/``downloading``/``import_blocked``/``no_acceptable_release``/
    ``pending``) is left completely untouched -- an in-flight or already-finished
    season is never regressed by a re-request.
    """
    present: frozenset[int] = (
        await _present_seasons(library, tmdb_id) if library is not None else frozenset()
    )
    season_repo = SqlSeasonRequestRepository(session)
    records: list[SeasonRequestRecord] = []
    for season_number in seasons:
        initial_status = (
            RequestStatus.available.value
            if season_number in present
            else RequestStatus.pending.value
        )
        record = await season_repo.ensure(media_request_id, season_number, status=initial_status)
        if record.status == RequestStatus.evicted.value:
            await season_repo.set_status(record.id, initial_status)
            record = await season_repo.get(record.id) or record
        records.append(record)
    await _recompute_parent(session, media_request_id)
    return records


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

    ``tolerate_active_conflict`` (default ``False``, strict for every ordinary
    caller) is passed straight through to :func:`_recompute_parent` -- see its
    docstring. ``eviction_service._evict_one`` is the ONLY caller that opts in
    (``True``): the season CAS above (and its history row, written by the
    caller after this returns) is the authoritative "the file is gone" record
    and must survive even when the coarser parent-rollup write below it
    collides with a newer active request for the same show.
    """
    changed = await SqlSeasonRequestRepository(session).set_status_if_in(
        season_request_id, status, allowed_from
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
    await _recompute_parent(session, media_request_id)


async def mark_no_acceptable_release(
    session: AsyncSession, *, media_request_id: int, season_number: int
) -> None:
    """Persist ``no_acceptable_release`` on one season when a grab finds nothing.

    The season-level analogue of ``request_service.mark_no_acceptable_release``:
    honesty over silence (a visible, retryable status, never a silent
    ``downloading``/``searching`` left lying), and the SAME never-un-terminate
    guard -- a season already FINISHED (``completed`` / ``available`` / ``failed``)
    is left untouched, so a stale search-exhausted signal can never resurrect a
    finished season as a dedup-blocking ghost.
    """
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    if row.status in _TERMINAL_SEASON_STATUS_VALUES:
        return
    await season_repo.set_status(row.id, RequestStatus.no_acceptable_release.value)
    await _recompute_parent(session, media_request_id)
