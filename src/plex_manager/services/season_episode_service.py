"""Episode-level fallback orchestration for whole-season TV requests (ADR-0018).

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
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import season_request_service

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.repositories import DownloadRepository

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
    ``searching`` so auto-grab collects the newcomer (ADR-0018 §6).

    Bounded by ``max_refresh`` to protect the single TMDB budget from a large
    install. Best-effort per season: a TMDB error for one season is logged and
    skipped (the SAME "target unknown this cycle" posture as :func:`refresh_target`
    callers), never aborting the whole pass -- mirrors
    ``season_request_service._present_seasons``'s best-effort posture. Returns the
    count of seasons actually re-armed.
    """
    season_repo = SqlSeasonRequestRepository(session)
    episode_repo = SqlSeasonEpisodeStateRepository(session)

    candidates = [
        season
        for status in ("available", "completed")
        for season in await season_repo.list_by_status(status)
    ][:max_refresh]

    rearmed = 0
    for season in candidates:
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
            continue

        states = await episode_repo.list_for_season(season.id)
        imported = {state.episode_number for state in states if state.status == "imported"}
        if target <= imported:
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

    return rearmed
