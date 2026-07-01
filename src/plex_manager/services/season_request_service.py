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

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.domain.season_rollup import rollup_status
from plex_manager.models import RequestStatus
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.repositories import SeasonRequestRecord

__all__ = [
    "ensure_seasons",
    "mark_available",
    "mark_completed",
    "mark_no_acceptable_release",
    "set_status",
]

_logger = logging.getLogger(__name__)

# Season statuses (string values) at which a SEASON is FINISHED. Duplicated here
# rather than imported from ``request_service`` -- that module imports THIS one
# (to run ``ensure_seasons`` from ``create_request``), so importing back would be
# a circular module dependency. Mirrors ``request_service.
# TERMINAL_REQUEST_STATUS_VALUES`` at the season granularity: a finished season
# must never be re-armed to a non-terminal status by a stale
# ``mark_no_acceptable_release``.
_TERMINAL_SEASON_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value for s in (RequestStatus.completed, RequestStatus.available, RequestStatus.failed)
)


async def _recompute_parent(session: AsyncSession, media_request_id: int) -> None:
    """Re-read every tracked season's status, fold via ``rollup_status``, persist.

    Called after EVERY season-status transition, in the SAME transaction as the
    transition itself: the parent ``MediaRequest.status`` is a pure fold of its
    children, never independently authoritative for a TV request. A show with no
    tracked seasons yet (should not happen once ``ensure_seasons`` has run at
    least once) is a no-op rather than a crash -- ``rollup_status`` itself raises
    on an empty sequence, so guard before calling it.
    """
    season_repo = SqlSeasonRequestRepository(session)
    seasons = await season_repo.list_for_request(media_request_id)
    if not seasons:
        return
    status = rollup_status([season.status for season in seasons])
    await SqlRequestRepository(session).set_status(media_request_id, status)


async def _already_available(library: LibraryPort, tmdb_id: int, season_number: int) -> bool:
    """Best-effort per-season Plex availability check; an error is an explicit 'no'.

    Mirrors ``request_service._already_in_library``: a transient Plex outage or the
    still-partial per-episode NotImplementedError must never block tracking a
    season, so the failure is logged and treated as "can't prove it's already
    there" -- an explicit decision, not a swallowed ``False``.

    ``use_cache=False`` for the same reason ``_already_in_library`` uses it: a
    season just REMOVED from Plex must read as absent immediately on a fresh
    ``ensure_seasons`` call, not a stale cached "present" held for the cache TTL.
    """
    try:
        return await library.is_available(tmdb_id, "tv", use_cache=False, season=season_number)
    except (PlexLibraryError, PlexAuthError, NotImplementedError) as exc:
        _logger.warning(
            "plex availability check failed for tmdb %s season %s (%s); proceeding with a request",
            tmdb_id,
            season_number,
            type(exc).__name__,
        )
        return False


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
    (``is_available(tv, season=n)``), the row is created straight to
    ``available`` rather than ``pending`` -- a season already in the library
    skips search/grab, exactly mirroring ``create_request``'s already-in-library
    short-circuit for movies, but PER SEASON. An unconfigured/unreachable Plex (or
    the still-partial per-episode check) is treated as "not proven available", so
    the season is created ``pending`` and search proceeds normally.

    ``SeasonRequestRepository.ensure`` never re-applies ``status`` to an
    already-established season, so calling this on EVERY ``create_request`` call
    for a tv media_type -- including the dedup path, with a possibly-DIFFERENT
    season list -- only ever ADDS newly-named seasons; it never regresses one
    already in flight or finished.

    The parent rollup is recomputed ONCE after every season in ``seasons`` has
    been ensured, not once per season -- cheaper, and avoids persisting
    transient intermediate rollups.
    """
    season_repo = SqlSeasonRequestRepository(session)
    records: list[SeasonRequestRecord] = []
    for season_number in seasons:
        initial_status = RequestStatus.pending.value
        if library is not None and await _already_available(library, tmdb_id, season_number):
            initial_status = RequestStatus.available.value
        records.append(
            await season_repo.ensure(media_request_id, season_number, status=initial_status)
        )
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
    await _recompute_parent(session, media_request_id)


async def mark_available(
    session: AsyncSession, *, media_request_id: int, season_number: int
) -> None:
    """Phase 2 of honest per-season availability: Plex has confirmed the season is indexed."""
    season_repo = SqlSeasonRequestRepository(session)
    row = await season_repo.ensure(
        media_request_id, season_number, status=RequestStatus.pending.value
    )
    await season_repo.mark_available(row.id)
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
