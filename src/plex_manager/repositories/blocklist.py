"""``BlocklistRepository`` implementation over an :class:`AsyncSession`.

The two-tier "same release" identity test is NOT reimplemented here: this
adapter pre-scopes the candidate entries by ``tmdb_id`` (so a hash/title match
can never collide across media items) and delegates the actual matching to the
pure :func:`plex_manager.domain.blocklist.is_blocklisted`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import or_, select

from plex_manager.domain.blocklist import BlocklistedRelease
from plex_manager.domain.blocklist import is_blocklisted as _domain_is_blocklisted
from plex_manager.models import Blocklist, BlocklistReason, MediaType
from plex_manager.ports.repositories import BlocklistRecord

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlBlocklistRepository"]


def _media_type_scope(media_type: str | None) -> ColumnElement[bool] | None:
    """A WHERE clause scoping the blocklist to one media namespace, or ``None``.

    TMDB movie and TV ids are SEPARATE namespaces, so a movie and a show can share
    the same numeric ``tmdb_id``; scoping only by ``tmdb_id`` lets a TV blocklist
    entry match a movie candidate (and vice versa). Match rows of the requested media
    type. Legacy ``NULL``-media_type rows predate BOTH the column and the TV feature,
    so they are movie-era: fold them into the ``movie`` scope ONLY (keeping old movie
    blocklists working) and NEVER into a ``tv`` scope (that would re-introduce exactly
    the cross-namespace false block for legacy rows). ``None`` (an untyped "search")
    imposes no scope.
    """
    if media_type is None:
        return None
    typed = Blocklist.media_type == MediaType(media_type)
    if media_type == "movie":
        return or_(typed, Blocklist.media_type.is_(None))
    return typed


def _to_record(row: Blocklist) -> BlocklistRecord:
    """Map a ``Blocklist`` ORM row to its frozen read-model DTO."""
    return BlocklistRecord(
        id=row.id,
        source_title=row.source_title,
        reason=row.reason.value,
        tmdb_id=row.tmdb_id,
        torrent_hash=row.torrent_hash,
        indexer=row.indexer,
        protocol=row.protocol,
        media_type=row.media_type.value if row.media_type is not None else None,
        added_at=row.added_at,
    )


class SqlBlocklistRepository:
    """Persist and read blocklist entries via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_blocklisted(
        self,
        tmdb_id: int | None,
        torrent_hash: str | None,
        source_title: str,
        indexer: str | None,
        *,
        media_type: str | None = None,
    ) -> bool:
        # Pre-scope to the same media item (tmdb_id, with NULL matching NULL) AND the
        # same media namespace (see _media_type_scope) so the pure identity check
        # never crosses media boundaries.
        stmt = select(Blocklist).where(Blocklist.tmdb_id == tmdb_id)
        scope = _media_type_scope(media_type)
        if scope is not None:
            stmt = stmt.where(scope)
        rows = (await self._session.execute(stmt)).scalars().all()
        entries = [
            BlocklistedRelease(
                source_title=row.source_title,
                info_hash=row.torrent_hash,
                indexer=row.indexer,
            )
            for row in rows
        ]
        return _domain_is_blocklisted(
            info_hash=torrent_hash,
            source_title=source_title,
            indexer=indexer,
            entries=entries,
        )

    async def list_for_media(
        self, tmdb_id: int | None = None, *, media_type: str | None = None
    ) -> list[BlocklistRecord]:
        stmt = select(Blocklist)
        if tmdb_id is not None:
            stmt = stmt.where(Blocklist.tmdb_id == tmdb_id)
        scope = _media_type_scope(media_type)
        if scope is not None:
            stmt = stmt.where(scope)
        stmt = stmt.order_by(Blocklist.id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def create(
        self,
        *,
        source_title: str,
        reason: str,
        tmdb_id: int | None = None,
        torrent_hash: str | None = None,
        indexer: str | None = None,
        protocol: str | None = None,
        media_type: str | None = None,
    ) -> BlocklistRecord:
        row = Blocklist(
            source_title=source_title,
            reason=BlocklistReason(reason),
            tmdb_id=tmdb_id,
            torrent_hash=torrent_hash,
            indexer=indexer,
            protocol=protocol,
            media_type=MediaType(media_type) if media_type is not None else None,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def delete(self, blocklist_id: int) -> None:
        row = await self._session.get(Blocklist, blocklist_id)
        if row is None:
            raise LookupError(f"blocklist entry {blocklist_id} does not exist")
        await self._session.delete(row)
        await self._session.flush()
