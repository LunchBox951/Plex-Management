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
import contextlib
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

__all__ = ["import_download", "run_availability_cycle", "run_import_cycle"]

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


def _resolve_source(fs: FileSystemPort, content_path: str) -> tuple[str, int, str]:
    """Find the primary video file under ``content_path``: ``(abs_path, size, rel)``.

    ``rel`` includes the release FOLDER, not just the file. A torrent whose folder
    carries the title/quality (``The.Matrix.1999.1080p.WEB-DL/movie.mkv``) but ships
    a generic feature file would otherwise reach the validator as a token-less
    ``movie.mkv`` and be wrongly rejected as wrong/unknown media; anchoring the
    relative path ABOVE the download root keeps the folder tokens — and any
    ``CD1``/``Disc 1`` split-disk marker under it — in the string the validator
    parses. For a single-file torrent (``content_path`` is the file) the anchor is
    the save dir, so ``rel`` is just the token-rich filename, which is sufficient.
    """
    src = fs.largest_video_file(content_path)
    if src is None:
        raise _NoVideoError(content_path)
    anchor = (
        os.path.dirname(os.path.normpath(content_path))
        if os.path.isdir(content_path)
        else os.path.dirname(content_path)
    )
    return src, os.path.getsize(src), os.path.relpath(src, anchor)


def _place_file(fs: FileSystemPort, src: str, dst: Path) -> bool:
    """Hardlink/copy ``src`` to ``dst``, idempotently (sync I/O, run in a thread).

    Returns ``True`` iff THIS call created ``dst``; ``False`` when ``dst`` was
    already supplied by another writer — a prior fully-imported copy (idempotent
    skip) or a concurrent import that won a placement race. The caller rolls ``dst``
    back on a later failure ONLY when it actually placed it, so it never unlinks a
    file another import (or the user) owns.

    A fully-imported destination (same size) is left untouched. A *differently*-sized
    file already at ``dst`` (a user's library file, or a stale partial) is NEVER
    blind-deleted — it is surfaced as a ``FileExistsError`` conflict for the operator
    to resolve, so a re-import never silently overwrites someone else's file.
    """
    os.makedirs(dst.parent, exist_ok=True)
    if dst.exists():
        if dst.stat().st_size == os.path.getsize(src):
            return False  # already fully imported here — idempotent skip; not ours
        # A differently-sized file is already at the destination: a user's
        # manually-managed library file, or a title Plex availability missed. NEVER
        # blind-delete it (that is data loss) — surface it as an import conflict the
        # operator resolves, instead of overwriting their file with the download.
        raise FileExistsError(f"destination already exists with different content: {dst}")
    try:
        fs.hardlink_or_copy(Path(src), dst)
    except FileExistsError:
        # Lost a placement race: a concurrent import (the reconcile loop racing the
        # operator's POST /queue/{id}/import retry) created ``dst`` between the
        # exists() check above and this link. Same content (same size) is an
        # idempotent win for the other attempt, NOT a failure to block on; a
        # different size is a genuine conflict, surfaced like the pre-existing case.
        if dst.exists() and dst.stat().st_size == os.path.getsize(src):
            return False  # the race winner's file — not ours to roll back
        raise
    return True  # we created dst; a later failure may roll it back


def _remove_quietly(path: Path) -> None:
    """Best-effort unlink (rolling back a placed file when a later step fails)."""
    with contextlib.suppress(OSError):
        path.unlink()


# Per-download serialization. The reconcile loop and an operator's
# POST /queue/{id}/import retry share ONE event loop (single process; SQLite is the
# store), so without this two import attempts of the SAME download could both claim
# ``Importing`` and race on placement/finalize — risking deletion of the file the
# other attempt placed. One lock per download id; different downloads never block.
# The registry grows by one small lock per imported download and is never evicted:
# bounded by the lifetime download count (negligible for a self-hosted server).
# Correct per-key eviction needs a ref-counted manager (a bare pop races a waiter),
# so it is deferred rather than done unsafely.
_import_locks: dict[int, asyncio.Lock] = {}


def _import_lock(download_id: int) -> asyncio.Lock:
    lock = _import_locks.get(download_id)
    if lock is None:
        lock = asyncio.Lock()
        _import_locks[download_id] = lock
    return lock


