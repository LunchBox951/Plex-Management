"""Retain a suspect movie for an explicit override while replacement search proceeds."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from plex_manager.domain.media_identity import REVIEWABLE_REQUEST_STATUSES
from plex_manager.domain.state_machine import DownloadState
from plex_manager.logsafe import safe_int
from plex_manager.models import Blocklist, BlocklistReason, Download, RequestStatus
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import blocklist_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.download_client import DownloadClientPort

_logger = logging.getLogger(__name__)


class ImportReviewConflictError(Exception):
    """The held import or its replacement changed; retry against current state."""


async def hold_movie(session: AsyncSession, row: Download, reason: str) -> None:
    download_id, request_id, torrent_hash = row.id, row.media_request_id, row.torrent_hash
    if request_id is None:
        raise ImportReviewConflictError("review has no owning request")
    downloads = SqlDownloadRepository(session)
    changed = await downloads.update_status_if_in(
        download_id,
        DownloadState.NoAcceptableRelease.value,
        frozenset({"import_pending", "import_blocked", "importing"}),
        failed_reason=reason,
    )
    if not changed or not await SqlRequestRepository(session).set_status_if_in(
        request_id,
        RequestStatus.searching.value,
        REVIEWABLE_REQUEST_STATUSES,
    ):
        await session.rollback()
        raise ImportReviewConflictError("request changed during runtime verification")
    source_title = (
        row.release_title
        or await blocklist_service.source_title_for(session, torrent_hash)
        or torrent_hash
    )
    blocked = await SqlBlocklistRepository(session).create(
        source_title=source_title,
        reason=BlocklistReason.failed.value,
        tmdb_id=row.tmdb_id,
        torrent_hash=torrent_hash,
        indexer=await blocklist_service.indexer_for(session, torrent_hash),
        media_type="movie",
    )
    row.import_review_reason = reason
    row.import_review_approved = False
    row.import_review_blocklist_id = blocked.id
    await session.commit()
    _logger.warning(
        "movie import retained for review; replacement search rearmed",
        extra={"download_id": safe_int(download_id), "request_id": safe_int(request_id)},
    )


async def approve_movie(
    session: AsyncSession,
    qbt: DownloadClientPort,
    download_id: int,
) -> None:
    row = await session.get(Download, download_id, populate_existing=True)
    if row is None or row.import_review_reason is None or row.status != "no_acceptable_release":
        raise ImportReviewConflictError("this download is no longer awaiting review")
    if row.media_request_id is None:
        raise ImportReviewConflictError("review has no owning request")
    request_id = row.media_request_id
    requests = SqlRequestRepository(session)
    request = await requests.get(request_id)
    if request is None or request.status not in REVIEWABLE_REQUEST_STATUSES:
        raise ImportReviewConflictError("the request has already completed or was cancelled")
    downloads = SqlDownloadRepository(session)
    replacement = await downloads.find_active_for_request(request_id)
    if replacement is not None:
        if replacement.status == "importing":
            raise ImportReviewConflictError(
                "a replacement is being imported; retry after it finishes"
            )
        replacement_id = replacement.id
        replacement_hash = replacement.torrent_hash
        # No write transaction crosses client I/O. Failure or cancellation leaves
        # both records eligible for the operator to retry the same action.
        await session.rollback()
        try:
            await qbt.pause(replacement_hash)
            for _ in range(5):
                state = await qbt.get_status(replacement_hash)
                if state is None or state.raw_state in {
                    "stoppedDL",
                    "stoppedUP",
                    "pausedDL",
                    "pausedUP",
                    "error",
                    "missingFiles",
                }:
                    break
                await asyncio.sleep(0.1)
            else:
                raise ImportReviewConflictError(
                    "replacement did not stop; retry the import approval"
                )
        except ImportReviewConflictError:
            raise
        except Exception as exc:
            raise ImportReviewConflictError(
                "could not stop the replacement; retry the import approval"
            ) from exc
        row = await session.get(Download, download_id, populate_existing=True)
        request = await requests.get(request_id)
        if (
            row is None
            or row.media_request_id != request_id
            or row.import_review_reason is None
            or row.status != "no_acceptable_release"
            or request is None
            or request.status not in REVIEWABLE_REQUEST_STATUSES
        ):
            raise ImportReviewConflictError("request changed while stopping the replacement")
        replacement = await downloads.find_active_for_request(request_id)
        if replacement is not None and (
            replacement.id != replacement_id or replacement.status == "importing"
        ):
            raise ImportReviewConflictError("replacement changed while it was being stopped")
    try:
        if replacement is not None and not await downloads.update_status_if_in(
            replacement.id,
            "failed",
            frozenset({replacement.status}),
            failed_reason="replacement stopped because a retained import was approved",
        ):
            raise ImportReviewConflictError("replacement changed during approval")
        if not await downloads.update_status_if_in(
            download_id,
            "import_pending",
            frozenset({"no_acceptable_release"}),
            clear_failed_reason=True,
        ) or not await requests.set_status_if_in(
            request_id, "downloading", REVIEWABLE_REQUEST_STATUSES
        ):
            raise ImportReviewConflictError("request changed during approval")
        row.import_review_approved = True
        if row.import_review_blocklist_id is not None:
            blocked = (
                await session.execute(
                    select(Blocklist).where(
                        Blocklist.id == row.import_review_blocklist_id,
                        Blocklist.torrent_hash == row.torrent_hash,
                        Blocklist.reason == BlocklistReason.failed,
                    )
                )
            ).scalar_one_or_none()
            if blocked is not None:
                await session.delete(blocked)
            row.import_review_blocklist_id = None
        await session.commit()
    except (IntegrityError, ImportReviewConflictError) as exc:
        await session.rollback()
        raise ImportReviewConflictError(
            "request changed during approval; retry against current state"
        ) from exc
