"""Queue endpoints — reconciled download list, grab, and mark-failed. AUTHENTICATED."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.release import ScoredRelease
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.parser import ParserPort
from plex_manager.ports.repositories import DownloadRecord
from plex_manager.services import grab_service, queue_service, request_service
from plex_manager.services.grab_service import NoGrabSourceError
from plex_manager.services.queue_service import InvalidStateTransitionError
from plex_manager.web.deps import (
    get_parser,
    get_prowlarr,
    get_qbittorrent,
    get_quality_profile,
    get_session,
    require_api_key,
)
from plex_manager.web.routers.search_preview import run_preview
from plex_manager.web.schemas import (
    GrabRequest,
    QueueItem,
    QueueResponse,
    SearchPreviewRequest,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/queue",
    tags=["queue"],
    dependencies=[Depends(require_api_key)],
)


def _to_item(record: DownloadRecord) -> QueueItem:
    return QueueItem(
        id=record.id,
        torrent_hash=record.torrent_hash,
        status=record.status,
        progress=record.progress,
        seed_ratio=record.seed_ratio,
        media_request_id=record.media_request_id,
        tmdb_id=record.tmdb_id,
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
    qbt: Annotated[DownloadClientPort, Depends(get_qbittorrent)],
) -> QueueResponse:
    """Reconcile active downloads against the client and return the live queue."""
    records = await queue_service.reconcile_and_list(qbt, session)
    return QueueResponse(queue=[_to_item(r) for r in records])


@router.post("/grab", status_code=status.HTTP_201_CREATED)
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

    result = await run_preview(
        SearchPreviewRequest(request_id=body.request_id),
        session,
        prowlarr,
        parser,
        profile,
    )
    if not result.accepted:
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
            season=None,
        )
    except NoGrabSourceError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="no_grab_source") from exc
    return _to_item(record)


@router.post("/{download_id}/mark-failed")
async def mark_failed_endpoint(
    download_id: int,
    session: Annotated[AsyncSession, Depends(get_session)],
    blocklist: Annotated[bool, Query()] = False,
) -> QueueItem:
    """Operator move: mark a download failed (optionally blocklisting the release)."""
    try:
        record = await queue_service.mark_failed(
            session, download_id=download_id, blocklist=blocklist
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
