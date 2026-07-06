"""Queue endpoints — reconciled download list, grab, and mark-failed. AUTHENTICATED."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.release import ScoredRelease
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.filesystem import FileSystemPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.parser import ParserPort
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.services import (
    grab_service,
    import_service,
    queue_service,
    request_service,
    season_request_service,
)
from plex_manager.services.grab_service import (
    AlreadyDownloadingError,
    DownloadScopeConflictError,
    GrabError,
    NoGrabSourceError,
    RequestNotActiveError,
    SeasonRequiredError,
    TorrentAlreadyTrackedError,
)
from plex_manager.services.queue_service import InvalidStateTransitionError
from plex_manager.web.deps import (
    ServiceNotConfiguredError,
    get_anime_movie_root_optional,
    get_anime_tv_root_optional,
    get_filesystem,
    get_library,
    get_movies_root_optional,
    get_parser,
    get_prowlarr,
    get_qbittorrent,
    get_qbittorrent_optional,
    get_quality_profile,
    get_session,
    get_tv_root_optional,
    require_admin,
)
from plex_manager.web.routers.search_preview import run_preview
from plex_manager.web.schemas import (
    ErrorDetail,
    GrabRequest,
    QueueItem,
    QueueResponse,
    SearchPreviewRequest,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/queue",
    tags=["queue"],
    dependencies=[Depends(require_admin)],
)

_QUEUE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail, "description": "Referenced queue resource not found"},
    409: {"model": ErrorDetail, "description": "Queue action conflict"},
}

# The grab endpoint additionally 422s with an ErrorDetail code (a missing
# descriptor from run_preview, or a season/media-type mismatch) on top of
# FastAPI's own body-validation 422 -- document both shapes, mirroring
# search_preview's anyOf.
_GRAB_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_QUEUE_ERROR_RESPONSES,
    422: {
        "description": "Validation error, missing request descriptor, or season mismatch",
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                        {"$ref": "#/components/schemas/ErrorDetail"},
                    ]
                }
            }
        },
    },
}


def _to_item(record: DownloadRecord) -> QueueItem:
    return QueueItem(
        id=record.id,
        torrent_hash=record.torrent_hash,
        status=record.status,
        progress=record.progress,
        seed_ratio=record.seed_ratio,
        media_request_id=record.media_request_id,
        tmdb_id=record.tmdb_id,
        season=record.season,
        episodes=record.episodes,
        failed_reason=record.failed_reason,
    )


def _select_release(
    accepted: list[ScoredRelease],
    grab: GrabRequest,
) -> ScoredRelease:
    """Pick the operator's chosen release, or the top-ranked one if none given."""
    if grab.info_hash is None and grab.guid is None:
        return accepted[0]  # grab top
    wanted_hash = grab.info_hash.lower() if grab.info_hash else None
    for scored in accepted:
        candidate = scored.candidate
        if wanted_hash is not None and (candidate.info_hash or "").lower() == wanted_hash:
            return scored
        if grab.guid is not None and candidate.guid == grab.guid:
            return scored
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="release_not_found")


