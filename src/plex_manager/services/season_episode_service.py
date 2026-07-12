"""Episode-level fallback orchestration for whole-season TV requests (ADR-0020).

Bridges TMDB (via :class:`~plex_manager.ports.metadata.MetadataPort`), the
``season_episode_states`` repository, and the pure
:mod:`plex_manager.domain.season_completeness` arithmetic that the Pass-2
episode-level fallback (issue #178, issue #167's hard gate stays Pass 1) needs:
what episodes have aired, what is still missing, and whether a season is truly
complete. Flush-only (module convention): every caller owns its own commit
boundary, exactly like :mod:`plex_manager.services.season_request_service`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError
from plex_manager.domain.season_completeness import (
    aired_target,
    season_is_complete,
)
from plex_manager.domain.season_completeness import (
    compute_missing as _compute_missing,
)
from plex_manager.logsafe import safe_int
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import season_request_service

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.repositories import DownloadRepository, RequestRecord

__all__ = [
    "apply_import",
    "compute_missing",
    "reconcile_airing",
    "refresh_target",
]

_logger = logging.getLogger(__name__)

# The two TERMINAL "real-done" season statuses :func:`reconcile_airing` may
# re-arm out of when an airing show's target grows past what is imported.
# Mirrors ``season_request_service._REAL_DONE_SEASON_STATUS_VALUES`` (duplicated,
# not imported: that module is the season lifecycle owner and importing it back
# would invert the natural dependency direction for no benefit here).
_REARMABLE_DONE_STATUSES: Final[frozenset[str]] = frozenset({"available", "completed"})


async def refresh_target(
    session: AsyncSession,
    metadata: MetadataPort,
    *,
    media_request_id: int,
    season_number: int,
    tmdb_id: int,
    today: date,
) -> frozenset[int]:
    """Refresh the aired-episode target from TMDB and seed/upsert tracked states.

    Lets ``TmdbApiError``/``TmdbAuthError`` propagate -- honesty over silence: a
    TMDB outage means "target unknown this cycle", never a guessed empty target.
    The caller decides how to handle the raise (auto-grab's Pass-2 hook treats it
    as "skip the fallback this cycle, fall through to the normal park").
    """
    season_repo = SqlSeasonRequestRepository(session)
    season = await season_repo.ensure(media_request_id, season_number, status="pending")

    episodes = await metadata.season_episodes(tmdb_id, season_number)
    aired = aired_target({e.episode_number: e.air_date for e in episodes}, today)

    episode_repo = SqlSeasonEpisodeStateRepository(session)
    aired_dates = {e.episode_number: e.air_date for e in episodes if e.episode_number in aired}
    await episode_repo.upsert_target(season.id, aired_dates)
    return aired


async def compute_missing(
    session: AsyncSession,
    download_repo: DownloadRepository,
    *,
    media_request_id: int,
    season_number: int,
    season_request_id: int,
    target: frozenset[int],
) -> frozenset[int]:
    """Aired episodes still needed: ``target`` minus imported minus in-flight.

    The "in-flight" (downloading) set is computed defensively from the season's
    current active download, though at the Pass-2 call site it is structurally
    empty: the auto-grab pre-search ``find_active_for_request`` check already
    skips a scope with an active download before it ever reaches this fallback.
    An active PACK download (``episodes is None``) excludes the WHOLE target --
    never fall back to a single-episode grab while a pack is still downloading.
    """
    episode_repo = SqlSeasonEpisodeStateRepository(session)
    states = await episode_repo.list_for_season(season_request_id)
    imported = {state.episode_number for state in states if state.status == "imported"}

    active = await download_repo.find_active_for_request(media_request_id, season=season_number)
    downloading: set[int]
    if active is None:
        downloading = set()
    elif active.episodes is None:
        downloading = set(target)
    else:
        downloading = set(active.episodes)

    return _compute_missing(target, imported, downloading)


async def apply_import(
    session: AsyncSession,
    *,
    media_request_id: int,
    season_number: int,
    imported_episodes: Iterable[int],
    download_id: int,
    target: frozenset[int],
) -> bool:
    """Record newly-imported episodes; return whether the season is now complete.

    ``complete`` is ``True`` when ``target`` is empty (target unknown -- the
    legacy whole-season-pack behavior: a plain pack import completes the season
    exactly as before this feature existed) OR every target episode is imported.
    The caller (``import_service._import_tv_locked``) uses this to choose between
    ``mark_completed`` and re-arming the season to keep collecting.
    """
    season_repo = SqlSeasonRequestRepository(session)
    season = await season_repo.ensure(media_request_id, season_number, status="pending")

    episode_repo = SqlSeasonEpisodeStateRepository(session)
    await episode_repo.mark_imported(season.id, sorted(imported_episodes), download_id)

    if not target:
        return True

    states = await episode_repo.list_for_season(season.id)
    imported_now = {state.episode_number for state in states if state.status == "imported"}
    return season_is_complete(target, imported_now)


async def reconcile_airing(
    session: AsyncSession,
    metadata: MetadataPort,
    *,
    today: date,
    max_refresh: int,
) -> int:
    """Re-arm a bounded number of ``available``/``completed`` seasons whose aired
    target has grown past what is imported (a new episode aired) back to
    ``searching`` so auto-grab collects the newcomer (ADR-0020 §6).

    Bounded by ``max_refresh`` to protect the single TMDB budget from a large
    install. Best-effort per season: a TMDB error for one season is logged and
    skipped (the SAME "target unknown this cycle" posture as :func:`refresh_target`
    callers), never aborting the whole pass -- mirrors
    ``season_request_service._present_seasons``'s best-effort posture. Returns the
    count of seasons actually re-armed.

    The candidate window ROTATES (P2 fix): :meth:`SeasonRequestRepository.
    list_for_airing_refresh` returns the ``max_refresh`` LEAST-recently-checked
    rows (never-checked first), and every candidate this call actually looks at --
    rearmed, unchanged, OR a TMDB failure -- is stamped via :meth:`SeasonRequest
    Repository.mark_airing_refresh_checked` so it moves to the back of the queue.
    Without this an install with more than ``max_refresh`` airing/completed
    seasons would only ever re-check the same id-lowest slice, permanently
    starving every other season's re-arm.
    """
    season_repo = SqlSeasonRequestRepository(session)
    episode_repo = SqlSeasonEpisodeStateRepository(session)
    request_repo = SqlRequestRepository(session)

    candidates = await season_repo.list_for_airing_refresh(
        _REARMABLE_DONE_STATUSES, limit=max_refresh
    )

    rearmed = 0
    parents: dict[int, RequestRecord | None] = {}
    for season in candidates:
        # (P1 fix, issue #178 review) Episode-scoped requests are TERMINAL for the
        # episodes they named -- the airing refresh must never widen them to the
        # whole aired season. Doing so would seed the whole season, see only the
        # requested episode imported, and re-arm the season to ``searching``, so
        # auto-grab would re-search the already-satisfied episode (or park the done
        # request as no-acceptable) -- a duplicate grab / regression of a request
        # that should stay done. Whole-season / whole-show seasons ONLY. This is
        # the SAME per-season gate auto-grab's Pass-2 fallback uses (``not
        # scope_episodes``): a season is episode-scoped iff its parent named
        # specific episodes FOR THAT SEASON. ``tv_request_mode`` alone is NOT a
        # correct discriminator -- an ``explicit_episodes`` request can still hold
        # whole-season siblings (``request_service._merge_tv_request_intent``).
        if season.media_request_id not in parents:
            parents[season.media_request_id] = await request_repo.get(season.media_request_id)
        parent = parents[season.media_request_id]
        scoped_episodes = (
            parent.requested_episodes.get(season.season_number)
            if parent is not None and parent.requested_episodes
            else None
        )
        if scoped_episodes:
            # Leave the terminal episode-scoped season entirely alone; just rotate
            # it out of the bounded candidate window so it never starves a
            # whole-season sibling that legitimately needs re-arming.
            await season_repo.mark_airing_refresh_checked(season.id, today)
            continue

        # (P1 fix, issue #178 review) Capture whether a real per-episode baseline
        # exists BEFORE ``refresh_target`` seeds pending rows -- it always inserts,
        # so this must be read first.
        had_baseline = bool(await episode_repo.list_for_season(season.id))

        try:
            target = await refresh_target(
                session,
                metadata,
                media_request_id=season.media_request_id,
                season_number=season.season_number,
                tmdb_id=season.tmdb_id,
                today=today,
            )
        except (TmdbApiError, TmdbAuthError) as exc:
            _logger.warning(
                "auto-grab: airing-refresh target lookup failed (%s); leaving season as-is",
                type(exc).__name__,
                extra={
                    "request_id": safe_int(season.media_request_id),
                    "tmdb_id": safe_int(season.tmdb_id),
                },
            )
            # Stamped even on failure: a persistently-erroring show must not
            # monopolise the bounded rotation window forever (it will simply be
            # re-tried once the rotation comes back around to it).
            await season_repo.mark_airing_refresh_checked(season.id, today)
            continue

        if not had_baseline and target:
            # Baseline adoption. INVARIANT: an already-watchable
            # (``available``/``completed``) season with NO episode-state rows
            # became watchable BEFORE per-episode tracking existed -- Plex already
            # owned it, or a whole-season-pack import the migration deliberately
            # seeded nothing for (0004ea..._add_season_episode_states_table). Its
            # content is fully OWNED, so its aired target must count as already
            # imported and must NEVER be re-downloaded. Adopt the just-seeded
            # target as the imported baseline (no backing download) rather than
            # re-arming. Future airing GROWTH still works: a NEWLY-aired episode
            # added to the target on a later cycle will NOT be in this baseline, so
            # ``target > imported`` re-arms the season then -- exactly as a normally
            # tracked season does.
            await episode_repo.adopt_baseline(season.id)
            await season_repo.mark_airing_refresh_checked(season.id, today)
            continue

        states = await episode_repo.list_for_season(season.id)
        imported = {state.episode_number for state in states if state.status == "imported"}
        if target <= imported:
            await season_repo.mark_airing_refresh_checked(season.id, today)
            continue  # nothing new aired -- still fully covered

        changed = await season_request_service.set_status_if_in(
            session,
            media_request_id=season.media_request_id,
            season_request_id=season.id,
            status="searching",
            allowed_from=_REARMABLE_DONE_STATUSES,
        )
        if changed:
            await season_repo.schedule_search(season.id, search_attempts=0, next_search_at=None)
            rearmed += 1
        await season_repo.mark_airing_refresh_checked(season.id, today)

    return rearmed
