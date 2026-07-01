"""Media-request endpoints — create (dedup), list, get. AUTHENTICATED."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.ports.library import LibraryPort
from plex_manager.ports.metadata import MetadataPort
from plex_manager.ports.repositories import RequestRecord
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import request_service
from plex_manager.services.request_service import (
    MediaNotFoundError,
    MediaTypeDeferredError,
    NoAiredSeasonsError,
)
from plex_manager.web.deps import (
    get_library_optional,
    get_session,
    get_tmdb,
    require_api_key,
)
from plex_manager.web.schemas import (
    CreateRequestBody,
    KeepForeverBody,
    RequestListResponse,
    RequestResponse,
    SeasonStatus,
)

if TYPE_CHECKING:
    from plex_manager.ports.repositories import SeasonRequestRecord

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/requests",
    tags=["requests"],
    dependencies=[Depends(require_api_key)],
)

_MEDIA_TYPE_DEFERRED_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"description": "Media type deferred"},
}


async def _to_response(
    session: AsyncSession,
    record: RequestRecord,
    seasons_by_request: dict[int, list[SeasonRequestRecord]] | None = None,
) -> RequestResponse:
    """Map a request record to the wire DTO, embedding its per-season rollup for tv.

    ``seasons_by_request`` is an optional pre-fetched ``{media_request_id:
    [SeasonRequestRecord, ...]}`` map -- see ``list_requests_endpoint``, which
    fetches EVERY tracked show's season rows in ONE batched query up front (via
    ``SeasonRequestRepository.list_for_requests``) rather than calling this
    per-row, which would otherwise issue one ``list_for_request`` query per tv
    request on the list endpoint (an N+1). When omitted (the single-record ``GET
    /requests/{id}`` path, where batching buys nothing), a tv record fetches its
    OWN season rows directly. A movie record's ``seasons`` is always ``None`` --
    movies have no ``SeasonRequest`` rows.
    """
    seasons: list[SeasonRequestRecord] | None = None
    if record.media_type == "tv":
        if seasons_by_request is not None:
            seasons = seasons_by_request.get(record.id, [])
        else:
            seasons = await SqlSeasonRequestRepository(session).list_for_request(record.id)
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
        seasons=(
            [SeasonStatus(season_number=s.season_number, status=s.status) for s in seasons]
            if seasons is not None
            else None
        ),
        keep_forever=record.keep_forever,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    responses=_MEDIA_TYPE_DEFERRED_RESPONSES,
)
async def create_request_endpoint(
    body: CreateRequestBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    tmdb: Annotated[MetadataPort, Depends(get_tmdb)],
    library: Annotated[LibraryPort | None, Depends(get_library_optional)],
) -> RequestResponse:
    """Create a request (or return the existing active one for this media).

    If Plex is configured and the movie is already in the library, the request is
    recorded directly as ``available`` (no needless search/grab). For a tv
    request, ``body.seasons`` (omitted/empty = the whole aired series) is threaded
    to ``request_service.create_request``, which tracks each named season as its
    own ``SeasonRequest`` row -- including on the dedup path, where a repeat POST
    with a NEW season list grows the tracked set rather than being dropped.
    """
    try:
        record = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=body.tmdb_id,
            media_type=body.media_type,
            library=library,
            seasons=body.seasons,
        )
    except MediaNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="media_not_found",
        ) from exc
    except NoAiredSeasonsError as exc:
        # The show exists in TMDB but resolved to zero trackable seasons (a data
        # gap, or a specials-only show) -- an honest 404, never a persisted
        # 'pending' request with nothing to search/grab.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no_aired_seasons",
        ) from exc
    except MediaTypeDeferredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="media_type_deferred",
        ) from exc
    return await _to_response(session, record)


@router.get("")
async def list_requests_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestListResponse:
    """List all media requests."""
    records = await request_service.list_requests(session)
    # Batch every tv row's season rows in ONE query (avoids an N+1 query per tv
    # request that calling ``_to_response`` per-row without this would cause).
    tv_ids = [r.id for r in records if r.media_type == "tv"]
    seasons_by_request = await SqlSeasonRequestRepository(session).list_for_requests(tv_ids)
    return RequestListResponse(
        requests=[await _to_response(session, r, seasons_by_request) for r in records]
    )


@router.get("/{request_id}")
async def get_request_endpoint(
    request_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestResponse:
    """Return a single media request, or 404."""
    record = await request_service.get_request(session, request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    return await _to_response(session, record)


@router.post("/{request_id}/keep-forever")
async def keep_forever_endpoint(
    request_id: int,
    body: KeepForeverBody,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestResponse:
    """Set or clear the operator's "keep forever" pin (ADR-0012).

    The north-star #1 correction path for "don't let the eviction sweep touch
    this one": a pinned title (or, for a show, every one of its seasons -- the
    pin lives on the parent) is never selected by ``domain/eviction.py``
    regardless of watch state or disk pressure. A 404 for an unknown id, never
    a silent no-op.
    """
    record = await request_service.set_keep_forever(
        session, request_id, keep_forever=body.keep_forever
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    return await _to_response(session, record)
