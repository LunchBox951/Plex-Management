"""Blocklist orchestration — list entries, operator un-blocklist (delete), and
resolve a release's blocklist identity from the download history."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from plex_manager.models import DownloadHistory
from plex_manager.repositories.blocklist import SqlBlocklistRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.repositories import BlocklistRecord

__all__ = ["delete", "indexer_for", "list_for_media", "source_title_for"]


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


async def source_title_for(session: AsyncSession, torrent_hash: str) -> str | None:
    """Best-effort original release title from the download history (for blocklist).

    Shared by every blocklist-writing path (``queue_service``'s reconcile/mark-failed
    and ``correction_service``'s report-issue) so the two-tier blocklist identity is
    resolved ONE way.
    """
    stmt = (
        select(DownloadHistory.source_title)
        .where(DownloadHistory.torrent_hash == torrent_hash)
        .where(DownloadHistory.source_title.is_not(None))
        .order_by(DownloadHistory.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def indexer_for(session: AsyncSession, torrent_hash: str) -> str | None:
    """Best-effort originating indexer from the download history (for blocklist).

    Recorded at grab time (``grab_service`` writes ``DownloadHistory.indexer``).
    Without it a blocklist row has ``indexer=None``, so the pure two-tier identity
    check can never fall back to title+indexer for a candidate that exposes no
    info_hash — defeating blocklist-then-research for hashless feeds.
    """
    stmt = (
        select(DownloadHistory.indexer)
        .where(DownloadHistory.torrent_hash == torrent_hash)
        .where(DownloadHistory.indexer.is_not(None))
        .order_by(DownloadHistory.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()
