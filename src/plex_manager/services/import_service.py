"""Import orchestration — close the loop: validate, hardlink, scan -> Available.

When a completed torrent reaches ``ImportPending`` (the reconciler maps the
client's seeding/complete states there), this service validates the file against
the requested movie with the SAME decision brain the search uses, hardlinks it
into the Movies library under the Plex naming convention, triggers a targeted Plex
scan, and marks the request ``completed`` ("Finalizing"). A later reconcile cycle
confirms availability via :meth:`LibraryPort.is_available` and promotes it to
``available`` — honest two-phase availability (ADR-0010).

A failed validation or move is an honest, retryable ``ImportBlocked`` (a surfaced
reason + the ``POST /queue/{id}/import`` retry button) — never a silent failure
and never a row stranded in ``ImportPending``. Idempotent: re-running on an
already-imported destination skips the copy; the copy runs off the event loop
(``asyncio.to_thread``) so a multi-GB cross-mount copy never blocks the app.

The reconcile loop only AUTO-drains ``ImportPending`` (and a crash-stranded
``Importing``); an ``ImportBlocked`` row is retried only by an explicit operator
action, so a permanently-bad file (e.g. a mislabelled CAM) is not re-validated
every cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.domain.import_validation import VideoFile, validate_import
from plex_manager.domain.naming import plex_movie_relative_path
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import (
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    RequestStatus,
)
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.ports.download_client import DownloadClientPort, DownloadStatus
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.parser import ParserPort
    from plex_manager.ports.repositories import DownloadRecord

__all__ = ["import_download", "run_import_cycle"]

_logger = logging.getLogger(__name__)

# States ``import_download`` will (re)process: a freshly-completed torrent, an
# operator retry of a blocked one, and a row left mid-import by a crash — all
# resumable and idempotent.
_RESUMABLE: frozenset[str] = frozenset(
    {
        DownloadState.ImportPending.value,
        DownloadState.ImportBlocked.value,
        DownloadState.Importing.value,
    }
)
# States the reconcile LOOP auto-drains (ImportBlocked is retried only on demand).
_AUTO_DRAIN: frozenset[str] = frozenset(
    {DownloadState.ImportPending.value, DownloadState.Importing.value}
)


class _NoVideoError(Exception):
    """No importable video file was found under the resolved content path."""


def _resolve_content(status: DownloadStatus | None, download_path: str | None) -> str | None:
    """Resolve the absolute path to a torrent's completed content (file or dir).

    Prefer the client's ``content_path``; the adapter nulls it when it merely
    echoed ``save_path``, so fall back to the stored ``download_path`` and finally
    to ``save_path`` joined with the torrent name. ``save_path`` alone is never
    used — it can hold other torrents' files, which would scan the wrong tree.
    """
    if status is not None and status.content_path:
        return status.content_path
    if download_path:
        return download_path
    if status is not None and status.save_path and status.name:
        return os.path.join(status.save_path, status.name)
    return None


def _resolve_source(fs: FileSystemPort, content_path: str) -> tuple[str, int]:
    """Find the primary video file under ``content_path`` and its size (sync I/O)."""
    src = fs.largest_video_file(content_path)
    if src is None:
        raise _NoVideoError(content_path)
    return src, os.path.getsize(src)


def _place_file(fs: FileSystemPort, src: str, dst: Path) -> None:
    """Hardlink/copy ``src`` to ``dst``, idempotently (sync I/O, run in a thread).

    A fully-imported destination (same size) is left untouched; a partial/stale
    leftover (e.g. a crash mid-copy) is removed and redone, so a re-import never
    blesses a truncated file.
    """
    os.makedirs(dst.parent, exist_ok=True)
    if dst.exists():
        if dst.stat().st_size == os.path.getsize(src):
            return
        os.remove(dst)
    fs.hardlink_or_copy(Path(src), dst)


async def _block(
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    download_id: int,
    reason: str,
    *,
    request_id: int | None = None,
) -> None:
    """Move a download to the retryable ``ImportBlocked`` state, honestly.

    The owning request (when known) is moved to ``import_blocked`` — a surfaced,
    retryable "needs attention" state — so it never lies as ``downloading`` while
    nothing is downloading (north-star #3). The operator retries the import or
    rejects the release (mark-failed -> blocklist + re-search).
    """
    await download_repo.update_status(
        download_id, DownloadState.ImportBlocked.value, failed_reason=reason
    )
    if request_id is not None:
        await SqlRequestRepository(session).set_status(
            request_id, RequestStatus.import_blocked.value
        )
    await session.commit()


async def import_download(
    *,
    download_id: int,
    fs: FileSystemPort,
    library: LibraryPort,
    qbt: DownloadClientPort,
    parser: ParserPort,
    profile: QualityProfile,
    session: AsyncSession,
    movies_root: str,
) -> DownloadRecord | None:
    """Validate, import, and scan a single completed download (movies-first).

    Idempotent and safe to re-run: an already-``Imported`` (or non-import-stage)
    row is a no-op. Returns the resulting :class:`DownloadRecord`, or ``None`` if
    the download id no longer exists.
    """
    download_repo = SqlDownloadRepository(session)
    request_repo = SqlRequestRepository(session)

    row = await session.get(Download, download_id)
    if row is None:
        return None
    if row.status not in _RESUMABLE:
        return await download_repo.get_by_hash(row.torrent_hash)  # already done / not importable
    if row.media_request_id is None:
        await _block(session, download_repo, download_id, "import has no owning request")
        return await download_repo.get_by_hash(row.torrent_hash)

    request = await request_repo.get(row.media_request_id)
    if request is None:
        await _block(session, download_repo, download_id, "owning request no longer exists")
        return await download_repo.get_by_hash(row.torrent_hash)
    if request.media_type != "movie":
        await _block(
            session,
            download_repo,
            download_id,
            "tv import deferred to the next beta",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(row.torrent_hash)

    # Locate the completed video file on disk.
    status = await qbt.get_status(row.torrent_hash)
    content = _resolve_content(status, row.download_path)
    if content is None:
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no content path",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(row.torrent_hash)
    try:
        src, size = await asyncio.to_thread(_resolve_source, fs, content)
    except _NoVideoError:
        await _block(
            session,
            download_repo,
            download_id,
            "no video file found in the download",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(row.torrent_hash)

    # Validate the file IS the requested movie at acceptable quality (same brain as
    # search), gating on profile-allowed (not equal-to-grab) so benign source drift
    # imports while CAM/TS/sample is rejected — the prototype's defining-bug fix.
    validation = validate_import(
        [VideoFile(relative_path=os.path.basename(src), size_bytes=size)],
        parser=parser,
        profile=profile,
        expected_title=request.title,
        expected_year=request.year,
        expected_tmdb_id=request.tmdb_id,
    )
    if not validation.accepted:
        # Surface EVERY rejection the validator collected (not just the first), so
        # the operator sees the full picture before retrying or rejecting.
        reason = "; ".join(f"{r.reason.value}: {r.detail}" for r in validation.rejections)
        await _block(session, download_repo, download_id, reason, request_id=request.id)
        return await download_repo.get_by_hash(row.torrent_hash)

    ext = os.path.splitext(src)[1].lstrip(".")
    relative = plex_movie_relative_path(request.title, request.year, ext)
    dst = Path(movies_root) / relative

    # Commit ``Importing`` BEFORE the (possibly long) copy: the queue shows progress
    # and no DB transaction is held open across the copy. A crash mid-copy leaves
    # the row resumable as ``Importing``; the re-run is idempotent.
    await download_repo.update_status(download_id, DownloadState.Importing.value)
    session.add(
        DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=row.torrent_hash,
            event_type=DownloadHistoryEvent.import_started,
            source_title=os.path.basename(src),
            message=f"importing to {relative}",
        )
    )
    await session.commit()

    try:
        await asyncio.to_thread(_place_file, fs, src, dst)
    except OSError as exc:
        await _block(
            session,
            download_repo,
            download_id,
            f"import copy failed: {type(exc).__name__}",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(row.torrent_hash)

    # Targeted Plex scan of the movie folder — the partial scan the prototype never
    # did. A scan failure (incl. a path no Plex movie section covers) is retryable
    # (the file IS in the library), so block rather than assert availability the
    # operator can't see.
    try:
        await library.trigger_scan(str(dst.parent))
    except (PlexLibraryError, PlexAuthError) as exc:
        await _block(
            session,
            download_repo,
            download_id,
            f"plex scan failed: {type(exc).__name__}",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(row.torrent_hash)

    # Imported. The download is terminal; the request is 'completed' ("Finalizing")
    # until a reconcile cycle confirms availability via is_available (phase 2).
    await download_repo.update_status(
        download_id, DownloadState.Imported.value, download_path=str(dst)
    )
    session.add(
        DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=row.torrent_hash,
            event_type=DownloadHistoryEvent.imported,
            source_title=os.path.basename(src),
            message=f"imported to {relative}",
        )
    )
    await request_repo.mark_completed(request.id)
    await session.commit()
    return await download_repo.get_by_hash(row.torrent_hash)


async def run_import_cycle(
    *,
    fs: FileSystemPort,
    library: LibraryPort,
    qbt: DownloadClientPort,
    parser: ParserPort,
    profile: QualityProfile,
    session: AsyncSession,
    movies_root: str,
) -> None:
    """One pass of the import + availability phases (driven by the reconcile loop).

    Phase 1: drain freshly-completed (and crash-stranded) imports. Phase 2: confirm
    ``completed`` movies are indexed in Plex and promote them to ``available``. One
    item failing never aborts the cycle (it is logged and retried next cycle).
    """
    download_repo = SqlDownloadRepository(session)
    for row in await download_repo.list_active():
        if row.status in _AUTO_DRAIN and row.media_request_id is not None:
            try:
                await import_download(
                    download_id=row.id,
                    fs=fs,
                    library=library,
                    qbt=qbt,
                    parser=parser,
                    profile=profile,
                    session=session,
                    movies_root=movies_root,
                )
            except Exception:
                await session.rollback()
                _logger.exception("import of download %s failed; will retry next cycle", row.id)

    request_repo = SqlRequestRepository(session)
    for request in await request_repo.list_by_status(RequestStatus.completed.value):
        if request.media_type != "movie":
            continue
        try:
            if await library.is_available(request.tmdb_id, "movie"):
                await request_repo.mark_available(request.id)
                await session.commit()
        except (PlexLibraryError, PlexAuthError, NotImplementedError):
            await session.rollback()
            _logger.warning(
                "availability check failed for tmdb %s; will retry next cycle", request.tmdb_id
            )
