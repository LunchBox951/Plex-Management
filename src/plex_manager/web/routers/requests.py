"""Media-request endpoints — create (dedup), list, get. AUTHENTICATED."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.metadata import MetadataPort
from plex_manager.ports.parser import ParserPort
from plex_manager.ports.repositories import RequestRecord
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import correction_service, request_service
from plex_manager.services.correction_service import (
    ActiveDuplicateError,
    DownloadClientRequiredError,
    ImportInProgressError,
    MediaRootUnavailableError,
    NotCancellableError,
    NotReportableError,
    ReportSeasonRequiredError,
    SeasonNotFoundError,
)
from plex_manager.services.request_service import (
    MediaNotFoundError,
    MediaTypeDeferredError,
    NoAiredSeasonsError,
)
from plex_manager.web.deps import (
    ServiceNotConfiguredError,
    get_eviction_filesystem,
    get_library,
    get_library_optional,
    get_movies_root_optional,
    get_parser,
    get_prowlarr,
    get_qbittorrent,
    get_qbittorrent_optional,
    get_quality_profile,
    get_session,
    get_tmdb,
    get_tv_root_optional,
    require_api_key,
)
from plex_manager.web.schemas import (
    CreateRequestBody,
    ErrorDetail,
    KeepForeverBody,
    ReportIssueBody,
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

_CREATE_REQUEST_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": RequestResponse, "description": "Existing matching request"},
    404: {"model": ErrorDetail, "description": "Media not found"},
    409: {"model": ErrorDetail, "description": "Media type deferred"},
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
    recorded directly as ``available`` (no needless search/grab). For a tv
    request, ``body.seasons`` (omitted/empty = the whole aired series) is threaded
    to ``request_service.create_request``, which tracks each named season as its
    own ``SeasonRequest`` row -- including on the dedup path, where a repeat POST
    with a NEW season list grows the tracked set rather than being dropped.
    """
    try:
        result = await request_service.create_request_result(
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
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return await _to_response(session, result.record)


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


@router.get(
    "/{request_id}",
    responses={404: {"model": ErrorDetail, "description": "Request not found"}},
)
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


@router.post("/{request_id}/report-issue")
async def report_issue_endpoint(
    request_id: int,
    body: ReportIssueBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
    library: Annotated[LibraryPort, Depends(get_library)],
    prowlarr: Annotated[IndexerPort, Depends(get_prowlarr)],
    parser: Annotated[ParserPort, Depends(get_parser)],
    profile: Annotated[QualityProfile, Depends(get_quality_profile)],
    movies_root: Annotated[str | None, Depends(get_movies_root_optional)],
    tv_root: Annotated[str | None, Depends(get_tv_root_optional)],
) -> RequestResponse:
    """Report a bad imported/available movie or TV season (ADR-0014).

    Blocklists the culprit release, removes its torrent + the library file, and
    synchronously re-searches for a DIFFERENT release (the honest
    ``no_acceptable_release`` park if nothing is acceptable). Requires Plex +
    qBittorrent + Prowlarr configured (their deps 409 ``service_not_configured``
    otherwise). The correction-without-a-terminal button for "this file is bad".
    """
    record = await request_service.get_request(session, request_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    # The purge target's root: an unmounted/empty root is refused inside the service
    # (MediaRootUnavailableError -> 409). Build the root-scoped filesystem the same
    # way the eviction trigger does -- the ONLY FileSystemPort whose delete() guard
    # has real roots to check against (see get_eviction_filesystem).
    root_path = movies_root if record.media_type == "movie" else tv_root
    fs = get_eviction_filesystem(movies_root, tv_root)
    try:
        updated = await correction_service.report_issue(
            session,
            qbt,
            fs,
            library,
            prowlarr,
            parser,
            profile,
            request_id=request_id,
            reason=body.reason,
            season=body.season,
            root_path=root_path,
        )
    except correction_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found"
        ) from exc
    except ReportSeasonRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="report_requires_season"
        ) from exc
    except SeasonNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="season_not_found"
        ) from exc
    except NotReportableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_reportable") from exc
    except ActiveDuplicateError as exc:
        # A newer active request already owns this media's dedup slot -- re-arming the
        # reported (settled) row would collide. Refused before any blocklist/purge, so
        # nothing was touched; the operator acts on the live active request instead.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="active_duplicate"
        ) from exc
    except MediaRootUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="media_root_unavailable"
        ) from exc
    return await _to_response(session, updated)


@router.post("/{request_id}/cancel")
async def cancel_request_endpoint(
    request_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort | None, Depends(get_qbittorrent_optional)],
) -> RequestResponse:
    """Cancel a not-yet-imported request (ADR-0014): drop active torrent(s), settle.

    Removes any active torrent(s) WITH data (best-effort) and flips the request
    (and every tracked season, for tv) to the settled ``cancelled`` status; the
    row is kept for history and nothing is re-grabbed. A request past the
    not-yet-imported stage is refused (409 ``not_cancellable``) -- use report-issue
    to redo an imported title instead.

    qBittorrent is resolved OPTIONALLY (``get_qbittorrent_optional``): a cancel for a
    ``pending``/``searching``/``no_acceptable_release`` request with NO active download
    rows is a pure DB settle that never touches the client, so it still works on an
    install with qBittorrent unconfigured. When there ARE active torrents to remove but
    the client is unconfigured, the service refuses up front (409
    ``service_not_configured``) rather than silently leaking a seeding torrent.
    """
    try:
        updated = await correction_service.cancel_request(session, qbt, request_id=request_id)
    except correction_service.RequestNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found"
        ) from exc
    except NotCancellableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_cancellable") from exc
    except ImportInProgressError as exc:
        # A download is finalizing its import: cancelling now would race the importer
        # and could strand a placed file under a cancelled request. Honest, retryable
        # 409 -- the operator retries once the import lands (report-issue takes over).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="import_in_progress"
        ) from exc
    except DownloadClientRequiredError as exc:
        # Active torrent(s) to remove, but qBittorrent is unconfigured. Surface the same
        # honest 409 ``service_not_configured`` the mark-failed endpoint uses -- refused
        # before any state change, so nothing was settled or removed.
        raise ServiceNotConfiguredError("qbittorrent") from exc
    return await _to_response(session, updated)
