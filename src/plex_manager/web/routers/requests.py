"""Media-request endpoints — create (dedup), list, get. AUTHENTICATED."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.ports.library import LibraryPort
from plex_manager.ports.metadata import MetadataPort
from plex_manager.ports.repositories import RequestRecord
from plex_manager.services import request_service
from plex_manager.services.request_service import MediaNotFoundError, MediaTypeDeferredError
from plex_manager.web.deps import (
    get_library_optional,
    get_session,
    get_tmdb,
    require_api_key,
)
from plex_manager.web.schemas import (
    CreateRequestBody,
    ErrorDetail,
    RequestListResponse,
    RequestResponse,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/requests",
    tags=["requests"],
    dependencies=[Depends(require_api_key)],
)

_CREATE_REQUEST_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": RequestResponse, "description": "Existing matching request"},
    404: {"model": ErrorDetail, "description": "Media not found"},
    409: {"model": ErrorDetail, "description": "Media type deferred"},
}


def _to_response(record: RequestRecord) -> RequestResponse:
    return RequestResponse(
        id=record.id,
        tmdb_id=record.tmdb_id,
        media_type=record.media_type,
        title=record.title,
        status=record.status,
        year=record.year,
        is_anime=record.is_anime,
        poster_url=record.poster_url,
        backdrop_url=record.backdrop_url,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=_CREATE_REQUEST_RESPONSES,
)
async def create_request_endpoint(
    body: CreateRequestBody,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    library: Annotated[LibraryPort | None, Depends(get_library_optional)],
) -> RequestResponse:
    """Create a request (or return the existing active one for this media).

    If Plex is configured and the movie is already in the library, the request is
    recorded directly as ``available`` (no needless search/grab).
    """
    try:
        result = await request_service.create_request_result(
            session,
            tmdb,
            tmdb_id=body.tmdb_id,
            media_type=body.media_type,
            library=library,
        )
    except MediaNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="media_not_found",
        ) from exc
    except MediaTypeDeferredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="media_type_deferred",
        ) from exc
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _to_response(result.record)


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
