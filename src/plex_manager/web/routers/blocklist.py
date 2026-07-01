"""Blocklist endpoints — list and delete (operator un-blocklist). AUTHENTICATED."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.ports.repositories import BlocklistRecord
from plex_manager.services import blocklist_service
from plex_manager.web.deps import get_session, require_api_key
from plex_manager.web.schemas import BlocklistEntry, BlocklistResponse

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/blocklist",
    tags=["blocklist"],
    dependencies=[Depends(require_api_key)],
)


def _to_entry(record: BlocklistRecord) -> BlocklistEntry:
    return BlocklistEntry(
        id=record.id,
        source_title=record.source_title,
        reason=record.reason,
        tmdb_id=record.tmdb_id,
        torrent_hash=record.torrent_hash,
        indexer=record.indexer,
        protocol=record.protocol,
        media_type=record.media_type,
        added_at=record.added_at,
    )


@router.get("")
async def list_blocklist(
    session: Annotated[AsyncSession, Depends(get_session)],
    tmdb_id: Annotated[int | None, Query()] = None,
    media_type: Annotated[str | None, Query(pattern="^(movie|tv)$")] = None,
) -> BlocklistResponse:
    """List blocklist entries, optionally scoped to one media item."""
    records = await blocklist_service.list_for_media(session, tmdb_id, media_type)
    return BlocklistResponse(entries=[_to_entry(r) for r in records])


@router.delete("/{blocklist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_blocklist(
    blocklist_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    """Remove a blocklist entry (operator un-blocklist)."""
    try:
        await blocklist_service.delete(session, blocklist_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="blocklist_entry_not_found"
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
