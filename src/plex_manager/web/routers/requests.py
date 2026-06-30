"""Media-request endpoints — create (dedup), list, get. AUTHENTICATED."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.ports.metadata import MetadataPort
from plex_manager.ports.repositories import RequestRecord
from plex_manager.services import request_service
from plex_manager.services.request_service import MediaNotFoundError
from plex_manager.web.deps import get_session, get_tmdb, require_api_key
from plex_manager.web.schemas import (
    CreateRequestBody,
    RequestListResponse,
    RequestResponse,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/requests",
    tags=["requests"],
    dependencies=[Depends(require_api_key)],
)


def _to_response(record: RequestRecord) -> RequestResponse:
    return RequestResponse(
        id=record.id,
        tmdb_id=record.tmdb_id,
        media_type=record.media_type,
        title=record.title,
        status=record.status,
        year=record.year,
        is_anime=record.is_anime,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_request_endpoint(
    body: CreateRequestBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
) -> RequestResponse:
    """Create a request (or return the existing active one for this media)."""
    try:
        record = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=body.tmdb_id,
            media_type=body.media_type,
        )
    except MediaNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="media_not_found",
        ) from exc
    return _to_response(record)


@router.get("")
async def list_requests_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestListResponse:
    """List all media requests."""
    records = await request_service.list_requests(session)
    return RequestListResponse(requests=[_to_response(r) for r in records])


@router.get("/{request_id}")
async def get_request_endpoint(
    request_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestResponse:
    """Return a single media request, or 404."""
    record = await request_service.get_request(session, request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    return _to_response(record)
