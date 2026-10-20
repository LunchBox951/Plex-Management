"""Follow-style TV request modes (UI overhaul) -- NOT IMPLEMENTED.

Two modes the request sheet offers beyond explicit seasons/episodes:

* ``latest_season`` -- resolve the show's newest regular season and request that
  season only, every episode (unaired ones are grabbed as they air). It does NOT
  move on to a later season.
* ``new_episodes`` -- request nothing that exists today; grab each newly aired
  episode from now on, with no backfill.

Both need a persisted request mode (``tv_request_mode`` values + Alembic
migration) and hooks in the air-date wake / auto-grab passes. Until then the
create endpoint raises ``NotImplementedError`` -> 501 ``not_implemented``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NoReturn

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.metadata import MetadataPort

__all__ = ["TvFollowMode", "create_follow_request"]

# Identical to ``web.schemas.TvRequestModeField`` -- defined here (not imported) so
# the service layer never depends on the web layer.
TvFollowMode = Literal["latest_season", "new_episodes"]


async def create_follow_request(
    session: AsyncSession,
    tmdb: MetadataPort,
    *,
    tmdb_id: int,
    mode: TvFollowMode,
    user_id: int | None,
) -> NoReturn:
    """Create a ``latest_season`` / ``new_episodes`` TV request."""
    if mode == "latest_season":
        raise NotImplementedError("Requesting only the latest season isn't available yet.")
    raise NotImplementedError("Getting new episodes as they air isn't available yet.")