@router.get("")
async def get_queue(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> QueueResponse:
    """Return the live download queue (read-only).

    Passive by design: the background reconcile loop is the single owner of
    cross-system truth, so a queue poll never reconciles — doing so could race the
    loop's importer CAS and clobber an ``importing`` claim. The loop refreshes
    frequently, so the persisted progress/status is fresh enough to display, and the
    queue stays viewable even while qBittorrent is unreachable.
    """
    records = await queue_service.list_queue(session)
    return QueueResponse(queue=[_to_item(r) for r in records])


@router.post("/grab", status_code=status.HTTP_201_CREATED, responses=_GRAB_ERROR_RESPONSES)
async def grab_endpoint(
    body: GrabRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
    prowlarr: Annotated[IndexerPort, Depends(get_prowlarr)],
    parser: Annotated[ParserPort, Depends(get_parser)],
    profile: Annotated[QualityProfile, Depends(get_quality_profile)],
) -> QueueItem:
    """Grab a release for a request: a chosen one, or the top accepted pick."""
    request = await request_service.get_request(session, body.request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")

    if request.status in request_service.TERMINAL_REQUEST_STATUS_VALUES:
        # A stale TERMINAL request id (completed / available / failed) is not
        # grabbable: a newer ACTIVE request for the same media owns the
        # uq_media_requests_active slot. Reject up front — BEFORE run_preview can
        # reach the empty-preview branch and flip this finished request to the
        # non-terminal dead-end no_acceptable_release (which would resurrect it as
        # a dedup-blocking ghost), and before grab can hand anything to the client.
        # Mirrors grab_service's RequestNotActiveError guard so both paths agree.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request_not_active")

    # Branch on the request's ACTUAL media type, never on whether ``body.season``
    # happens to be set -- a grab is always per-season for tv and never scoped
    # for a movie, so the two invalid combinations are rejected up front, BEFORE
    # run_preview runs an (unscoped, for tv) search or grab persists a bad row:
    #   - tv with no season: an unscoped preview would run and, if accepted,
    #     grab_service would update the parent MediaRequest directly instead of a
    #     SeasonRequest (breaking the computed-rollup invariant), and the
    #     importer would later block the download as season-less. And on the
    #     empty-preview branch the season-scoped dead-end marker never fires
    #     either, since there is no season to mark.
    #   - movie with a season: would masquerade as a tv grab downstream (a fake
    #     SeasonRequest, a season-scoped active-download guard bypassing the
    #     real one-active-per-movie guard).
    if request.media_type == "tv":
        if body.season is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="tv_grab_requires_season"
            )
    elif body.season is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="movie_grab_rejects_season"
        )

    result = await run_preview(
        # Carry season/episodes so a TV grab searches (and later records) the
        # right scope; both are None for a movie and so leave movie behaviour
        # unchanged.
        SearchPreviewRequest(
            request_id=body.request_id, season=body.season, episodes=body.episodes
        ),
        session,
        prowlarr,
        parser,
        profile,
    )
    if not result.accepted:
        # Honesty over silence: reflect the dead-end on the owning request so it
        # does not linger as 'downloading'/'searching' with nothing in flight.
        # BUT only when nothing is actually in flight: a re-search for a request
        # that ALREADY has an active download must not flip it to a dead-end status
        # while that download is still running. Leave such a request untouched. The
        # active check is season-SCOPED for a TV grab (body.season): another season
        # still downloading must not suppress THIS season's honest dead-end, and a
        # movie (season=None) keeps its whole-request guard unchanged.
        active = await SqlDownloadRepository(session).find_active_for_request(
            request.id, season=body.season
        )
        if active is None:
            # Both services are FLUSH-ONLY -- a genuine compare-and-swap (issue
            # #72), not read-then-write -- so this caller owns the commit boundary
            # for both branches uniformly, and commits ONLY when the CAS actually
            # won: a concurrent writer that already moved the row out of the
            # parkable set (e.g. a racing grab landed it on ``downloading``
            # between the ``find_active_for_request`` re-check above and this
            # write) is left alone rather than silently regressed.
            if body.season is not None:
                # TV: record the dead-end on the SEASON so it is visible + retryable
                # per season, and let the parent MediaRequest.status stay a computed
                # rollup (never a direct write _recompute_parent would clobber).
                parked = await season_request_service.mark_no_acceptable_release(
                    session, media_request_id=request.id, season_number=body.season
                )
            else:
                parked = await request_service.mark_no_acceptable_release(session, request.id)
            if parked:
                await session.commit()
            else:
                await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no_acceptable_release")

    scored = _select_release(result.accepted, body)
    try:
        record = await grab_service.grab(
            qbt,
            session,
            scored=scored,
            request_id=request.id,
            tmdb_id=request.tmdb_id,
            year=request.year,
            season=body.season,
            episodes=body.episodes,
        )
    except NoGrabSourceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no_grab_source") from exc
    except SeasonRequiredError as exc:
        # Defense in depth: the endpoint guard above already rejects this before
        # run_preview even runs, but the service-level invariant holds
        # regardless of caller (never a season=None tv download persisted).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="tv_grab_requires_season"
        ) from exc
    except RequestNotActiveError as exc:
        # A stale terminal request id was grabbed while a newer active request owns
        # the media. Refused before anything was added to the client (no untracked
        # torrent), surfaced honestly so the operator grabs the live request.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="request_not_active"
        ) from exc
    except AlreadyDownloadingError as exc:
        # The request already has an active download for a different release;
        # refuse the parallel grab instead of spawning a second active row.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="already_downloading"
        ) from exc
    except DownloadScopeConflictError as exc:
        # The same physical torrent is already downloading for a DIFFERENT season
        # (a multi-season pack re-grabbed per season). Refused honestly rather than
        # returned as a no-op that would leave this season untracked.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="download_scope_conflict"
        ) from exc
    except TorrentAlreadyTrackedError as exc:
        # The same torrent hash is already actively owned by a different request.
        # Returning that row would claim this request was grabbed while leaving it
        # untouched, so surface a conflict.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="torrent_already_tracked"
        ) from exc
    except GrabError as exc:
        # qBittorrent took the grab but no real info-hash could be determined;
        # surfaced (not silently tracked by an unmatchable guid) so the operator
        # can retry a different release rather than watch a phantom false-fail.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="grab_hash_unresolved"
        ) from exc
    return _to_item(record)


