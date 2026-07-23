"""Queue endpoints — reconciled download list, grab, and mark-failed. AUTHENTICATED."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.release import ScoredRelease
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import DownloadScopeStatus, RequestStatus
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.filesystem import FileSystemPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.media_probe import MediaProbePort
from plex_manager.ports.parser import ParserPort
from plex_manager.ports.repositories import DownloadRecord, QueueRecord
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import (
    correction_service,
    grab_service,
    import_service,
    queue_service,
    request_service,
    season_request_service,
)
from plex_manager.services.grab_service import (
    AlreadyDownloadingError,
    GrabError,
    NoGrabSourceError,
    RequestNotActiveError,
    SeasonRequiredError,
    TorrentAlreadyTrackedError,
    TorrentRemovalInFlightError,
)
from plex_manager.services.queue_service import InvalidStateTransitionError, RemovalInProgressError
from plex_manager.web.deps import (
    ServiceNotConfiguredError,
    get_anime_movie_root_optional,
    get_anime_tv_root_optional,
    get_downloads_host_root,
    get_filesystem,
    get_library,
    get_media_probe,
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
from plex_manager.web.events import publish_realtime
from plex_manager.web.routers.search_preview import run_preview, stored_episodes_for_request
from plex_manager.web.schemas import (
    ErrorDetail,
    GrabRequest,
    QueueItem,
    QueueResponse,
    QueueScope,
    SearchPreviewRequest,
    ServiceNotConfiguredErrorDetail,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/queue",
    tags=["queue"],
    dependencies=[Depends(require_admin)],
)

# Shared 404/409 map for every queue mutation endpoint below (grab -- via
# ``_GRAB_ERROR_RESPONSES``'s spread --, import, mark-failed, relocate). The 409
# entry's ``model`` is a union (issue #291): on top of each endpoint's own plain
# ``HTTPException`` conflicts (``ErrorDetail``), grab/import/relocate resolve
# qBittorrent (and grab additionally Prowlarr) via NON-optional deps, and
# mark-failed raises ``ServiceNotConfiguredError`` directly when
# ``remove_torrent=true`` and qBittorrent is unconfigured -- all rendered by the
# app-wide handler as the ``service``-carrying ``ServiceNotConfiguredErrorDetail``
# shape, not the bare ``ErrorDetail`` one. FastAPI expands a ``X | Y`` "model"
# into an anyOf AND registers both members' component schemas, so this documents
# ``service`` on the generated client type for every endpoint that shares this
# map, letting a caller route the operator straight to that service's setup step.
_QUEUE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorDetail, "description": "Referenced queue resource not found"},
    409: {
        "model": ErrorDetail | ServiceNotConfiguredErrorDetail,
        "description": "Queue action conflict, or a required service is not configured",
    },
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
    """Map a download record to the wire ``QueueItem``.

    Accepts either a plain ``DownloadRecord`` (the grab/import/mark-failed
    mutation endpoints, which never join ``MediaRequest``) or the queue-list's
    enriched ``QueueRecord`` (a ``DownloadRecord`` subtype) -- ``title`` /
    ``poster_url`` are only ever populated for the latter; a plain
    ``DownloadRecord`` renders those two ``None`` (honest degrade, not a lie),
    while ``release_title`` -- persisted on the download row itself -- is
    always available regardless of which record type this is.
    """
    return QueueItem(
        id=record.id,
        torrent_hash=record.torrent_hash,
        status=cast(DownloadState, record.status),
        progress=record.progress,
        seed_ratio=record.seed_ratio,
        media_request_id=record.media_request_id,
        tmdb_id=record.tmdb_id,
        season=record.season,
        episodes=record.episodes,
        failed_reason=record.failed_reason,
        title=record.title if isinstance(record, QueueRecord) else None,
        poster_url=record.poster_url if isinstance(record, QueueRecord) else None,
        release_title=record.release_title,
        scopes=[
            QueueScope(
                media_request_id=scope.media_request_id,
                season=scope.season,
                episodes=scope.episodes,
                status=cast(DownloadScopeStatus, scope.status),
            )
            for scope in record.scopes
        ],
    )


def _select_release(
    accepted: Sequence[ScoredRelease],
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
    http_request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
    prowlarr: Annotated[IndexerPort, Depends(get_prowlarr)],
    parser: Annotated[ParserPort, Depends(get_parser)],
    profile: Annotated[QualityProfile, Depends(get_quality_profile)],
    downloads_host_root: Annotated[str, Depends(get_downloads_host_root)],
) -> QueueItem:
    """Grab a release for a request: a chosen one, or the top accepted pick."""
    request = await request_service.get_request(session, body.request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request_not_found")

    if (
        request.media_type != "tv"
        and request.status in request_service.TERMINAL_REQUEST_STATUS_VALUES
    ):
        # A stale TERMINAL request id (completed / available / failed) is not
        # grabbable: a newer ACTIVE request for the same media owns the
        # uq_media_requests_active slot. Reject up front — BEFORE run_preview can
        # reach the empty-preview branch and flip this finished request to the
        # non-terminal dead-end no_acceptable_release (which would resurrect it as
        # a dedup-blocking ghost), and before grab can hand anything to the client.
        # Mirrors grab_service's RequestNotActiveError guard so both paths agree.
        #
        # MOVIE-ONLY: for a TV request, ``request.status`` is not this request's
        # own state -- it is ``season_rollup.rollup_status``'s COMPUTED fold over
        # every tracked season (see grab_service.grab's matching guard for the
        # full rationale). A still-finalizing season (``completed``, issue #265)
        # wins that fold outright over a genuinely due sibling, and ``completed``
        # is ALSO in ``TERMINAL_REQUEST_STATUS_VALUES`` -- gating here on the
        # coarse rollup would 409 a legitimate per-season grab (issue #272
        # review). grab_service's own season-scoped guard (cancelled /
        # waiting_for_air_date / a stale caller premise) is season-precise where
        # this coarse check never was, and still runs for every tv grab below.
        # But skipping the terminal gate ENTIRELY for tv (issue #287) let a
        # wholly-terminal TV row slip into the empty-preview parking path below;
        # the dedicated, season-scoped TV terminal gate just after the media-type
        # validation restores that guarantee here too.
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

    # TV terminal gate (issue #287): the up-front guard above is MOVIE-ONLY on
    # purpose -- a TV ``request.status`` is season_rollup's COMPUTED fold, not a
    # real state, so gating the coarse rollup unconditionally would 409 a season
    # that is genuinely due behind a still-finalizing ``completed`` sibling (issue
    # #272). But skipping the terminal check ENTIRELY for tv let a wholly-terminal
    # TV row (e.g. an old available/evicted request for an untracked season) slip
    # into the empty-preview parking path below, where
    # ``season_request_service.mark_no_acceptable_release`` ``ensure``s the
    # untracked season as ``pending`` and recomputes the parent rollup to a
    # non-terminal dead-end -- resurrecting a settled TV request as a
    # dedup-blocking ghost, the very guarantee restored at grab's own qbt.add
    # gate. Apply that SAME season-scoped gate here, BEFORE run_preview can reach
    # the parking path: read this season's decision-time status (an untracked
    # season reads ``pending``, exactly what ``mark_no_acceptable_release`` would
    # ``ensure`` it as) and refuse unless it is genuinely due.
    if request.media_type == "tv" and body.season is not None:
        season_rows = await SqlSeasonRequestRepository(session).list_for_request(request.id)
        observed_season_status = next(
            (row.status for row in season_rows if row.season_number == body.season),
            RequestStatus.pending.value,
        )
        # issue #295: check cancelled/waiting_for_air_date BEFORE the coarse
        # terminal-parent gate below, exactly the order grab_service.grab uses
        # internally. Both are refused unconditionally regardless of the
        # parent rollup — a cancelled/unaired season is never due — but
        # `tv_grab_blocked_by_terminal_parent` only inspects `parent_status`
        # first and returns False outright for a NON-terminal parent (e.g. a
        # pending sibling elsewhere in the show), so checking it alone here
        # let a cancelled/unaired season under a non-terminal parent fall
        # through to `run_preview` below — a wasted indexer search for a
        # season the operator explicitly stopped or that hasn't aired yet —
        # before the deeper grab_service gate finally 409s. Rejecting these two
        # statuses here, up front, closes that gap the same way grab_service
        # already does.
        if observed_season_status == RequestStatus.cancelled.value:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request_not_active")
        if observed_season_status == RequestStatus.waiting_for_air_date.value:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request_not_active")
        if grab_service.tv_grab_blocked_by_terminal_parent(
            parent_status=request.status,
            observed_season_status=observed_season_status,
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="request_not_active")

    effective_episodes = stored_episodes_for_request(
        request,
        season=body.season,
        episodes=body.episodes,
        episodes_was_provided="episodes" in body.model_fields_set,
    )
    scope_episodes_by_season = (
        {season: list(values) for season, values in request.requested_episodes.items()}
        if request.media_type == "tv" and request.requested_episodes
        else None
    )

    result = await run_preview(
        # Carry season/episodes so a TV grab searches (and later records) the
        # right scope; both are None for a movie and so leave movie behaviour
        # unchanged.
        SearchPreviewRequest(
            request_id=body.request_id, season=body.season, episodes=effective_episodes
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
        active = await SqlDownloadRepository(session).find_active_for_request_or_coverage(
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
                publish_realtime(
                    http_request.app,
                    ("requests", "discover"),
                    reason="no_acceptable_release",
                )
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
            episodes=effective_episodes,
            scope_episodes_by_season=scope_episodes_by_season,
            save_path=downloads_host_root,
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
    except TorrentAlreadyTrackedError as exc:
        # The same torrent hash is already actively owned by a different request.
        # Returning that row would claim this request was grabbed while leaving it
        # untouched, so surface a conflict.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="torrent_already_tracked"
        ) from exc
    except TorrentRemovalInFlightError as exc:
        # #206: the same torrent's removal is mid-flight (a concurrent cancel), so
        # reusing its terminal row would re-own data about to be deleted. Honest 409;
        # the operator/auto-grab retries once the removal settles.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="removal_in_progress"
        ) from exc
    except GrabError as exc:
        # qBittorrent took the grab but no real info-hash could be determined;
        # surfaced (not silently tracked by an unmatchable guid) so the operator
        # can retry a different release rather than watch a phantom false-fail.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="grab_hash_unresolved"
        ) from exc
    item = _to_item(record)
    publish_realtime(
        http_request.app,
        ("queue", "requests", "discover"),
        reason="grab",
    )
    return item


@router.post("/{download_id}/import", responses=_QUEUE_ERROR_RESPONSES)
async def import_endpoint(
    download_id: int,
    http_request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
    library: Annotated[LibraryPort, Depends(get_library)],
    fs: Annotated[FileSystemPort, Depends(get_filesystem)],
    media_probe: Annotated[MediaProbePort, Depends(get_media_probe)],
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
        media_probe=media_probe,
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
    item = _to_item(record)
    publish_realtime(
        http_request.app,
        ("queue", "requests", "discover"),
        reason="import",
    )
    return item


@router.post("/{download_id}/mark-failed", responses=_QUEUE_ERROR_RESPONSES)
async def mark_failed_endpoint(
    download_id: int,
    http_request: Request,
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
    except RemovalInProgressError as exc:
        # Ownership protocol step 5 (queue_service module docstring): another
        # mark-failed's torrent removal is mid-flight for this download, so its
        # remove decision is already irreversible -- superseding it would lie.
        # Honest 409; the operator retries once the in-flight call resolves.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="removal_in_progress"
        ) from exc
    item = _to_item(record)
    publish_realtime(
        http_request.app,
        ("queue", "requests", "blocklist", "discover"),
        reason="mark_failed",
    )
    return item


@router.post("/{download_id}/relocate", responses=_QUEUE_ERROR_RESPONSES)
async def relocate_endpoint(
    download_id: int,
    http_request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
    downloads_host_root: Annotated[str, Depends(get_downloads_host_root)],
) -> QueueItem:
    """Operator correction: relocate an import-blocked, path-invisible download
    into the mounted downloads root (issues #133/#157).

    Scoped to EXACTLY the honest "download path not visible inside the
    container" block (409 ``not_relocatable`` for any other row/reason). qBittorrent
    moves content asynchronously -- this call only REQUESTS the move and returns; the
    operator retries the import (``POST /queue/{id}/import``, already retryable —
    ``import_blocked`` is a resumable state) once qBittorrent settles it. Root-guarded:
    only ever relocates INTO the app's own derived downloads root (409
    ``downloads_root_unavailable`` when that root cannot be derived — bare metal, no
    Docker split), never an arbitrary path. If a concurrent Retry Import re-blocks
    the row with a newer, different reason before this call's own status write
    lands, the move was still requested but the row's message is left alone (409
    ``relocation_superseded`` — re-fetch the queue item to see the current reason).
    Requires qBittorrent configured (409 ``service_not_configured`` otherwise,
    issue #291).
    """
    try:
        record = await correction_service.relocate_stranded_download(
            session,
            qbt,
            download_id=download_id,
            downloads_host_root=downloads_host_root,
        )
    except correction_service.DownloadNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="download_not_found"
        ) from exc
    except correction_service.NotRelocatableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="not_relocatable") from exc
    except correction_service.DownloadsRootUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="downloads_root_unavailable"
        ) from exc
    except correction_service.RelocationSupersededError as exc:
        # The move was still requested of qBittorrent; a concurrent Retry Import
        # re-blocked the row with a newer, different reason before our own status
        # write landed -- surface that honestly rather than clobber it silently.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="relocation_superseded"
        ) from exc
    item = _to_item(record)
    publish_realtime(
        http_request.app,
        ("queue",),
        reason="relocate",
    )
    return item
