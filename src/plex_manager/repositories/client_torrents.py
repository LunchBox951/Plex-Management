"""Persistence shape for future client-only correction observations.

PR 1 intentionally provides only the schema-compatible repository boundary; the
inventory/adopt/remove workflow is activated in the later correction PR.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from plex_manager.models import ClientOnlyTorrent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlClientOnlyTorrentRepository"]


class SqlClientOnlyTorrentRepository:
    """Session-owned reader for the client-only observation table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, torrent_hash: str) -> ClientOnlyTorrent | None:
        return await self._session.get(ClientOnlyTorrent, torrent_hash.lower())
