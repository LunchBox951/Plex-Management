"""Synchronize one Plex account watchlist into requests and a safe snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from plex_manager.models import MediaType, User, WatchlistItem
from plex_manager.services import request_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.watchlist import WatchlistPort

__all__ = ["WatchlistSyncResult", "is_watchlisted", "list_sync_users", "sync_user"]


@dataclass(frozen=True)
class WatchlistSyncResult:
    fetched: int
    created: int
    existing: int


async def list_sync_users(session: AsyncSession) -> list[User]:
    """Return users with reusable Plex account credentials."""
    stmt = select(User).where(User.encrypted_plex_token.is_not(None)).order_by(User.id)
    return list((await session.execute(stmt)).scalars().all())


async def is_watchlisted(session: AsyncSession, tmdb_id: int, media_type: str) -> bool:
    stmt = select(WatchlistItem.user_id).where(
        WatchlistItem.tmdb_id == tmdb_id,
        WatchlistItem.media_type == MediaType(media_type),
    )
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def sync_user(
    session: AsyncSession,
    watchlist: WatchlistPort,
    tmdb: MetadataPort,
    *,
    user_id: int,
    library: LibraryPort | None = None,
) -> WatchlistSyncResult:
    """Replace one complete snapshot, then idempotently request every title."""
    entries = await watchlist.list_entries()
    await session.execute(delete(WatchlistItem).where(WatchlistItem.user_id == user_id))
    session.add_all(
        WatchlistItem(
            user_id=user_id,
            tmdb_id=entry.tmdb_id,
            media_type=MediaType(entry.media_type),
        )
        for entry in entries
    )
    await session.commit()

    created = 0
    existing = 0
    for entry in entries:
        result = await request_service.create_request_result(
            session,
            tmdb,
            tmdb_id=entry.tmdb_id,
            media_type=entry.media_type,
            user_id=user_id,
            actor_is_admin=False,
            library=library,
        )
        created += int(result.created)
        existing += int(not result.created)
    return WatchlistSyncResult(fetched=len(entries), created=created, existing=existing)
