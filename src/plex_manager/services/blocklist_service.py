"""Blocklist orchestration — list entries and operator un-blocklist (delete)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from plex_manager.repositories.blocklist import SqlBlocklistRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.repositories import BlocklistRecord

__all__ = ["delete", "list_for_media"]


async def list_for_media(
    session: AsyncSession,
    tmdb_id: int | None = None,
) -> list[BlocklistRecord]:
    """List blocklist entries, optionally scoped to one media item."""
    return await SqlBlocklistRepository(session).list_for_media(tmdb_id)


async def delete(session: AsyncSession, blocklist_id: int) -> None:
    """Remove a blocklist entry (operator un-blocklist). Raises if absent."""
    await SqlBlocklistRepository(session).delete(blocklist_id)
    await session.commit()
