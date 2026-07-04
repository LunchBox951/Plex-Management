"""Import orchestration — close the loop: validate, hardlink, scan -> Available.

When a completed torrent reaches ``ImportPending`` (the reconciler maps the
client's seeding/complete states there), this service validates the file against
the requested movie/show with the SAME decision brain the search uses, hardlinks
it into the Movies/TV library under the Plex naming convention, triggers a
targeted Plex scan, and marks the request (or, for TV, the season)
``completed`` ("Finalizing"). A later reconcile cycle confirms availability via
:meth:`LibraryPort.is_available` and promotes it to ``available`` — honest
two-phase availability (ADR-0010).

A failed validation or move is an honest, retryable ``ImportBlocked`` (a surfaced
reason + the ``POST /queue/{id}/import`` retry button) — never a silent failure
and never a row stranded in ``ImportPending``. Idempotent: re-running on an
already-imported destination skips the copy; the copy runs off the event loop
(``asyncio.to_thread``) so a multi-GB cross-mount copy never blocks the app.

The reconcile loop only AUTO-drains ``ImportPending`` (and a crash-stranded
``Importing``); an ``ImportBlocked`` row is retried only by an explicit operator
action, so a permanently-bad file (e.g. a mislabelled CAM) is not re-validated
every cycle.

TV is season-scoped and legitimately PARTIAL: a season-pack download can ship
many independently-valid episode files, so :func:`_import_tv_locked` validates
and places EVERY file, running ONE targeted Plex scan for the whole season
directory (never one per episode) and honestly blocking only when NOTHING in the
download validated.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import weakref
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.domain.import_validation import (
    EpisodeImportResult,
    VideoFile,
    validate_import,
    validate_season_import,
)
from plex_manager.domain.naming import (
    plex_movie_relative_path,
    plex_tv_episode_relative_path,
    plex_tv_season_relative_dir,
)
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import (
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    RequestStatus,
)
from plex_manager.ports.filesystem import VIDEO_EXTENSIONS
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import season_request_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.ports.download_client import DownloadClientPort, DownloadStatus
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.parser import ParserPort
    from plex_manager.ports.repositories import DownloadRecord, RequestRecord

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
# qBittorrent states whose files are no longer being downloaded or moved. The
# importer reads the filesystem directly, so it must only touch settled payloads.
_IMPORT_READY_RAW_STATES: frozenset[str] = frozenset(
    {"uploading", "stalledUP", "pausedUP", "stoppedUP", "queuedUP", "checkingUP", "forcedUP"}
)

# Cap on how many per-file rejection reasons a whole-season block's
# ``failed_reason`` joins together -- a 20+ episode pack where every file is
# rejected must not produce an unreadably long string.
_MAX_BLOCK_REASONS: Final = 10


class _NoVideoError(Exception):
    """No importable video file was found under the resolved content path."""


class _UnsafeContentPathError(Exception):
    """The download client reported a content path outside its save path."""


def _is_settled_for_import(status: DownloadStatus) -> bool:
    return status.progress >= 1.0 and status.raw_state in _IMPORT_READY_RAW_STATES


def _is_within(root_real: str, candidate_real: str) -> bool:
    """True if ``candidate_real`` is ``root_real`` or sits under it (both realpaths).

    Mirrors ``adapters.filesystem.local._is_within`` verbatim (kept local rather
    than imported: that name is private to the adapter, and this service must not
    reach into a specific ``FileSystemPort`` implementation's internals).
    """
    return candidate_real == root_real or candidate_real.startswith(root_real + os.sep)


def _ensure_under_save_path(save_path: str, candidate: str) -> str:
    root_real = os.path.realpath(save_path)
    candidate_real = os.path.realpath(candidate)
    if not _is_within(root_real, candidate_real):
        raise _UnsafeContentPathError("download content path is outside download save path")
    return candidate


def _resolve_content(status: DownloadStatus | None, download_path: str | None) -> str | None:
    """Resolve the absolute path to a torrent's completed content (file or dir).

    Prefer the client's ``content_path``; the adapter nulls it when it merely
    echoed ``save_path``, so fall back to the stored ``download_path`` and finally
    to ``save_path`` joined with the torrent name. ``save_path`` alone is never
    used — it can hold other torrents' files, which would scan the wrong tree.
    """
    if status is not None and status.content_path:
        if status.save_path:
            return _ensure_under_save_path(status.save_path, status.content_path)
        raise _UnsafeContentPathError("download client reported content path without save path")
    if status is not None and status.save_path and status.name:
        if os.path.isabs(status.name):
            raise _UnsafeContentPathError("download content path is outside download save path")
        return _ensure_under_save_path(
            status.save_path, os.path.join(status.save_path, status.name)
        )
    if download_path:
        return download_path
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


def _resolve_tv_sources(fs: FileSystemPort, content_path: str) -> list[tuple[str, int, str]]:
    """Enumerate EVERY candidate video file under a TV download's content.

    Same anchor-above-content-root trick as :func:`_resolve_source` (the release
    folder's season/quality tokens stay in the parsed relative path), but returns
    every eligible file rather than picking one "largest" feature: a season pack
    legitimately ships many independently-valid episodes, each validated on its
    own by :func:`~plex_manager.domain.import_validation.validate_season_import`.
    """
    root_path = Path(content_path)
    if root_path.is_file():
        # A single-file torrent (no season-pack folder): mirror
        # ``largest_video_file``'s is_file branch (adapters/filesystem/local.py)
        # verbatim -- the lone file is the only candidate, and its filename alone
        # is sufficient (no folder to anchor above). Same containment + extension
        # guard as that branch: a single-episode grab is a single-file torrent, so
        # a content root that is ITSELF a symlink escaping its own parent
        # directory (or that isn't even a video file) must never be followed, or
        # the importer would copy an arbitrary out-of-tree file into the public
        # TV library. An honest "no video found" ([]) -> whole-download block,
        # never a silent skip.
        resolved = os.path.realpath(content_path)
        parent_real = os.path.realpath(root_path.parent)
        if root_path.suffix.lower() not in VIDEO_EXTENSIONS or not _is_within(
            parent_real, resolved
        ):
            return []
        return [(resolved, os.path.getsize(resolved), root_path.name)]
    anchor = os.path.dirname(os.path.normpath(content_path))
    return [
        (abs_path, size, os.path.relpath(abs_path, anchor))
        for abs_path, size, _rel in fs.list_video_files(content_path)
    ]


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
        if _same_file_content(src, dst):
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
        if dst.exists() and _same_file_content(src, dst):
            return False  # the race winner's file — not ours to roll back
        raise FileExistsError(f"destination already exists with different content: {dst}") from None
    return True  # we created dst; a later failure may roll it back


def _file_digest(path: str | Path) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _same_file_content(src: str, dst: Path) -> bool:
    # samefile is only the cheap same-inode short-circuit; on any OSError (a side
    # not stat-able) fall through to the honest size + digest comparison below.
    with contextlib.suppress(OSError):
        if os.path.samefile(src, dst):
            return True
    if dst.stat().st_size != os.path.getsize(src):
        return False
    return _file_digest(src) == _file_digest(dst)


def _remove_quietly(path: Path) -> None:
    """Best-effort unlink (rolling back a placed file when a later step fails)."""
    with contextlib.suppress(OSError):
        path.unlink()


def _remove_quietly_many(paths: list[Path]) -> None:
    """Best-effort unlink of every path THIS call placed (TV scan-failure rollback).

    Unlike the movie path's single ``dst``, a season import can place several
    episode files before its one combined scan fails; each is rolled back the
    same best-effort way.
    """
    for path in paths:
        _remove_quietly(path)


# Per-download serialization. The reconcile loop and an operator's
# POST /queue/{id}/import retry share ONE event loop (single process; SQLite is the
# store), so without this two import attempts of the SAME download could both claim
# ``Importing`` and race on placement/finalize — risking deletion of the file the
# other attempt placed. One lock per download id; different downloads never block.
#
# A ``WeakValueDictionary`` keeps this self-bounded WITHOUT a naive pop-after-
# release, which would race an incoming waiter that already captured a reference
# to the old Lock object (two Lock instances for one download_id loses mutual
# exclusion — the hazard a bare pop() would reintroduce). As long as a coroutine
# is inside or awaiting ``async with _import_lock(download_id):``, the ``async
# with`` statement's own temporary holds a strong reference to that Lock object
# for the whole block, including while suspended on ``.acquire()`` — CPython's
# immediate refcounting only drops the weak-value entry once the TRUE last
# strong reference (no coroutine still holding/awaiting it) disappears, never
# mid-wait. Relies on CPython's deterministic refcounting (not portable to
# PyPy); acceptable for this project's single-runtime deployment target.
_import_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


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
    season: int | None = None,
    clear_download_path: bool = False,
) -> None:
    """Move a download to the retryable ``ImportBlocked`` state, honestly.

    The block is a COMPARE-AND-SWAP, conditional on the row still being resumable.
    Every pre-claim caller runs after a long async gap (qbt status, source resolve,
    parse) during which an operator can ``mark_failed`` the row in a SEPARATE session
    — committing ``failed`` + blocklist + re-search. An unconditional block would
    overwrite that committed decision (``failed`` -> ``import_blocked``) and re-arm the
    request away from ``searching``, undoing the operator. So we block ONLY while the
    row is still in ``_RESUMABLE``; if it left (the operator failed it), do nothing and
    return — honoring the operator's decision.

    The owning request (when known) is moved to ``import_blocked`` — a surfaced,
    retryable "needs attention" state — so it never lies as ``downloading`` while
    nothing is downloading (north-star #3) — but ONLY when the block actually applied,
    so a row the operator already re-armed to ``searching`` is left alone. The operator
    retries the import or rejects the release (mark-failed -> blocklist + re-search).

    ``season`` (TV only) routes the write through ``season_request_service`` instead
    of setting the request status directly: a TV request's status is a COMPUTED
    rollup of its seasons (never itself the direct target of a move), so only the
    OWNING SEASON moves to ``import_blocked`` and the parent's rollup is recomputed
    from it. ``None`` (movie, the default) leaves the movie behaviour unchanged.
    """
    blocked = await download_repo.update_status_if_in(
        download_id,
        DownloadState.ImportBlocked.value,
        _RESUMABLE,
        failed_reason=reason,
        clear_download_path=clear_download_path,
    )
    if not blocked:
        # The row left ``_RESUMABLE`` underneath us (an operator's mark_failed
        # committed ``failed`` + blocklist + re-search during the validation gap).
        # Honor that: don't overwrite it and don't re-arm the request. Roll back so the
        # caller's get_by_hash reads the operator's committed state, not this
        # transaction's stale snapshot.
        await session.rollback()
        return
    if request_id is not None:
        if season is not None:
            await season_request_service.set_status(
                session,
                media_request_id=request_id,
                season_number=season,
                status=RequestStatus.import_blocked.value,
            )
        else:
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
    movies_root: str | None = None,
    tv_root: str | None = None,
) -> DownloadRecord | None:
    """Validate, import, and scan a single completed download.

    Idempotent and safe to re-run: an already-``Imported`` (or non-import-stage)
    row is a no-op. Returns the resulting :class:`DownloadRecord`, or ``None`` if
    the download id no longer exists.

    Both roots are optional and independently honest: a movie download reaching
    this function while ``movies_root`` is unset gets a retryable ``ImportBlocked``
    ("movies library root is not configured"); a tv download reaching it while
    ``tv_root`` is unset gets the same treatment ("tv library root is not
    configured"). Neither ever crashes, and an install that has only ONE of the
    two roots configured still imports that type normally.

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
            tv_root=tv_root,
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
    movies_root: str | None = None,
    tv_root: str | None = None,
) -> DownloadRecord | None:
    download_repo = SqlDownloadRepository(session)
    request_repo = SqlRequestRepository(session)

    row = await session.get(Download, download_id)
    if row is None:
        return None
    # Capture now: the conditional-claim abort path below rolls back, which
    # expires ``row``; reading these after that would trigger an async lazy-load
    # from a sync context (MissingGreenlet). ``season``/``episodes`` are movie-
    # irrelevant (always ``None`` there) and only consumed by the tv branch below.
    torrent_hash = row.torrent_hash
    season = row.season
    episodes = row.episodes_json
    starting_status = row.status
    if row.status not in _RESUMABLE:
        return await download_repo.get_by_hash(torrent_hash)  # already done / not importable
    if row.media_request_id is None:
        await _block(session, download_repo, download_id, "import has no owning request")
        return await download_repo.get_by_hash(torrent_hash)

    request = await request_repo.get(row.media_request_id)
    if request is None:
        await _block(session, download_repo, download_id, "owning request no longer exists")
        return await download_repo.get_by_hash(torrent_hash)

    if request.media_type == "tv":
        if tv_root is None:
            await _block(
                session,
                download_repo,
                download_id,
                "tv library root is not configured",
                request_id=request.id,
                season=season,
            )
            return await download_repo.get_by_hash(torrent_hash)
        if season is None:  # pragma: no cover - grab_service always threads season for tv
            await _block(
                session,
                download_repo,
                download_id,
                "tv download is missing its season",
                request_id=request.id,
            )
            return await download_repo.get_by_hash(torrent_hash)
        return await _import_tv_locked(
            download_id=download_id,
            request=request,
            season=season,
            episodes=episodes,
            download_path=row.download_path,
            fs=fs,
            library=library,
            qbt=qbt,
            parser=parser,
            profile=profile,
            session=session,
            tv_root=tv_root,
            torrent_hash=torrent_hash,
        )
    if request.media_type != "movie":  # pragma: no cover - MediaType enum has only movie/tv
        await _block(
            session,
            download_repo,
            download_id,
            f"unsupported media_type {request.media_type!r}",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)

    if movies_root is None:
        # Mirrors the tv branch's ``tv_root is None`` guard above: an honest,
        # retryable block rather than gating this whole cycle on movies_root being
        # set (an install with only the TV root configured must still import TV).
        await _block(
            session,
            download_repo,
            download_id,
            "movies library root is not configured",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)

    # Locate the completed video file on disk.
    status = await qbt.get_status(row.torrent_hash)
    if status is not None and not _is_settled_for_import(status):
        # The row may be resumable because a prior reconcile saw completion, but the
        # live client can still be moving/downloading the payload. Do not validate or
        # import a changing file tree; re-arm the honest Downloading state and let the
        # reconciler promote it back to ImportPending when qBittorrent settles.
        deferred = await download_repo.update_status_if_in(
            download_id,
            DownloadState.Downloading.value,
            _RESUMABLE,
            clear_failed_reason=True,
            progress=status.progress,
            seed_ratio=status.ratio,
        )
        if not deferred:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash)
        await request_repo.set_status(request.id, RequestStatus.downloading.value)
        await session.commit()
        return await download_repo.get_by_hash(torrent_hash)
    try:
        content = _resolve_content(status, row.download_path)
    except _UnsafeContentPathError as exc:
        await _block(session, download_repo, download_id, str(exc), request_id=request.id)
        return await download_repo.get_by_hash(torrent_hash)
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
        download_id,
        DownloadState.Importing.value,
        _RESUMABLE,
        clear_failed_reason=True,
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
    recovered_orphan = (
        not placed
        and starting_status == DownloadState.Importing.value
        and row.download_path is None
    )

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
        await library.trigger_scan(str(dst.parent), "movie")
    except (PlexLibraryError, PlexAuthError) as exc:
        # ``row.download_path`` is read live here (the breadcrumb commit set it for a
        # placed=True attempt; a crash-resume loaded it as dst). Safe only because the
        # sessionmaker uses ``expire_on_commit=False`` and the claim CAS's
        # ``synchronize_session="fetch"`` refreshes only ``status`` — changing either
        # would turn this into a post-commit lazy-load (MissingGreenlet) hazard.
        owns_placement = placed or recovered_orphan or row.download_path == str(dst)
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
        clear_failed_reason=True,
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
    # Persist the eviction breadcrumb (ADR-0012) NOW, in the SAME transaction as
    # the finalize -- ``dst.parent`` is the movie's own folder (Plex's
    # ``Title (Year)/`` dir the file was placed into), the exact directory a
    # later disk-pressure sweep's ``fs.delete()`` must remove. Without this,
    # ``MediaRequest.library_path`` stays ``None`` forever and
    # ``eviction_service._movie_candidates`` has no deletion target for this
    # request (see that module's docstring on skipping a candidate with no
    # stored breadcrumb).
    await request_repo.set_library_path(request.id, str(dst.parent))
    await request_repo.mark_completed(request.id)
    await session.commit()
    return await download_repo.get_by_hash(torrent_hash)


async def _import_tv_locked(
    *,
    download_id: int,
    request: RequestRecord,
    season: int,
    episodes: list[int] | None,
    download_path: str | None,
    fs: FileSystemPort,
    library: LibraryPort,
    qbt: DownloadClientPort,
    parser: ParserPort,
    profile: QualityProfile,
    session: AsyncSession,
    tv_root: str,
    torrent_hash: str,
) -> DownloadRecord | None:
    """Validate EVERY file in a completed TV season download; import whatever
    accepted, ONE Plex scan for the whole season, then mark the SEASON completed.

    Mirrors ``_import_download_locked`` (movies-first) but a season-pack download
    legitimately ships MANY independently-valid episode files, so partial success
    is legitimate: every file :func:`~plex_manager.domain.import_validation.
    validate_season_import` accepts is placed, one targeted scan runs for the
    whole season directory (never one per episode), and a whole-season block only
    happens when NOTHING in the download validated. Rejected files are LOGGED (not
    persisted as history); skipped-not-requested files (validated cleanly but
    outside an operator-scoped ``episodes`` filter) are dropped silently -- neither
    is a failure worth surfacing on its own. Accepted files that collapse to the
    SAME destination (e.g. an episode plus its PROPER/REPACK) are deduped to the
    largest one BEFORE placing, so one duplicate can never roll back / block
    every OTHER genuinely-distinct episode in the pack.

    Per-episode ``imported`` :class:`DownloadHistory` rows are staged in memory
    while files are placed and only durably committed once the scan AND the
    finalize compare-and-swap have BOTH succeeded -- mirroring the movie path,
    which likewise writes its ``imported`` row only after a successful scan. A
    later file's copy failure or a scan failure rolls placed files back via
    :func:`_remove_quietly_many`/:func:`_block` before any of those rows are ever
    added to the session, so the audit trail can never claim an episode was
    imported when it was in fact deleted moments later.

    Unlike the movie path's single ``download_path`` breadcrumb (which lets a
    crash-resumed run re-adopt a placement it made before a prior crash), a TV
    import that crashes mid-copy is simply retried by a fresh run: each file's
    placement is independently idempotent (:func:`_place_file` skips an already-
    identical-size destination), so only the files THIS invocation itself placed
    are rolled back on a scan failure -- a single column cannot represent "N
    placed files" across invocations, so cross-invocation ownership tracking is
    not attempted here (a scoped follow-up, like true per-episode completeness).
    """
    download_repo = SqlDownloadRepository(session)

    status = await qbt.get_status(torrent_hash)
    if status is not None and not _is_settled_for_import(status):
        deferred = await download_repo.update_status_if_in(
            download_id,
            DownloadState.Downloading.value,
            _RESUMABLE,
            clear_failed_reason=True,
            progress=status.progress,
            seed_ratio=status.ratio,
        )
        if not deferred:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash)
        await season_request_service.set_status(
            session,
            media_request_id=request.id,
            season_number=season,
            status=RequestStatus.downloading.value,
        )
        await session.commit()
        return await download_repo.get_by_hash(torrent_hash)
    try:
        content = _resolve_content(status, download_path)
    except _UnsafeContentPathError as exc:
        await _block(
            session, download_repo, download_id, str(exc), request_id=request.id, season=season
        )
        return await download_repo.get_by_hash(torrent_hash)
    if content is None:
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no content path",
            request_id=request.id,
            season=season,
        )
        return await download_repo.get_by_hash(torrent_hash)

    sources = await asyncio.to_thread(_resolve_tv_sources, fs, content)
    if not sources:
        await _block(
            session,
            download_repo,
            download_id,
            "no video file found in the download",
            request_id=request.id,
            season=season,
        )
        return await download_repo.get_by_hash(torrent_hash)

    # Validate EVERY file against the SAME decision brain the search uses (title +
    # season identity, quality gate, sample floor, episode-number gate). Partial
    # success is legitimate here, unlike the movie path's all-or-nothing verdict.
    validation = validate_season_import(
        [VideoFile(relative_path=rel, size_bytes=size) for _abs, size, rel in sources],
        parser=parser,
        profile=profile,
        expected_title=request.title,
        expected_tmdb_id=request.tmdb_id,
        expected_season=season,
        requested_episodes=episodes,
    )
    for rejection in validation.rejected:
        _logger.warning(
            "tv import: rejected %s (%s): %s",
            rejection.relative_path,
            rejection.reason.value,
            rejection.detail,
        )

    if not validation.accepted:
        # Nothing survived -- an honest, whole-season block. Cap the joined
        # per-file reasons so a large pack where every file is rejected doesn't
        # produce an unreadably long ``failed_reason``.
        reason_parts = [
            f"{r.relative_path}: {r.reason.value}: {r.detail}"
            for r in validation.rejected[:_MAX_BLOCK_REASONS]
        ]
        if len(validation.rejected) > _MAX_BLOCK_REASONS:
            reason_parts.append(f"(+{len(validation.rejected) - _MAX_BLOCK_REASONS} more)")
        reason = "; ".join(reason_parts) or (
            f"no accepted episode among {len(sources)} file(s) inspected "
            f"({len(validation.skipped_not_requested)} not requested)"
        )
        await _block(
            session, download_repo, download_id, reason, request_id=request.id, season=season
        )
        return await download_repo.get_by_hash(torrent_hash)

    # Episode-SCOPED completeness gate. When the grab named specific episodes
    # (``episodes`` is a concrete list, not a whole-season ``None``), finalizing on a
    # PARTIAL set would place those files and mark the season completed/available
    # while a REQUESTED episode is still missing -- with no retry, a dishonest
    # "done". Require the union of accepted episodes to COVER every requested
    # episode; otherwise block honestly as a retryable ``ImportBlocked`` (never
    # place a partial set), so the operator can re-search for a release carrying the
    # rest. A whole-season grab (``episodes`` falsy) imposes no coverage requirement.
    if episodes:
        accepted_episodes = {ep for result in validation.accepted for ep in result.episodes}
        missing = sorted(set(episodes) - accepted_episodes)
        if missing:
            await _block(
                session,
                download_repo,
                download_id,
                f"episode-scoped grab is incomplete: requested {sorted(set(episodes))}, "
                f"missing {missing} (accepted {sorted(accepted_episodes)})",
                request_id=request.id,
                season=season,
            )
            return await download_repo.get_by_hash(torrent_hash)

    # Multiple accepted files can resolve to the SAME destination -- e.g. an
    # episode alongside its PROPER/REPACK, or a mixed-resolution pack shipping the
    # same episode twice (identical episode number(s) AND extension). Collapse to
    # the LARGEST file per destination BEFORE claiming/placing (mirrors the movie
    # path's largest-feature pick): without this, the second ``_place_file`` call
    # below would find a differently-sized file already at ``dst``, raise
    # ``FileExistsError``, and the rollback+block path a few lines down would
    # discard every OTHER already-placed, genuinely-distinct episode and block the
    # WHOLE season -- defeating "partial success is legitimate" over what is
    # really just a single duplicate. The smaller duplicate is dropped with a
    # logged warning (never silently), same posture as a validation rejection.
    abs_by_rel = {rel: abs_path for abs_path, _size, rel in sources}
    by_relative: dict[PurePosixPath, EpisodeImportResult] = {}
    for result in validation.accepted:
        src = abs_by_rel[result.video.relative_path]
        ext = os.path.splitext(src)[1].lstrip(".")
        relative = plex_tv_episode_relative_path(
            request.title, request.year, season, result.episodes, ext
        )
        current = by_relative.get(relative)
        if current is None or result.video.size_bytes > current.video.size_bytes:
            if current is not None:
                _logger.warning(
                    "tv import: dropping smaller duplicate %s for %s (kept %s)",
                    current.video.relative_path,
                    relative,
                    result.video.relative_path,
                )
            by_relative[relative] = result
        else:
            _logger.warning(
                "tv import: dropping smaller duplicate %s for %s (kept %s)",
                result.video.relative_path,
                relative,
                current.video.relative_path,
            )

    # Claim Importing BEFORE the (possibly long) copy -- same CAS discipline as
    # the movie path, conditional on the row still being resumable (an operator's
    # mark_failed during the validation gap above must not be overwritten).
    claimed = await download_repo.update_status_if_in(
        download_id, DownloadState.Importing.value, _RESUMABLE
    )
    if not claimed:
        await session.rollback()
        return await download_repo.get_by_hash(torrent_hash)

    season_dir = Path(tv_root) / plex_tv_season_relative_dir(request.title, request.year, season)
    session.add(
        DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=torrent_hash,
            event_type=DownloadHistoryEvent.import_started,
            source_title=None,  # never shadow the grabbed release title
            message=f"importing {len(by_relative)} episode(s) to {season_dir}",
        )
    )
    await session.commit()

    # ``imported`` history rows are staged (basename, relative) here rather than
    # written immediately: they are committed to ``session`` ONLY after the scan
    # AND the finalize CAS below have both succeeded (mirrors the movie path,
    # which likewise writes its ``imported`` row only once the scan succeeds). A
    # later file's copy failure OR a scan failure both roll placed files back via
    # ``_remove_quietly_many`` / ``_block`` — writing history eagerly here would let
    # the audit trail claim an episode was imported when it was in fact deleted
    # moments later (honesty over silence: history must never lie about a rollback).
    placed_paths: list[Path] = []
    imported: list[tuple[str, PurePosixPath]] = []
    for relative, result in by_relative.items():
        src = abs_by_rel[result.video.relative_path]
        dst = Path(tv_root) / relative
        try:
            placed = await asyncio.to_thread(_place_file, fs, src, dst)
        except (FileExistsError, OSError) as exc:
            await asyncio.to_thread(_remove_quietly_many, placed_paths)
            reason = (
                str(exc)
                if isinstance(exc, FileExistsError)
                else f"import copy failed: {type(exc).__name__}"
            )
            await _block(
                session, download_repo, download_id, reason, request_id=request.id, season=season
            )
            return await download_repo.get_by_hash(torrent_hash)
        if placed:
            placed_paths.append(dst)
        imported.append((os.path.basename(src), relative))

    # download_path is stamped with the SEASON folder (not one file) purely for
    # queue-display observability -- unlike the movie path, it is never consulted
    # to decide scan-failure rollback ownership (see the docstring above).
    await download_repo.update_status(
        download_id, DownloadState.Importing.value, download_path=str(season_dir)
    )
    await session.commit()

    # ONE targeted scan of the whole season directory, never one per episode.
    try:
        await library.trigger_scan(str(season_dir), "tv")
    except (PlexLibraryError, PlexAuthError) as exc:
        await asyncio.to_thread(_remove_quietly_many, placed_paths)
        await _block(
            session,
            download_repo,
            download_id,
            f"plex scan failed: {type(exc).__name__}",
            request_id=request.id,
            season=season,
            clear_download_path=True,
        )
        return await download_repo.get_by_hash(torrent_hash)

    # Imported. The season is 'completed' ("Finalizing") until a reconcile cycle
    # confirms availability via is_available (phase 2). Finalize with a
    # compare-and-swap, conditional on STILL holding the Importing claim.
    finalized = await download_repo.update_status_if_in(
        download_id,
        DownloadState.Imported.value,
        frozenset({DownloadState.Importing.value}),
        download_path=str(season_dir),
    )
    if not finalized:
        await session.rollback()
        return await download_repo.get_by_hash(torrent_hash)
    # Only now -- after the scan AND the finalize CAS have both succeeded -- is the
    # per-episode ``imported`` history durably recorded. See the staging comment
    # above the loop for why this cannot happen any earlier.
    for basename, relative in imported:
        session.add(
            DownloadHistory(
                tmdb_id=request.tmdb_id,
                torrent_hash=torrent_hash,
                event_type=DownloadHistoryEvent.imported,
                source_title=None,
                message=f"imported {basename} to {relative}",
            )
        )
    # Persist the eviction breadcrumb (ADR-0012) NOW, in the SAME transaction as
    # mark_completed -- ``season_dir`` is the season's own directory (the one
    # this call just scanned), the exact directory a later disk-pressure sweep's
    # ``fs.delete()`` must remove. Without this, ``SeasonRequest.library_path``
    # stays ``None`` forever and ``eviction_service._season_candidates`` has no
    # deletion target for this season.
    await season_request_service.set_library_path(
        session, media_request_id=request.id, season_number=season, library_path=str(season_dir)
    )
    await season_request_service.mark_completed(
        session, media_request_id=request.id, season_number=season
    )
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
    movies_root: str | None = None,
    tv_root: str | None = None,
) -> None:
    """Drain freshly-completed (and crash-stranded) imports. Needs the download
    client; the Movies/TV roots are each optional. One item failing never aborts
    the cycle.

    ``movies_root`` / ``tv_root`` are each optional: a row of that media type
    reaching ``import_download`` while its root is unset gets its own honest,
    retryable ``ImportBlocked`` (never a crash, never silently skipped) rather
    than gating the whole cycle — an install with only ONE root configured still
    drains that type normally.

    The completed -> available promotion is a SEPARATE pass
    (:func:`run_availability_cycle`) that needs only Plex, so it keeps working even
    when the download client is down or the Movies/TV root is unset.
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
                    tv_root=tv_root,
                )
            except Exception:
                await session.rollback()
                _logger.exception(
                    "import of download failed; will retry next cycle",
                    extra={"download_id": row.id},
                )


async def run_availability_cycle(*, library: LibraryPort, session: AsyncSession) -> None:
    """Confirm ``completed`` ("Finalizing") movies/seasons are indexed in Plex and
    promote them to ``available``. Depends ONLY on Plex — so an import that already
    triggered a scan still reaches ``available`` even if the download client or the
    Movies/TV root is unavailable afterward. One item failing never aborts the cycle.
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
                "availability check failed; will retry next cycle",
                extra={"tmdb_id": request.tmdb_id, "request_id": request.id},
            )

    # TV: per-SEASON confirmation, mirroring the movie loop above but scoped to
    # SeasonRequest rows -- a show's OTHER seasons may still be mid-flight while
    # one season is ready to confirm, so this is never gated on the parent's
    # (computed rollup) status.
    season_repo = SqlSeasonRequestRepository(session)
    for season_request in await season_repo.list_by_status(RequestStatus.completed.value):
        try:
            if await library.is_available(
                season_request.tmdb_id, "tv", season=season_request.season_number
            ):
                await season_request_service.mark_available(
                    session,
                    media_request_id=season_request.media_request_id,
                    season_number=season_request.season_number,
                )
                await session.commit()
        except (PlexLibraryError, PlexAuthError, NotImplementedError):
            await session.rollback()
            _logger.warning(
                "availability check failed for season %s; will retry next cycle",
                season_request.season_number,
                extra={
                    "tmdb_id": season_request.tmdb_id,
                    "request_id": season_request.media_request_id,
                },
            )
