"""Media-request endpoints — create (dedup), list, get. AUTHENTICATED."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
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
from plex_manager.services.library_roots import LibraryRoots
from plex_manager.services.request_service import (
    MediaNotFoundError,
    NoAiredSeasonsError,
    RequestOwnedByAnotherUserError,
)
from plex_manager.web.deps import (
    AuthContext,
    ServiceNotConfiguredError,
    get_anime_movie_root_optional,
    get_anime_tv_root_optional,
    get_downloads_host_root,
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
    require_admin,
    require_api_key,
)
from plex_manager.web.errors import AppError
from plex_manager.web.events import publish_realtime
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
)

# NB: no 409 "media type deferred" here -- ``CreateRequestBody.media_type`` is
# ``Literal["movie", "tv"]``, so any other value is already a 422 at validation;
# the service-level ``MediaTypeDeferredError`` remains only as a defensive guard
# for internal (non-HTTP) callers.
_CREATE_REQUEST_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": RequestResponse, "description": "Existing matching request"},
    404: {"model": ErrorDetail, "description": "Media not found"},
    409: {"model": ErrorDetail, "description": "Already requested by another user"},
}

_REPORT_ISSUE_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail, "description": "Request or season not found"},
    # This status code has TWO distinct producers, so BOTH shapes are documented
    # via anyOf (the same pattern as setup/complete's 422 and PUT /settings): the
    # string-detail ``HTTPException`` 409s (``not_reportable`` /
    # ``active_duplicate`` -- ``ErrorDetail``) and the ``AppError`` 409
    # (``media_root_unavailable`` -- an ``ErrorEnvelope`` whose message/hint/
    # diagnostics carry the actionable broken-root guidance). Declaring only one
    # model would make the generated TS client mis-model the other shape.
    409: {
        "description": (
            "Not reportable in its current state, an active duplicate exists, or the "
            "title's library folder isn't reachable from the app"
        ),
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/ErrorDetail"},
                        {"$ref": "#/components/schemas/ErrorEnvelope"},
                    ]
                }
            }
        },
    },
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
        tv_request_mode=record.tv_request_mode,
        requested_seasons=list(record.requested_seasons) if record.requested_seasons else None,
        requested_episodes=(
            {season: list(values) for season, values in record.requested_episodes.items()}
            if record.requested_episodes
            else None
        ),
        seasons=(
            [
                SeasonStatus(
                    season_number=s.season_number,
                    status=s.status,
                    installed_quality_id=s.installed_quality_id,
                    installed_profile_index=s.installed_profile_index,
                )
                for s in seasons
            ]
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
    http_request: Request,
    auth: Annotated[AuthContext, Depends(require_api_key)],
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

    Re-acquire (issue #131): ``body.force`` (movie-only) bypasses the
    already-in-library short-circuit so a title Plex still reports present -- but
    whose file was deleted/replaced out-of-band -- yields a fresh, grabbable
    ``pending`` request instead of a terminal ``available`` one. Same authZ as any
    create; same response map (no new status codes) -- every dedup/ownership
    guard below still applies unchanged.
    """
    try:
        result = await request_service.create_request_result(
            session,
            tmdb,
            tmdb_id=body.tmdb_id,
            media_type=body.media_type,
            library=library,
            seasons=body.seasons,
            episodes=body.episodes,
            force=bool(body.force),
            user_id=auth.user_id,
            actor_is_admin=auth.is_admin,
        )
    except MediaNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="media_not_found",
        ) from exc
    except RequestOwnedByAnotherUserError as exc:
        # Issue #58: a non-admin cannot dedup onto another user's active request
        # (it would mutate/return a row they can't even see). Honest 409, not a
        # silent no-op or a hidden mutation.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="requested_by_another_user",
        ) from exc
    except NoAiredSeasonsError as exc:
        # The show exists in TMDB but resolved to zero trackable seasons (a data
        # gap, or a specials-only show) -- an honest 404, never a persisted
        # 'pending' request with nothing to search/grab.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no_aired_seasons",
        ) from exc
    if not result.created:
        response.status_code = status.HTTP_200_OK
    response_body = await _to_response(session, result.record)
    publish_realtime(
        http_request.app,
        ("requests", "discover"),
        reason="request_created" if result.created else "request_updated",
    )
    return response_body