async def _block(
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    download_id: int,
    reason: str,
    *,
    request_id: int | None = None,
    clear_download_path: bool = False,
) -> None:
    """Move a download to the retryable ``ImportBlocked`` state, honestly.

    The owning request (when known) is moved to ``import_blocked`` — a surfaced,
    retryable "needs attention" state — so it never lies as ``downloading`` while
    nothing is downloading (north-star #3). The operator retries the import or
    rejects the release (mark-failed -> blocklist + re-search).
    """
    await download_repo.update_status(
        download_id,
        DownloadState.ImportBlocked.value,
        failed_reason=reason,
        clear_download_path=clear_download_path,
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

    Serialized per download id: the reconcile loop and an operator's
    POST /queue/{id}/import retry must never import the SAME row concurrently.
    """
    async with _import_lock(download_id):
        return await _import_download_locked(
            download_id=download_id,
            fs=fs,
            library=library,
            qbt=qbt,
            parser=parser,
            profile=profile,
            session=session,
            movies_root=movies_root,
        )


async def _import_download_locked(
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
    download_repo = SqlDownloadRepository(session)
    request_repo = SqlRequestRepository(session)

    row = await session.get(Download, download_id)
    if row is None:
        return None
    # Capture the hash now: the conditional-claim abort path below rolls back, which
    # expires ``row``; reading ``row.torrent_hash`` after that would trigger an async
    # lazy-load from a sync context (MissingGreenlet).
    torrent_hash = row.torrent_hash
    if row.status not in _RESUMABLE:
        return await download_repo.get_by_hash(torrent_hash)  # already done / not importable
    if row.media_request_id is None:
        await _block(session, download_repo, download_id, "import has no owning request")
        return await download_repo.get_by_hash(torrent_hash)

    request = await request_repo.get(row.media_request_id)
    if request is None:
        await _block(session, download_repo, download_id, "owning request no longer exists")
        return await download_repo.get_by_hash(torrent_hash)
    if request.media_type != "movie":
        await _block(
            session,
            download_repo,
            download_id,
            "tv import deferred to the next beta",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)

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
        return await download_repo.get_by_hash(torrent_hash)
    try:
        src, size, source_rel = await asyncio.to_thread(_resolve_source, fs, content)
    except _NoVideoError:
        await _block(
            session,
            download_repo,
            download_id,
            "no video file found in the download",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)

    # Validate the file IS the requested movie at acceptable quality (same brain as
    # search), gating on profile-allowed (not equal-to-grab) so benign source drift
    # imports while CAM/TS/sample is rejected — the prototype's defining-bug fix.
    validation = validate_import(
        [VideoFile(relative_path=source_rel, size_bytes=size)],
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
        return await download_repo.get_by_hash(torrent_hash)

    ext = os.path.splitext(src)[1].lstrip(".")
    relative = plex_movie_relative_path(request.title, request.year, ext)
    dst = Path(movies_root) / relative

    # Claim ``Importing`` with a compare-and-swap BEFORE the (possibly long) copy: the
    # queue shows progress and no DB transaction is held open across the copy. A crash
    # mid-copy leaves the row resumable as ``Importing``; the re-run is idempotent.
    #
    # The claim is CONDITIONAL on the row still being resumable. The validation above
    # (qbt status, source resolve, parse) is a long async gap; an operator can call
    # ``mark_failed`` in a separate session during it, committing ``failed`` +
    # blocklist + re-search. An unconditional update would overwrite that committed
    # decision AND go on to copy and complete the rejected release. If the CAS fails
    # (the row left ``_RESUMABLE`` underneath us), abort honestly and return the row's
    # current state without importing. (The per-download lock already excludes a second
    # concurrent import; this CAS handles the separate mark_failed path.)
    claimed = await download_repo.update_status_if_in(
        download_id, DownloadState.Importing.value, _RESUMABLE
    )
    if not claimed:
        await session.rollback()
        return await download_repo.get_by_hash(torrent_hash)
    session.add(
        DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=torrent_hash,
            event_type=DownloadHistoryEvent.import_started,
            # NULL on purpose: queue_service._source_title_for returns the latest
            # non-null history source_title for the blocklist, and the import file
            # basename must NOT shadow the grabbed RELEASE title. Keep the basename in
            # ``message`` only.
            source_title=None,
            message=f"importing {os.path.basename(src)} to {relative}",
        )
    )
    await session.commit()

    try:
        placed = await asyncio.to_thread(_place_file, fs, src, dst)
    except FileExistsError as exc:
        # A pre-existing, differently-sized file at the destination (a user's file,
        # or a stale partial) — surfaced as a conflict, never overwritten.
        await _block(session, download_repo, download_id, str(exc), request_id=request.id)
        return await download_repo.get_by_hash(torrent_hash)
    except OSError as exc:
        await _block(
            session,
            download_repo,
            download_id,
            f"import copy failed: {type(exc).__name__}",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)

    # Record the placed library file on the still-``Importing`` row BEFORE the scan,
    # so a crash between placement and the ``Imported`` write leaves a durable
    # breadcrumb: a resumed run finds ``download_path == dst`` (the file still on disk)
    # and can roll that orphan back on a repeat scan failure (F8). Written ONLY when
    # THIS attempt placed dst — a lost-race loser (placed is False) never stamps the
    # winner's file (F3). The per-download lock means no other import races this write.
    if placed:
        await download_repo.update_status(
            download_id, DownloadState.Importing.value, download_path=str(dst)
        )
        await session.commit()

    # Targeted Plex scan of the movie folder — the partial scan the prototype never
    # did. movies_root is a Plex library location (the picker guarantees the path↔
    # section match), so a scan failure here is a transient Plex error, not a wrong
    # path. Roll the file back before blocking so a later reject / re-search can't
    # orphan it (the retry re-places it). We own the rollback when THIS attempt placed
    # dst OR a prior (crashed) attempt of this row placed it and left the breadcrumb
    # (download_path == dst); a lost-race loser owns neither, so the winner's file is
    # left intact. Clear the breadcrumb on rollback so the deleted path can't shadow
    # the torrent's content on a later retry.
    try:
        await library.trigger_scan(str(dst.parent))
    except (PlexLibraryError, PlexAuthError) as exc:
        # ``row.download_path`` is read live here (the breadcrumb commit set it for a
        # placed=True attempt; a crash-resume loaded it as dst). Safe only because the
        # sessionmaker uses ``expire_on_commit=False`` and the claim CAS's
        # ``synchronize_session="fetch"`` refreshes only ``status`` — changing either
        # would turn this into a post-commit lazy-load (MissingGreenlet) hazard.
        owns_placement = placed or row.download_path == str(dst)
        if owns_placement:
            await asyncio.to_thread(_remove_quietly, dst)
        await _block(
            session,
            download_repo,
            download_id,
            f"plex scan failed: {type(exc).__name__}",
            request_id=request.id,
            clear_download_path=owns_placement,
        )
        return await download_repo.get_by_hash(torrent_hash)

    # Imported. The download is terminal; the request is 'completed' ("Finalizing")
    # until a reconcile cycle confirms availability via is_available (phase 2).
    # Finalize with a compare-and-swap, conditional on STILL holding the ``Importing``
    # claim. The per-download lock + the legal graph (a mark_failed on an ``Importing``
    # row 409s) mean the row should not have moved during the copy/scan; the CAS is
    # defense-in-depth. If it did move, abandon the finalize rather than overwrite
    # whatever moved it — and do NOT remove dst (the deterministic path a retry
    # re-adopts), so a successfully-placed file is never deleted.
    finalized = await download_repo.update_status_if_in(
        download_id,
        DownloadState.Imported.value,
        frozenset({DownloadState.Importing.value}),
        download_path=str(dst),
    )
    if not finalized:
        await session.rollback()
        return await download_repo.get_by_hash(torrent_hash)
    session.add(
        DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=torrent_hash,
            event_type=DownloadHistoryEvent.imported,
            source_title=None,  # never shadow the grabbed title (see import_started above)
            message=f"imported {os.path.basename(src)} to {relative}",
        )
    )
    await request_repo.mark_completed(request.id)
    await session.commit()
    return await download_repo.get_by_hash(torrent_hash)


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
    """Drain freshly-completed (and crash-stranded) imports. Needs the download
    client + the Movies root. One item failing never aborts the cycle.

    The completed -> available promotion is a SEPARATE pass
    (:func:`run_availability_cycle`) that needs only Plex, so it keeps working even
    when the download client is down or the Movies root is unset.
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


async def run_availability_cycle(*, library: LibraryPort, session: AsyncSession) -> None:
    """Confirm ``completed`` ("Finalizing") movies are indexed in Plex and promote
    them to ``available``. Depends ONLY on Plex — so an import that already triggered
    a scan still reaches ``available`` even if the download client or Movies root is
    unavailable afterward. One item failing never aborts the cycle.
    """
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