@router.post("/{download_id}/import", responses=_QUEUE_ERROR_RESPONSES)
async def import_endpoint(
    download_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
    library: Annotated[LibraryPort, Depends(get_library)],
    fs: Annotated[FileSystemPort, Depends(get_filesystem)],
    parser: Annotated[ParserPort, Depends(get_parser)],
    profile: Annotated[QualityProfile, Depends(get_quality_profile)],
    movies_root: Annotated[str | None, Depends(get_movies_root_optional)],
    tv_root: Annotated[str | None, Depends(get_tv_root_optional)],
    anime_movie_root: Annotated[str | None, Depends(get_anime_movie_root_optional)],
    anime_tv_root: Annotated[str | None, Depends(get_anime_tv_root_optional)],
) -> QueueItem:
    """Operator retry: (re)run the import for a download (e.g. an ImportBlocked row).

    Requires Plex + qBittorrent configured (409 ``service_not_configured``
    otherwise); the Movies/TV roots are each OPTIONAL here -- no upfront 409 for
    either. A download whose media type's root is unset gets its own honest,
    retryable ``ImportBlocked`` (surfaced in the returned ``QueueItem``, a normal
    200) instead of the endpoint refusing to even try, so an install with only
    ONE root configured can still retry-import that type. The
    correction-without-a-terminal button for a blocked import.
    """
    record = await import_service.import_download(
        download_id=download_id,
        fs=fs,
        library=library,
        qbt=qbt,
        parser=parser,
        profile=profile,
        session=session,
        movies_root=movies_root,
        tv_root=tv_root,
        anime_movie_root=anime_movie_root,
        anime_tv_root=anime_tv_root,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="download_not_found")
    return _to_item(record)


@router.post("/{download_id}/mark-failed", responses=_QUEUE_ERROR_RESPONSES)
async def mark_failed_endpoint(
    download_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort | None, Depends(get_qbittorrent_optional)],
    blocklist: Annotated[bool, Query()] = False,
    remove_torrent: Annotated[bool, Query()] = True,
) -> QueueItem:
    """Operator move: mark a download failed (optionally blocklisting the release).

    ``remove_torrent`` (default true): also remove the torrent + its data from the
    client, closing the seeding leak (ADR-0014) -- best-effort, so a client hiccup
    never blocks the fail/blocklist/re-arm.

    qBittorrent is resolved OPTIONALLY (``get_qbittorrent_optional``): the DB-only
    fail/blocklist/re-arm path never touches it, so ``remove_torrent=false`` still
    works on an install with qBittorrent unconfigured. When removal IS requested but
    the client is unconfigured, this re-imposes the honest 409 ``service_not_configured``
    up front (before any state change) rather than silently skipping the removal.
    """
    if remove_torrent and qbt is None:
        raise ServiceNotConfiguredError("qbittorrent")
    try:
        record = await queue_service.mark_failed(
            session,
            qbt,
            download_id=download_id,
            blocklist=blocklist,
            remove_torrent=remove_torrent,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="download_not_found"
        ) from exc
    except InvalidStateTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="invalid_state_transition"
        ) from exc
    return _to_item(record)