@router.get("")
async def list_requests_endpoint(
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestListResponse:
    """List all media requests."""
    records = await request_service.list_requests(session)
    if not auth.is_admin:
        records = [record for record in records if record.user_id == auth.user_id]
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
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> RequestResponse:
    """Return a single media request, or 404."""
    record = await request_service.get_request(session, request_id)
    if record is None or (not auth.is_admin and record.user_id != auth.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")
    return await _to_response(session, record)


@router.post("/{request_id}/keep-forever")
async def keep_forever_endpoint(
    request_id: int,
    body: KeepForeverBody,
    http_request: Request,
    _admin: Annotated[AuthContext, Depends(require_admin)],
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
    response_body = await _to_response(session, record)
    publish_realtime(
        http_request.app,
        ("requests", "discover", "ops:disk"),
        reason="request_updated",
    )
    return response_body


@router.post("/{request_id}/report-issue", responses=_REPORT_ISSUE_RESPONSES)
async def report_issue_endpoint(
    request_id: int,
    body: ReportIssueBody,
    http_request: Request,
    _admin: Annotated[AuthContext, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
    library: Annotated[LibraryPort, Depends(get_library)],
    prowlarr: Annotated[IndexerPort, Depends(get_prowlarr)],
    parser: Annotated[ParserPort, Depends(get_parser)],
    profile: Annotated[QualityProfile, Depends(get_quality_profile)],
    movies_root: Annotated[str | None, Depends(get_movies_root_optional)],
    tv_root: Annotated[str | None, Depends(get_tv_root_optional)],
    anime_movie_root: Annotated[str | None, Depends(get_anime_movie_root_optional)],
    anime_tv_root: Annotated[str | None, Depends(get_anime_tv_root_optional)],
    downloads_host_root: Annotated[str, Depends(get_downloads_host_root)],
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
    #
    # ADR-0015 fix: hand the service ALL configured roots (as a typed bundle) and let
    # it derive which one to mount-check FROM the stored library_path breadcrumb --
    # the DEEPEST containing root, so a nested anime root is verified itself, never a
    # mounted parent -- rather than picking a single root here from ``is_anime`` +
    # the currently-configured anime root. That earlier is_anime-based pick was wrong
    # whenever the breadcrumb lived under a DIFFERENT root than the current config
    # implies -- e.g. anime imported BEFORE an anime root was configured, whose file
    # is under movies_root/tv_root: it would 409 against an empty newly-configured
    # anime root, or (worse) wave the check through against a mounted anime root
    # while the real root was unmounted and strand the old file. A row with NO
    # breadcrumb falls back to the media-type-appropriate root inside the service
    # (``LibraryRoots.fallback_for``). The delete-guard's fs is built from the SAME
    # root set, so a breadcrumb under whichever root holds it is deletable.
    roots = LibraryRoots(
        movies=movies_root,
        tv=tv_root,
        anime_movie=anime_movie_root,
        anime_tv=anime_tv_root,
    )
    fs = get_eviction_filesystem(movies_root, tv_root, anime_movie_root, anime_tv_root)
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
            roots=roots,
            save_path=downloads_host_root,
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
        raise AppError(
            status_code=status.HTTP_409_CONFLICT,
            code="media_root_unavailable",
            message="The library folder for this title isn't reachable, so it can't be "
            "re-requested safely.",
            hint="Check the folder is mounted and visible to Plex Manager "
            "(Settings → Library), then try again.",
            diagnostics=({"root": exc.root_path} if exc.root_path else None),
        ) from exc
    response_body = await _to_response(session, updated)
    publish_realtime(
        http_request.app,
        ("requests", "queue", "blocklist", "discover"),
        reason="report_issue",
    )
    return response_body


@router.post("/{request_id}/cancel")
async def cancel_request_endpoint(
    request_id: int,
    http_request: Request,
    _admin: Annotated[AuthContext, Depends(require_admin)],
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
    response_body = await _to_response(session, updated)
    publish_realtime(
        http_request.app,
        ("requests", "queue", "discover"),
        reason="cancel_request",
    )
    return response_body
