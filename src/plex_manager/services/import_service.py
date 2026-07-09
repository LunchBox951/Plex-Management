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
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal, NamedTuple, cast

from sqlalchemy import update

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.adapters.qbittorrent import QbittorrentError
from plex_manager.domain.download_payload import (
    EMPTY_PAYLOAD_REJECTION_REASON,
    format_payload_rejection,
    validate_payload_files,
)
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
from plex_manager.domain.quality import Quality
from plex_manager.domain.source_mapping import resolve_quality
from plex_manager.domain.state_machine import DownloadState
from plex_manager.logsafe import safe_int, safe_text
from plex_manager.models import (
    BlocklistReason,
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    DownloadScope,
    RequestStatus,
)
from plex_manager.ports.filesystem import VIDEO_EXTENSIONS
from plex_manager.repositories.blocklist import SqlBlocklistRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import (
    blocklist_service,
    path_visibility,
    purge_service,
    queue_service,
    season_request_service,
)
from plex_manager.services.request_service import TERMINAL_REQUEST_STATUS_VALUES

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.quality_profile import QualityProfile
    from plex_manager.ports.download_client import DownloadClientPort, DownloadStatus
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.parser import ParserPort
    from plex_manager.ports.repositories import DownloadRecord, RequestRecord, SeasonRequestRecord

__all__ = [
    "PATH_NOT_VISIBLE_REASON_PREFIX",
    "import_download",
    "run_availability_cycle",
    "run_import_cycle",
]

_logger = logging.getLogger(__name__)

# The ``failed_reason`` prefix stamped by every "download path not visible inside the
# container" block (both the movie and TV paths, below) -- issues #133/#157.
# Exported so a caller (``correction_service``'s relocate verb) can recognise
# EXACTLY this block reason without re-deriving or loosely substring-matching a
# duplicated literal, and so the two call sites here can never drift apart.
PATH_NOT_VISIBLE_REASON_PREFIX: Final = "download path not visible inside the container "

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

_REARMABLE_REQUEST_STATUS_VALUES: frozenset[str] = (
    frozenset(s.value for s in RequestStatus) - TERMINAL_REQUEST_STATUS_VALUES
)

# Bounded Finalizing (issue #158): "Finalizing" ("completed", not yet
# "available") was previously an UNBOUNDED silent state -- a row whose Plex item
# carries no (or the wrong) tmdb GUID can never confirm via
# ``present_ids``/``season_presence`` alone, no matter how long the reconcile
# cycle waits, and nothing surfaced that. After this many minutes of a completed
# row confirming via NEITHER tmdb-GUID NOR path (``LibraryPort.confirm_paths``),
# a WARNING is logged into the durable, in-app console (any ``_logger.warning``
# call here already reaches ``log_events`` -- see
# ``services.log_capture_service``) naming the title. The row itself STAYS
# ``completed`` -- it genuinely is not confirmed, so that is the honest status;
# this is never a new status enum value, just a visible signal instead of a
# silent stall (north star: honesty over silence).
_FINALIZING_WARN_AFTER_MINUTES: Final = 30.0
# Re-warn at most this often per stuck row rather than every ~15s reconcile
# tick -- derived PURELY from elapsed-time bucketing (see
# ``_check_bounded_finalizing``), no new column or timer needed.
_FINALIZING_WARN_DUTY_CYCLE_MINUTES: Final = 60.0

# In-memory (never persisted -- no schema change) bookkeeping for the bounded-
# Finalizing warning's duty cycle, keyed by a caller-chosen row identity (see
# ``_movie_unconfirmed_key``/``_season_unconfirmed_key``). Maps to the elapsed-
# time BUCKET (see ``_check_bounded_finalizing``) a warning was already emitted
# for, so a sustained miss logs at most once per duty-cycle window rather than
# every tick. A restart simply re-arms the next-due warning -- an honest, safe
# direction: a genuine miss is merely re-measured, never permanently hidden by a
# lost counter. Entries are dropped the moment a row confirms/promotes, or is no
# longer in the CURRENT tick's completed set at all (``_forget_unconfirmed``,
# swept at the end of ``run_availability_cycle``), so this stays bounded to
# however many rows are ACTUALLY stuck completed right now.
_unconfirmed_warned_bucket: dict[str, int] = {}
# First-observed-miss timestamp -- the substitute anchor for "elapsed since
# completed" for any row with no PERSISTED completion stamp to anchor on. In
# practice this means every TV season (``SeasonRequest`` carries no per-season
# mirror of ``MediaRequest.completed_at``, deliberately deferred rather than
# added here without its own migration -- see ``SqlRequestRepository.
# heal_completed_at``'s docstring): a movie instead anchors on its real,
# persisted ``RequestRecord.completed_at`` and only falls through to this dict
# in the defensive (should-not-happen) case that stamp is somehow unset -- see
# ``_unconfirmed_anchor``.
_unconfirmed_since_fallback: dict[str, datetime] = {}


def _movie_unconfirmed_key(request_id: int) -> str:
    return f"movie:{request_id}"


def _season_unconfirmed_key(media_request_id: int, season_number: int) -> str:
    return f"season:{media_request_id}:{season_number}"


def reset_unconfirmed_tracking() -> None:
    """Clear the bounded-Finalizing (issue #158) in-memory bookkeeping.

    Test-isolation helper, not part of any port -- mirrors
    ``adapters.plex.library.reset_caches``. The dicts are process-global and
    keyed by request/season id, so a test suite whose fixtures hand out fresh
    (restarting) ids per test needs a way to wipe stale bookkeeping between
    tests without reaching into the private dicts directly.
    """
    _unconfirmed_warned_bucket.clear()
    _unconfirmed_since_fallback.clear()


def is_movie_unconfirmed_tracked(request_id: int) -> bool:
    """Whether the bounded-Finalizing bookkeeping still holds ANY entry (a
    warned duty-cycle bucket, or an in-memory first-seen anchor) for the movie
    ``request_id``. Test-only accessor -- lets a test assert
    ``_forget_unconfirmed`` actually cleared a row's state without reaching
    into the private dicts directly.
    """
    key = _movie_unconfirmed_key(request_id)
    return key in _unconfirmed_warned_bucket or key in _unconfirmed_since_fallback


def _forget_unconfirmed(key: str) -> None:
    """Drop every trace of ``key`` from the bounded-Finalizing bookkeeping.

    Called both when a row just confirmed/promoted (nothing left to warn about)
    and, at the end of each ``run_availability_cycle`` pass, for any previously-
    tracked key that is no longer in THIS tick's completed set at all -- an
    operator re-armed it, a report-issue/correction moved it off ``completed``
    some other way, or the row was deleted. Idempotent (``dict.pop`` with a
    default): safe to call on a key that was never tracked.
    """
    _unconfirmed_warned_bucket.pop(key, None)
    _unconfirmed_since_fallback.pop(key, None)


def _unconfirmed_anchor(key: str, persisted: datetime | None, *, now: datetime) -> datetime:
    """The instant elapsed-time for the bounded-Finalizing warning is measured
    from -- prefers ``persisted`` (a movie's real, restart-safe
    ``RequestRecord.completed_at``) when available; otherwise the first tick
    THIS PROCESS observed ``key`` unconfirmed (recorded in
    ``_unconfirmed_since_fallback`` -- see that dict's docstring).
    """
    if persisted is not None:
        return persisted
    existing = _unconfirmed_since_fallback.get(key)
    if existing is not None:
        return existing
    _unconfirmed_since_fallback[key] = now
    return now


def _check_bounded_finalizing(key: str, anchor: datetime, title: str, *, now: datetime) -> None:
    """Log a bounded, low-duty-cycle WARNING once ``key`` has failed both GUID and
    path confirmation for ``_FINALIZING_WARN_AFTER_MINUTES``. A no-op below that
    threshold, and a no-op again for the same duty-cycle window once already
    warned (see the module-level dicts' docstrings). The owning row is left
    ``completed`` by the caller -- this only makes an already-honest state
    VISIBLE, never changes it.
    """
    elapsed_minutes = (now - anchor).total_seconds() / 60.0
    if elapsed_minutes < _FINALIZING_WARN_AFTER_MINUTES:
        return
    bucket = int(
        (elapsed_minutes - _FINALIZING_WARN_AFTER_MINUTES) // _FINALIZING_WARN_DUTY_CYCLE_MINUTES
    )
    if _unconfirmed_warned_bucket.get(key) == bucket:
        return  # already warned for this duty-cycle window
    _unconfirmed_warned_bucket[key] = bucket
    _logger.warning(
        "%r imported but not confirmed by Plex — check the library match "
        "(unconfirmed for %.0f minute(s))",
        title,
        elapsed_minutes,
    )


class _UnsafeContentPathError(Exception):
    """The download client reported a content path outside its save path."""


class _TvImportTarget(NamedTuple):
    request: RequestRecord
    season: int
    episodes: list[int] | None
    scope_id: int | None


class _TvImportPlan(NamedTuple):
    target: _TvImportTarget
    season_dir: Path
    abs_by_rel: dict[str, str]
    by_relative: dict[PurePosixPath, EpisodeImportResult]
    accepted: tuple[EpisodeImportResult, ...]


class _TvImportFailure(NamedTuple):
    target: _TvImportTarget
    reason: str


def _is_settled_for_import(status: DownloadStatus) -> bool:
    return status.progress >= 1.0 and status.raw_state in _IMPORT_READY_RAW_STATES


def _lowest_profile_quality(
    results: tuple[EpisodeImportResult, ...],
    profile: QualityProfile,
) -> tuple[Quality, int | None]:
    pairs: list[tuple[Quality, int | None]] = []
    for result in results:
        quality = resolve_quality(
            result.parsed.source,
            result.parsed.resolution,
            result.parsed.modifier,
        )
        pairs.append((quality, profile.get_index(quality.id)))
    return min(pairs, key=lambda pair: pair[1] if pair[1] is not None else -1)


def _payload_manifest_is_complete(status: DownloadStatus) -> bool:
    return status.progress >= 1.0 or status.raw_state in _IMPORT_READY_RAW_STATES


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


class _ResolvedContent(NamedTuple):
    """A download's resolved content path plus the save-path to ANCHOR its remap.

    ``save_path`` is the torrent's live save directory when ``path`` came from the
    client (``content_path``, or ``save_path`` + ``name``) -- the anchor
    :func:`~plex_manager.services.path_visibility.remap_download_content` uses so a
    HOST->container remap of ``path`` never shortens the file below its download
    directory (which could match a stale, unrelated file). ``None`` when ``path`` is
    the stored crash-resume breadcrumb (no live save path): nothing to anchor to, so
    only the verbatim path is accepted, never a free suffix guess.
    """

    path: str
    save_path: str | None


def _resolve_content(
    status: DownloadStatus | None, download_path: str | None
) -> _ResolvedContent | None:
    """Resolve the absolute path to a torrent's completed content (file or dir).

    Prefer the client's ``content_path``; the adapter nulls it when it merely
    echoed ``save_path``, so fall back to the stored ``download_path`` and finally
    to ``save_path`` joined with the torrent name. ``save_path`` alone is never
    used — it can hold other torrents' files, which would scan the wrong tree.

    Returns the resolved content path PAIRED with the anchoring ``save_path`` (the
    torrent's live save directory) for the client-derived cases, or ``None`` for
    the ``save_path`` anchor when the path is the stored breadcrumb -- see
    :class:`_ResolvedContent`.
    """
    if status is not None and status.content_path:
        if status.save_path:
            return _ResolvedContent(
                _ensure_under_save_path(status.save_path, status.content_path), status.save_path
            )
        raise _UnsafeContentPathError("download client reported content path without save path")
    if status is not None and status.save_path and status.name:
        if os.path.isabs(status.name):
            raise _UnsafeContentPathError("download content path is outside download save path")
        return _ResolvedContent(
            _ensure_under_save_path(status.save_path, os.path.join(status.save_path, status.name)),
            status.save_path,
        )
    if download_path:
        return _ResolvedContent(download_path, None)
    return None


async def _resolve_visible_content(
    qbt: DownloadClientPort, torrent_hash: str, resolved: _ResolvedContent
) -> str | None:
    """Container-visible path for a download's resolved content, or ``None``.

    qBittorrent runs on the HOST, so ``resolved.path`` (from
    :func:`_resolve_content`) can be a HOST-namespace path this container cannot
    see (issue #133) -- e.g. ``/home/lunchbox/Downloads/.plex_manager/...`` when
    the real, mounted location is ``/downloads/...``. Returns the path unchanged
    when it already exists here (the same-namespace fast path -- no client call
    needed); else ANCHORS the remap on ``resolved.save_path`` and demands PROOF:
    the torrent's OWN file list is fetched from the client
    (:meth:`~plex_manager.ports.download_client.DownloadClientPort.list_files`,
    each entry a save-path-relative path + exact byte size) and the remapped
    candidate must exhibit that payload at the exact relative location with the
    exact size, under the DOWNLOAD mounts ONLY (never the library mounts). A
    same-named stale file with a different size is an immediate disproof, and no
    interpretation is ever accepted on bare existence -- an honest ``None``
    (retryable "not visible / content mismatch" block), never a guess. See
    :func:`~plex_manager.services.path_visibility.remap_download_content`. A
    client failure fetching the file list raises the adapter's typed error and is
    handled exactly like a ``get_status`` failure (retry next cycle / surfaced on
    the operator's manual retry). Stat probes offload via ``asyncio.to_thread``.
    """
    if await asyncio.to_thread(os.path.exists, resolved.path):
        return resolved.path
    if not resolved.save_path:
        # A stored crash-resume breadcrumb has no anchor to remap against: only
        # the verbatim path counts (a free suffix search would reintroduce the
        # stale-match hazard).
        return None
    files = await qbt.list_files(torrent_hash)
    expected = [(f.name, f.size_bytes) for f in files]
    return await asyncio.to_thread(
        path_visibility.remap_download_content, resolved.path, resolved.save_path, expected
    )


def _resolve_sources(fs: FileSystemPort, content_path: str) -> list[tuple[str, int, str]]:
    """Enumerate EVERY candidate video file under a download's content.

    Returns ``(abs_path, size, rel)`` for each eligible file. ``rel`` is anchored
    ABOVE the download root, so it includes the release FOLDER, not just the file.
    A torrent whose folder carries the title/quality
    (``The.Matrix.1999.1080p.WEB-DL/movie.mkv``) but ships a generic feature file
    would otherwise reach the validator as a token-less ``movie.mkv`` and be
    wrongly rejected as wrong/unknown media; anchoring the relative path above the
    download root keeps the folder tokens — and any ``CD1``/``Disc 1`` split-disk
    marker under it — in the string the validator parses.

    Shared by BOTH the movie and TV import paths (issue #69): the validator is
    designed to see EVERY candidate so it can drop sample/trailer/extra names
    itself and pick the largest surviving feature. Handing it a single
    pre-selected "largest" file (the old movie behaviour) let a larger
    featurette/proof/decoy beside the real feature be the only file the validator
    ever saw, wrongly blocking the download as ``NO_VIDEO_FILE`` / wrong-media even
    though the true feature was present. The movie path picks its one feature from
    the returned list via :func:`~plex_manager.domain.import_validation.validate_import`
    (largest non-sample survivor); the TV path validates every file independently
    via :func:`~plex_manager.domain.import_validation.validate_season_import` (a
    season pack legitimately ships many independently-valid episodes).
    """
    root_path = Path(content_path)
    if root_path.is_file():
        # A single-file torrent (no release folder): mirror ``largest_video_file``'s
        # is_file branch (adapters/filesystem/local.py) verbatim -- the lone file is
        # the only candidate, and its filename alone is sufficient (no folder to
        # anchor above). Same containment + extension guard as that branch: a
        # single-file grab is a single-file torrent, so a content root that is
        # ITSELF a symlink escaping its own parent directory (or that isn't even a
        # video file) must never be followed, or the importer would copy an
        # arbitrary out-of-tree file into the public library. An honest "no video
        # found" ([]) -> whole-download block, never a silent skip.
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
    # lexists, not exists: exists() follows a symlink and reads a DANGLING one as
    # absent, which would let hardlink_or_copy's rename fallback silently replace
    # the symlink entry (GHSA-8fj8) instead of surfacing the conflict below.
    if os.path.lexists(os.fspath(dst)):
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
        # lexists check above and this link. Same content (same size) is an
        # idempotent win for the other attempt, NOT a failure to block on; a
        # different size is a genuine conflict, surfaced like the pre-existing case.
        if os.path.lexists(os.fspath(dst)) and _same_file_content(src, dst):
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
    try:
        dst_size = dst.stat().st_size
    except OSError:
        # A dangling symlink (or dst vanished between the lexists check and here)
        # is NOT our identical file -- never raise FileNotFoundError out of a
        # content check; the caller surfaces this as an honest conflict instead.
        return False
    if dst_size != os.path.getsize(src):
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


async def _set_download_scope_status(
    session: AsyncSession,
    *,
    download_id: int,
    status: str,
    request_id: int | None = None,
    season: int | None = None,
    scope_id: int | None = None,
    completed: bool = False,
) -> None:
    stmt = update(DownloadScope).where(DownloadScope.download_id == download_id)
    if scope_id is not None:
        stmt = stmt.where(DownloadScope.id == scope_id)
    else:
        if request_id is not None:
            stmt = stmt.where(DownloadScope.media_request_id == request_id)
        if season is not None:
            stmt = stmt.where(DownloadScope.season_number == season)
    values: dict[str, object] = {"status": status}
    if completed:
        values["completed_at"] = datetime.now(UTC)
    await session.execute(stmt.values(**values))


async def _block(
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    download_id: int,
    reason: str,
    *,
    request_id: int | None = None,
    season: int | None = None,
    seasons: tuple[int, ...] = (),
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
        target_seasons = seasons or ((season,) if season is not None else ())
        if target_seasons:
            for target_season in target_seasons:
                await _set_download_scope_status(
                    session,
                    download_id=download_id,
                    request_id=request_id,
                    season=target_season,
                    status=RequestStatus.import_blocked.value,
                )
                await season_request_service.set_status(
                    session,
                    media_request_id=request_id,
                    season_number=target_season,
                    status=RequestStatus.import_blocked.value,
                )
        else:
            await SqlRequestRepository(session).set_status(
                request_id, RequestStatus.import_blocked.value
            )
    await session.commit()


def _tv_block_reason(validation_count: int, failures: tuple[str, ...]) -> str:
    reason_parts = failures[:_MAX_BLOCK_REASONS]
    if len(failures) > _MAX_BLOCK_REASONS:
        reason_parts = (*reason_parts, f"(+{len(failures) - _MAX_BLOCK_REASONS} more)")
    return (
        "; ".join(reason_parts) or f"no accepted episode among {validation_count} file(s) inspected"
    )


def _build_tv_import_plan(
    *,
    target: _TvImportTarget,
    sources: list[tuple[str, int, str]],
    parser: ParserPort,
    profile: QualityProfile,
    tv_root: str,
) -> _TvImportPlan | _TvImportFailure:
    validation = validate_season_import(
        [VideoFile(relative_path=rel, size_bytes=size) for _abs, size, rel in sources],
        parser=parser,
        profile=profile,
        expected_title=target.request.title,
        expected_tmdb_id=target.request.tmdb_id,
        expected_season=target.season,
        requested_episodes=target.episodes,
    )
    for rejection in validation.rejected:
        _logger.warning(
            "tv import: rejected %s for season %s (%s): %s",
            rejection.relative_path,
            safe_int(target.season),
            rejection.reason.value,
            rejection.detail,
        )

    if not validation.accepted:
        reason_parts = tuple(
            f"{r.relative_path}: {r.reason.value}: {r.detail}" for r in validation.rejected
        )
        reason = _tv_block_reason(len(sources), reason_parts) or (
            f"no accepted episode among {len(sources)} file(s) inspected "
            f"({len(validation.skipped_not_requested)} not requested)"
        )
        return _TvImportFailure(target, reason)

    if target.episodes:
        accepted_episodes = {ep for result in validation.accepted for ep in result.episodes}
        missing = sorted(set(target.episodes) - accepted_episodes)
        if missing:
            return _TvImportFailure(
                target,
                f"episode-scoped grab is incomplete: requested {sorted(set(target.episodes))}, "
                f"missing {missing} (accepted {sorted(accepted_episodes)})",
            )

    abs_by_rel = {rel: abs_path for abs_path, _size, rel in sources}
    by_relative: dict[PurePosixPath, EpisodeImportResult] = {}
    for result in validation.accepted:
        src = abs_by_rel[result.video.relative_path]
        ext = os.path.splitext(src)[1].lstrip(".")
        relative = plex_tv_episode_relative_path(
            target.request.title, target.request.year, target.season, result.episodes, ext
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

    season_dir = Path(tv_root) / plex_tv_season_relative_dir(
        target.request.title, target.request.year, target.season
    )
    return _TvImportPlan(target, season_dir, abs_by_rel, by_relative, validation.accepted)


async def _fail_unsafe_payload(
    *,
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    qbt: DownloadClientPort,
    download_id: int,
    torrent_hash: str,
    reason: str,
    request_id: int,
    season: int | None = None,
    owned_placement: Path | None = None,
) -> DownloadRecord | None:
    queue_service._reconcile_removals_in_flight.add(download_id)  # pyright: ignore[reportPrivateUsage]
    try:
        if queue_service._is_operator_claimed(download_id):  # pyright: ignore[reportPrivateUsage]
            return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
        failed_pending = await download_repo.update_status_if_in(
            download_id,
            DownloadState.FailedPending.value,
            _RESUMABLE,
            failed_reason=reason,
        )
        if not failed_pending:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
        await session.commit()

        await session.rollback()
        latest = await session.get(Download, download_id, populate_existing=True)
        latest_status = latest.status if latest is not None else None
        latest_reason = latest.failed_reason if latest is not None else None
        await session.rollback()
        if latest_status != DownloadState.FailedPending.value or latest_reason != reason:
            return await download_repo.get_by_hash(torrent_hash, populate_existing=True)

        if owned_placement is not None:
            cleared = await download_repo.update_status_if_in(
                download_id,
                DownloadState.FailedPending.value,
                frozenset({DownloadState.FailedPending.value}),
                clear_download_path=True,
                require_failed_reason=reason,
            )
            if cleared:
                await session.commit()
            else:
                await session.rollback()
                return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
            await asyncio.to_thread(_remove_quietly, owned_placement)

        await purge_service.remove_torrent(
            qbt,
            torrent_hash,
            context="an unsafe torrent payload rejection",
            extra={
                "torrent_hash": safe_text(torrent_hash),
                "download_id": safe_int(download_id),
                "request_id": safe_int(request_id),
            },
        )

        request_repo = SqlRequestRepository(session)
        request = await request_repo.get(request_id)
        source_title = (
            await blocklist_service.source_title_for(session, torrent_hash) or torrent_hash
        )
        indexer = await blocklist_service.indexer_for(session, torrent_hash)
        completed = await download_repo.update_status_if_in(
            download_id,
            DownloadState.Failed.value,
            frozenset({DownloadState.FailedPending.value}),
            failed_reason=reason,
            require_failed_reason=reason,
        )
        if not completed:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash, populate_existing=True)

        await SqlBlocklistRepository(session).create(
            source_title=source_title,
            reason=BlocklistReason.failed.value,
            tmdb_id=request.tmdb_id if request is not None else None,
            torrent_hash=torrent_hash,
            indexer=indexer,
            media_type=(
                request.media_type
                if request is not None
                else ("tv" if season is not None else "movie")
            ),
        )
        if request is not None:
            if season is not None:
                season_repo = SqlSeasonRequestRepository(session)
                row = await season_repo.ensure(
                    request_id, season, status=RequestStatus.pending.value
                )
                await season_request_service.set_status_if_in(
                    session,
                    media_request_id=request_id,
                    season_request_id=row.id,
                    status=RequestStatus.searching.value,
                    allowed_from=_REARMABLE_REQUEST_STATUS_VALUES,
                )
            else:
                await request_repo.set_status_if_in(
                    request_id,
                    RequestStatus.searching.value,
                    _REARMABLE_REQUEST_STATUS_VALUES,
                )
        await session.commit()
        return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
    finally:
        queue_service._reconcile_removals_in_flight.discard(download_id)  # pyright: ignore[reportPrivateUsage]


def _owned_movie_breadcrumb_for_unsafe_rollback(
    status: str, download_path: str | None, movies_root: str
) -> Path | None:
    if status != DownloadState.Importing.value or download_path is None:
        return None
    path = Path(download_path)
    root_real = os.path.realpath(movies_root)
    path_real = os.path.realpath(path)
    if path.suffix.lower() not in VIDEO_EXTENSIONS or not _is_within(root_real, path_real):
        return None
    return path


async def _reject_unsafe_payload_if_reported(
    *,
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    qbt: DownloadClientPort,
    download_id: int,
    torrent_hash: str,
    status: DownloadStatus | None,
    request_id: int,
    season: int | None = None,
    owned_placement: Path | None = None,
    block_existing_breadcrumb: bool = False,
) -> DownloadRecord | None:
    if status is None:
        return None
    complete = _payload_manifest_is_complete(status)
    if status.progress <= 0 and not complete:
        return None

    try:
        files = await qbt.list_files(torrent_hash)
    except QbittorrentError as exc:
        if complete:
            await _block(
                session,
                download_repo,
                download_id,
                f"could not validate torrent payload manifest: {type(exc).__name__}",
                request_id=request_id,
                season=season,
            )
            return await download_repo.get_by_hash(torrent_hash)
        return None
    reason: str | None = None
    if not files:
        if complete:
            reason = EMPTY_PAYLOAD_REJECTION_REASON
    else:
        validation = validate_payload_files(files)
        if not validation.accepted:
            reason = format_payload_rejection(validation)

    if reason is None:
        return None

    if block_existing_breadcrumb:
        await _block(
            session,
            download_repo,
            download_id,
            f"{reason}; stored import breadcrumb requires manual cleanup before re-search",
            request_id=request_id,
            season=season,
        )
        return await download_repo.get_by_hash(torrent_hash)

    return await _fail_unsafe_payload(
        session=session,
        download_repo=download_repo,
        qbt=qbt,
        download_id=download_id,
        torrent_hash=torrent_hash,
        reason=reason,
        request_id=request_id,
        season=season,
        owned_placement=owned_placement,
    )


async def _resume_breadcrumbed_movie_import(
    *,
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    request_repo: SqlRequestRepository,
    library: LibraryPort,
    download_id: int,
    torrent_hash: str,
    request: RequestRecord,
    movies_root: str,
    download_path: str,
) -> DownloadRecord | None:
    dst = Path(download_path)
    root_real = os.path.realpath(movies_root)
    dst_real = os.path.realpath(dst)
    if dst.suffix.lower() not in VIDEO_EXTENSIONS or not _is_within(root_real, dst_real):
        await _block(
            session,
            download_repo,
            download_id,
            "stored import breadcrumb is outside the movie library root",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)
    if not await asyncio.to_thread(os.path.exists, dst):
        await _block(
            session,
            download_repo,
            download_id,
            "stored import breadcrumb is not visible inside the container",
            request_id=request.id,
            clear_download_path=True,
        )
        return await download_repo.get_by_hash(torrent_hash)

    try:
        await library.trigger_scan(str(dst.parent), "movie")
    except (PlexLibraryError, PlexAuthError) as exc:
        await asyncio.to_thread(_remove_quietly, dst)
        await _block(
            session,
            download_repo,
            download_id,
            f"plex scan failed: {type(exc).__name__}",
            request_id=request.id,
            clear_download_path=True,
        )
        return await download_repo.get_by_hash(torrent_hash)

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
    relative = os.path.relpath(dst, movies_root)
    session.add(
        DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=torrent_hash,
            event_type=DownloadHistoryEvent.imported,
            source_title=None,
            message=f"imported {dst.name} to {relative}",
        )
    )
    await request_repo.set_library_path(request.id, str(dst.parent))
    await request_repo.mark_completed(request.id)
    await session.commit()
    return await download_repo.get_by_hash(torrent_hash)


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
    anime_movie_root: str | None = None,
    anime_tv_root: str | None = None,
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

    ``anime_movie_root``/``anime_tv_root`` (ADR-0015) are likewise optional: when
    the owning request is ``is_anime`` AND the matching anime root is configured,
    the anime root is used INSTEAD of ``movies_root``/``tv_root``; otherwise
    anime content falls back to the normal root exactly as before this feature
    existed (Overseerr's optional-override-else-default shape).

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
            anime_movie_root=anime_movie_root,
            anime_tv_root=anime_tv_root,
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
    anime_movie_root: str | None = None,
    anime_tv_root: str | None = None,
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
        # ADR-0015: an anime episode routes to anime_tv_root when the request is
        # is_anime AND that root is configured; otherwise it falls back to the
        # normal tv_root exactly as before this feature existed.
        effective_tv_root = anime_tv_root if request.is_anime and anime_tv_root else tv_root
        scope_records = [
            scope
            for scope in await download_repo.list_scopes(download_id)
            if scope.media_request_id == request.id
            and scope.season is not None
            and scope.status != "imported"
        ]
        scope_targets = tuple(
            _TvImportTarget(
                request=request,
                season=cast(int, scope.season),
                episodes=scope.episodes,
                scope_id=scope.id,
            )
            for scope in scope_records
        )
        if effective_tv_root is None:
            target_seasons = tuple(dict.fromkeys(scope.season for scope in scope_targets))
            await _block(
                session,
                download_repo,
                download_id,
                "tv library root is not configured",
                request_id=request.id,
                season=season if not target_seasons else None,
                seasons=target_seasons,
            )
            return await download_repo.get_by_hash(torrent_hash)
        if scope_targets:
            return await _import_tv_targets_locked(
                download_id=download_id,
                targets=scope_targets,
                download_path=row.download_path,
                fs=fs,
                library=library,
                qbt=qbt,
                parser=parser,
                profile=profile,
                session=session,
                tv_root=effective_tv_root,
                torrent_hash=torrent_hash,
            )
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
            download_status=row.status,
            download_path=row.download_path,
            fs=fs,
            library=library,
            qbt=qbt,
            parser=parser,
            profile=profile,
            session=session,
            tv_root=effective_tv_root,
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

    # ADR-0015: an anime movie routes to anime_movie_root when the request is
    # is_anime AND that root is configured; otherwise it falls back to the
    # normal movies_root exactly as before this feature existed.
    effective_movies_root = (
        anime_movie_root if request.is_anime and anime_movie_root else movies_root
    )
    if effective_movies_root is None:
        # Mirrors the tv branch's ``effective_tv_root is None`` guard above: an
        # honest, retryable block rather than gating this whole cycle on
        # movies_root being set (an install with only the TV root configured
        # must still import TV; an anime-only install with only anime_movie_root
        # configured must still import anime movies).
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
    download_path = row.download_path
    if status is None:
        if row.status == DownloadState.Importing.value and download_path is not None:
            return await _resume_breadcrumbed_movie_import(
                session=session,
                download_repo=download_repo,
                request_repo=request_repo,
                library=library,
                download_id=download_id,
                torrent_hash=torrent_hash,
                request=request,
                movies_root=effective_movies_root,
                download_path=download_path,
            )
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no status for payload validation",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)
    if _payload_manifest_is_complete(status):
        rejected = await _reject_unsafe_payload_if_reported(
            session=session,
            download_repo=download_repo,
            qbt=qbt,
            download_id=download_id,
            torrent_hash=torrent_hash,
            status=status,
            request_id=request.id,
            owned_placement=_owned_movie_breadcrumb_for_unsafe_rollback(
                row.status, row.download_path, effective_movies_root
            ),
        )
        if rejected is not None:
            return rejected
    if not _is_settled_for_import(status):
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
        resolved = _resolve_content(status, row.download_path)
    except _UnsafeContentPathError as exc:
        await _block(session, download_repo, download_id, str(exc), request_id=request.id)
        return await download_repo.get_by_hash(torrent_hash)
    if resolved is None:
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no content path",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)
    visible_content = await _resolve_visible_content(qbt, torrent_hash, resolved)
    if visible_content is None:
        # qBittorrent runs on the host: an honest, retryable block instead of the
        # misleading "no video file found" the empty _resolve_sources scan below
        # would otherwise produce for a tree this container simply cannot see.
        await _block(
            session,
            download_repo,
            download_id,
            PATH_NOT_VISIBLE_REASON_PREFIX
            + f"(check volume mounts / content mismatch): {resolved.path}",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)
    content = visible_content
    sources = await asyncio.to_thread(_resolve_sources, fs, content)
    if not sources:
        await _block(
            session,
            download_repo,
            download_id,
            "no video file found in the download",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)

    # Validate the download IS the requested movie at acceptable quality (same brain
    # as search), gating on profile-allowed (not equal-to-grab) so benign source
    # drift imports while CAM/TS/sample is rejected — the prototype's defining-bug
    # fix. Hand the validator EVERY candidate (issue #69), not one pre-selected
    # "largest": it drops sample/trailer/extra-named files itself and picks the
    # largest surviving feature, so a larger featurette/proof/decoy beside the real
    # feature no longer blinds it into a false ``NO_VIDEO_FILE`` / wrong-media block.
    validation = validate_import(
        [VideoFile(relative_path=rel, size_bytes=size) for _abs, size, rel in sources],
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

    # Map the validator's chosen feature back to its absolute source path — exactly
    # the ``abs_by_rel`` shape the TV path uses — so placement copies the file the
    # validator actually selected, not a separately re-derived "largest". An
    # ``accepted`` validation ALWAYS carries a chosen ``video`` (accepted is defined
    # as "no rejections", reachable only after a feature was picked), so the None
    # guard is unreachable defence-in-depth for the type checker.
    if validation.video is None:  # pragma: no cover - accepted implies a chosen feature
        await _block(
            session,
            download_repo,
            download_id,
            "no video file found in the download",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)
    abs_by_rel = {rel: abs_path for abs_path, _size, rel in sources}
    src = abs_by_rel[validation.video.relative_path]
    ext = os.path.splitext(src)[1].lstrip(".")
    relative = plex_movie_relative_path(request.title, request.year, ext)
    dst = Path(effective_movies_root) / relative

    if not await asyncio.to_thread(os.path.isdir, effective_movies_root):
        # A configured-but-invisible-in-this-container root (issue #132): never
        # os.makedirs a phantom in-container tree (_place_file's mkdir below is
        # unconditional) -- an honest, retryable block instead. Settings' write-time
        # gate remaps a host-shaped root at save time; this catches a legacy stored
        # root that predates that gate, and the operator's retry re-resolves fresh.
        # Runs BEFORE begin_placement so a block here never registers a placement.
        await _block(
            session,
            download_repo,
            download_id,
            f"library root not visible inside the container: {effective_movies_root}",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)

    # PURGE-vs-IMPORT ordering rule (round 9; stated identically at
    # purge_service's registry): FIRST-REGISTERED WINS, loser defers fast. If an
    # eviction/correction purge is mid-delete on this movie's directory, SKIP
    # this attempt -- the row stays ImportPending and the next import cycle
    # retries honestly -- rather than placing files into a tree an rmtree is
    # walking. Registered here (before the Importing claim) and released only
    # after the finalize commit, so a purge arriving anywhere in that window
    # defers instead of deleting freshly placed-and-committed files.
    if not purge_service.begin_placement(str(dst)):
        _logger.info(
            "deferring import of download %s: a purge is deleting this path; "
            "will retry next import cycle",
            safe_int(download_id),
            extra={"request_id": safe_int(request.id)},
        )
        return await download_repo.get_by_hash(torrent_hash)
    try:
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

        # Targeted Plex scan of the movie folder — the partial scan the prototype never
        # did. movies_root is a Plex library location (the picker guarantees the path↔
        # section match), so a scan failure here is a transient Plex error, not a wrong
        # path. Roll the file back before blocking so a later reject / re-search can't
        # orphan it (the retry re-places it).
        #
        # OWNERSHIP RULE (Codex PR #21): a file at dst may be rolled back ONLY on proof
        # it is ours — THIS invocation placed it (``placed``), or a prior attempt of
        # this row durably recorded placing it (the ``download_path == dst`` breadcrumb
        # committed above / by the finalize). NEVER by content-match alone: a
        # same-content file that we did NOT place (a lost placement race, a
        # user's manually-supplied copy, a prior retry's winner) is byte-for-byte
        # indistinguishable from our own crashed-before-breadcrumb placement, so an
        # inference like "resumed Importing + no breadcrumb -> the orphan is ours"
        # deletes an unowned library file on a transient scan failure. The honest cost
        # of refusing to guess: a genuinely-ours orphan from a crash INSIDE the
        # place→breadcrumb window is not rolled back on a repeat scan failure — it
        # stays on disk, is re-adopted by the next successful retry (idempotent
        # placement + the finalize stamps ``download_path``), and at worst surfaces
        # later as an explicit FileExistsError conflict for the operator; deleting
        # nothing beats maybe-deleting someone else's file. Clear the breadcrumb on
        # rollback so the deleted path can't shadow the torrent's content on a later
        # retry.
        try:
            await library.trigger_scan(str(dst.parent), "movie")
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
    finally:
        purge_service.end_placement(str(dst))


def _failure_summary(failures: list[_TvImportFailure]) -> str:
    if len(failures) == 1:
        return failures[0].reason
    parts = [f"S{failure.target.season:02d}: {failure.reason}" for failure in failures]
    return _tv_block_reason(len(parts), tuple(parts))


async def _mark_tv_scope_blocked(
    session: AsyncSession,
    *,
    download_id: int,
    failure: _TvImportFailure,
) -> None:
    await _set_download_scope_status(
        session,
        download_id=download_id,
        scope_id=failure.target.scope_id,
        request_id=failure.target.request.id,
        season=failure.target.season,
        status=RequestStatus.import_blocked.value,
    )
    await season_request_service.set_status(
        session,
        media_request_id=failure.target.request.id,
        season_number=failure.target.season,
        status=RequestStatus.import_blocked.value,
    )


async def _import_tv_targets_locked(
    *,
    download_id: int,
    targets: tuple[_TvImportTarget, ...],
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
    download_repo = SqlDownloadRepository(session)
    request = targets[0].request
    target_seasons = tuple(dict.fromkeys(target.season for target in targets))

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
        for target in targets:
            await _set_download_scope_status(
                session,
                download_id=download_id,
                scope_id=target.scope_id,
                request_id=target.request.id,
                season=target.season,
                status="active",
            )
            await season_request_service.set_status(
                session,
                media_request_id=target.request.id,
                season_number=target.season,
                status=RequestStatus.downloading.value,
            )
        await session.commit()
        return await download_repo.get_by_hash(torrent_hash)

    try:
        resolved = _resolve_content(status, download_path)
    except _UnsafeContentPathError as exc:
        await _block(
            session,
            download_repo,
            download_id,
            str(exc),
            request_id=request.id,
            seasons=target_seasons,
        )
        return await download_repo.get_by_hash(torrent_hash)
    if resolved is None:
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no content path",
            request_id=request.id,
            seasons=target_seasons,
        )
        return await download_repo.get_by_hash(torrent_hash)
    visible_content = await _resolve_visible_content(qbt, torrent_hash, resolved)
    if visible_content is None:
        await _block(
            session,
            download_repo,
            download_id,
            PATH_NOT_VISIBLE_REASON_PREFIX
            + f"(check volume mounts / content mismatch): {resolved.path}",
            request_id=request.id,
            seasons=target_seasons,
        )
        return await download_repo.get_by_hash(torrent_hash)

    sources = await asyncio.to_thread(_resolve_sources, fs, visible_content)
    if not sources:
        await _block(
            session,
            download_repo,
            download_id,
            "no video file found in the download",
            request_id=request.id,
            seasons=target_seasons,
        )
        return await download_repo.get_by_hash(torrent_hash)

    if not await asyncio.to_thread(os.path.isdir, tv_root):
        await _block(
            session,
            download_repo,
            download_id,
            f"library root not visible inside the container: {tv_root}",
            request_id=request.id,
            seasons=target_seasons,
        )
        return await download_repo.get_by_hash(torrent_hash)

    plans: list[_TvImportPlan] = []
    failures: list[_TvImportFailure] = []
    for target in targets:
        planned = _build_tv_import_plan(
            target=target,
            sources=sources,
            parser=parser,
            profile=profile,
            tv_root=tv_root,
        )
        if isinstance(planned, _TvImportFailure):
            failures.append(planned)
        else:
            plans.append(planned)

    if not plans:
        await _block(
            session,
            download_repo,
            download_id,
            _failure_summary(failures),
            request_id=request.id,
            seasons=target_seasons,
        )
        return await download_repo.get_by_hash(torrent_hash)

    claimed_paths: list[str] = []
    for plan in plans:
        if not purge_service.begin_placement(str(plan.season_dir)):
            for claimed in claimed_paths:
                purge_service.end_placement(claimed)
            _logger.info(
                "deferring import of download %s: a purge is deleting this path; "
                "will retry next import cycle",
                safe_int(download_id),
                extra={
                    "request_id": safe_int(plan.target.request.id),
                    "season": safe_int(plan.target.season),
                },
            )
            return await download_repo.get_by_hash(torrent_hash)
        claimed_paths.append(str(plan.season_dir))

    try:
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
                source_title=None,
                message=f"importing {len(plans)} tv scope(s)",
            )
        )
        await session.commit()

        successful_dirs: list[Path] = []
        for plan in plans:
            placed_paths: list[Path] = []
            imported: list[tuple[str, PurePosixPath]] = []
            for relative, result in plan.by_relative.items():
                src = plan.abs_by_rel[result.video.relative_path]
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
                    failures.append(_TvImportFailure(plan.target, reason))
                    break
                if placed:
                    placed_paths.append(dst)
                imported.append((os.path.basename(src), relative))
            else:
                await download_repo.update_status(
                    download_id,
                    DownloadState.Importing.value,
                    download_path=str(plan.season_dir),
                )
                await session.commit()
                try:
                    await library.trigger_scan(str(plan.season_dir), "tv")
                except (PlexLibraryError, PlexAuthError) as exc:
                    await asyncio.to_thread(_remove_quietly_many, placed_paths)
                    failures.append(
                        _TvImportFailure(plan.target, f"plex scan failed: {type(exc).__name__}")
                    )
                    continue

                for basename, relative in imported:
                    session.add(
                        DownloadHistory(
                            tmdb_id=plan.target.request.tmdb_id,
                            torrent_hash=torrent_hash,
                            event_type=DownloadHistoryEvent.imported,
                            source_title=None,
                            message=f"imported {basename} to {relative}",
                        )
                    )
                await season_request_service.set_library_path(
                    session,
                    media_request_id=plan.target.request.id,
                    season_number=plan.target.season,
                    library_path=str(plan.season_dir),
                )
                installed_quality, installed_profile_index = _lowest_profile_quality(
                    plan.accepted, profile
                )
                await season_request_service.set_installed_quality(
                    session,
                    media_request_id=plan.target.request.id,
                    season_number=plan.target.season,
                    quality_id=installed_quality.id,
                    profile_index=installed_profile_index,
                )
                await season_request_service.mark_completed(
                    session,
                    media_request_id=plan.target.request.id,
                    season_number=plan.target.season,
                )
                await _set_download_scope_status(
                    session,
                    download_id=download_id,
                    scope_id=plan.target.scope_id,
                    request_id=plan.target.request.id,
                    season=plan.target.season,
                    status="imported",
                    completed=True,
                )
                successful_dirs.append(plan.season_dir)

        if not successful_dirs:
            await _block(
                session,
                download_repo,
                download_id,
                _failure_summary(failures),
                request_id=request.id,
                seasons=target_seasons,
                clear_download_path=True,
            )
            return await download_repo.get_by_hash(torrent_hash)

        for failure in failures:
            await _mark_tv_scope_blocked(session, download_id=download_id, failure=failure)
        if failures:
            await download_repo.align_scalar_scope_with_active(download_id)
        finalized = await download_repo.update_status_if_in(
            download_id,
            DownloadState.ImportBlocked.value if failures else DownloadState.Imported.value,
            frozenset({DownloadState.Importing.value}),
            download_path=str(successful_dirs[-1]),
            failed_reason=_failure_summary(failures) if failures else None,
            clear_failed_reason=not failures,
        )
        if not finalized:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash)
        await session.commit()
        return await download_repo.get_by_hash(torrent_hash)
    finally:
        for claimed in claimed_paths:
            purge_service.end_placement(claimed)


async def _import_tv_locked(
    *,
    download_id: int,
    request: RequestRecord,
    season: int,
    episodes: list[int] | None,
    download_status: str,
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
    resume_from_breadcrumb = (
        status is None
        and download_path is not None
        and download_status == DownloadState.Importing.value
    )
    if status is None and not resume_from_breadcrumb:
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no status for payload validation",
            request_id=request.id,
            season=season,
        )
        return await download_repo.get_by_hash(torrent_hash)
    if status is not None and _payload_manifest_is_complete(status):
        rejected = await _reject_unsafe_payload_if_reported(
            session=session,
            download_repo=download_repo,
            qbt=qbt,
            download_id=download_id,
            torrent_hash=torrent_hash,
            status=status,
            request_id=request.id,
            season=season,
            block_existing_breadcrumb=(
                download_status == DownloadState.Importing.value and download_path is not None
            ),
        )
        if rejected is not None:
            return rejected
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
        resolved = _resolve_content(status, download_path)
    except _UnsafeContentPathError as exc:
        await _block(
            session, download_repo, download_id, str(exc), request_id=request.id, season=season
        )
        return await download_repo.get_by_hash(torrent_hash)
    if resolved is None:
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no content path",
            request_id=request.id,
            season=season,
        )
        return await download_repo.get_by_hash(torrent_hash)
    visible_content = await _resolve_visible_content(qbt, torrent_hash, resolved)
    if visible_content is None:
        await _block(
            session,
            download_repo,
            download_id,
            PATH_NOT_VISIBLE_REASON_PREFIX
            + f"(check volume mounts / content mismatch): {resolved.path}",
            request_id=request.id,
            season=season,
        )
        return await download_repo.get_by_hash(torrent_hash)
    content = visible_content

    sources = await asyncio.to_thread(_resolve_sources, fs, content)
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

    if not await asyncio.to_thread(os.path.isdir, tv_root):
        # Mirrors the movie path's root-visibility guard: never os.makedirs a
        # phantom in-container season tree -- an honest, retryable block instead.
        # Runs BEFORE begin_placement so a block here never registers a placement.
        await _block(
            session,
            download_repo,
            download_id,
            f"library root not visible inside the container: {tv_root}",
            request_id=request.id,
            season=season,
        )
        return await download_repo.get_by_hash(torrent_hash)

    season_dir = Path(tv_root) / plex_tv_season_relative_dir(request.title, request.year, season)
    # PURGE-vs-IMPORT ordering rule (round 9; stated identically at
    # purge_service's registry): FIRST-REGISTERED WINS, loser defers fast. If an
    # eviction/correction purge is mid-delete on this season directory, SKIP
    # this attempt -- the row stays ImportPending and the next import cycle
    # retries honestly -- rather than placing episodes into a tree an rmtree is
    # walking. Registered here (before the Importing claim) and released only
    # after the finalize commit, so a purge arriving anywhere in that window
    # defers instead of deleting freshly placed-and-committed files.
    if not purge_service.begin_placement(str(season_dir)):
        _logger.info(
            "deferring import of download %s: a purge is deleting this path; "
            "will retry next import cycle",
            safe_int(download_id),
            extra={"request_id": safe_int(request.id), "season": safe_int(season)},
        )
        return await download_repo.get_by_hash(torrent_hash)
    try:
        # Claim Importing BEFORE the (possibly long) copy -- same CAS discipline as
        # the movie path, conditional on the row still being resumable (an operator's
        # mark_failed during the validation gap above must not be overwritten).
        claimed = await download_repo.update_status_if_in(
            download_id,
            DownloadState.Importing.value,
            _RESUMABLE,
            # Clear any prior block reason on a clean retry (issue #73), mirroring the
            # movie path: a row re-entering import from ``ImportBlocked`` must not carry
            # its stale ``failed_reason`` forward into a successful import, or the queue
            # and audit trail would keep showing a block that no longer applies.
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
                    session,
                    download_repo,
                    download_id,
                    reason,
                    request_id=request.id,
                    season=season,
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
            # Clear any prior block reason on finalize (issue #73), mirroring the movie
            # path: a TV row that reached ``Imported`` via a retry must land with a null
            # ``failed_reason`` so a successfully-imported season never displays a stale
            # import-block reason (honesty over silence - the state and the reason agree).
            clear_failed_reason=True,
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
        installed_quality, installed_profile_index = _lowest_profile_quality(
            validation.accepted, profile
        )
        await season_request_service.set_installed_quality(
            session,
            media_request_id=request.id,
            season_number=season,
            quality_id=installed_quality.id,
            profile_index=installed_profile_index,
        )
        await season_request_service.mark_completed(
            session, media_request_id=request.id, season_number=season
        )
        await session.commit()
        return await download_repo.get_by_hash(torrent_hash)
    finally:
        purge_service.end_placement(str(season_dir))


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
    anime_movie_root: str | None = None,
    anime_tv_root: str | None = None,
) -> None:
    """Drain freshly-completed (and crash-stranded) imports. Needs the download
    client; the Movies/TV roots are each optional. One item failing never aborts
    the cycle.

    ``movies_root`` / ``tv_root`` are each optional: a row of that media type
    reaching ``import_download`` while its root is unset gets its own honest,
    retryable ``ImportBlocked`` (never a crash, never silently skipped) rather
    than gating the whole cycle — an install with only ONE root configured still
    drains that type normally. ``anime_movie_root`` / ``anime_tv_root``
    (ADR-0015) are likewise optional overrides applied only to ``is_anime``
    requests; see :func:`import_download`.

    The completed -> available promotion is a SEPARATE pass
    (:func:`run_availability_cycle`) that needs only Plex, so it keeps working even
    when the download client is down or the Movies/TV root is unset.
    """
    download_repo = SqlDownloadRepository(session)
    for row in await download_repo.list_active():
        # Auto-drain EVERY resumable row, ownerless ones included (issue #74). A row
        # whose ``media_request_id`` is NULL used to be filtered out here and so sat
        # in ``ImportPending`` / ``Importing`` forever, never surfacing. But
        # ``_import_download_locked`` already handles an ownerless row honestly — it
        # blocks it as a retryable ``ImportBlocked`` ("import has no owning request")
        # — so letting it through turns an invisible stuck row into a visible,
        # retryable block instead of silently skipping it every cycle.
        if row.status in _AUTO_DRAIN:
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
                    anime_movie_root=anime_movie_root,
                    anime_tv_root=anime_tv_root,
                )
            except Exception:
                await session.rollback()
                _logger.exception(
                    "import of download failed; will retry next cycle",
                    extra={"download_id": row.id},
                )


async def run_availability_cycle(
    *, library: LibraryPort, session: AsyncSession, now: datetime | None = None
) -> None:
    """Confirm ``completed`` ("Finalizing") movies/seasons are indexed in Plex and
    promote them to ``available``. Depends ONLY on Plex — so an import that already
    triggered a scan still reaches ``available`` even if the download client or the
    Movies/TV root is unavailable afterward. One item failing never aborts the cycle.

    ``now`` is the instant elapsed-time math (the bounded-Finalizing warning,
    issue #158) is measured against; defaults to ``datetime.now(UTC)`` and exists
    purely so a test can inject a fixed/advancing clock instead of sleeping real
    minutes.

    BATCHED, not per-row (issue #136): a burst of still-finalizing rows used to cost
    one ``is_available`` call PER row, and the adapter never trusts a cached absence
    -- so N pending movies meant N full movie-library crawls, and M pending seasons
    meant M whole-library ``/children`` crawls, every 15s tick. Movies now cost AT
    MOST one ``present_ids`` batch call for the whole tick, passed
    ``refresh_absent=True`` so it NEVER trusts a cached absence for a still-pending
    movie (mirroring the old per-row ``is_available``'s contract at batch
    granularity) -- a warmed snapshot missing one of this tick's pending movies
    triggers one fresh crawl before answering, so a movie is promoted on the tick
    after it actually finishes indexing in Plex, not held for the rest of the
    presence-cache TTL. TV seasons are grouped by show and resolved with exactly
    ONE batch ``season_presence`` call for the WHOLE tick's distinct pending shows
    (never one call per show, never a whole-library ``/children`` crawl, always
    fresh) -- see ``LibraryPort.season_presence``. A genuine TRANSPORT failure
    (the call itself raising) fails the WHOLE TV pass for this tick (mirroring
    the movie batch's failure posture just above): every pending season stays
    ``completed`` for the next tick's retry rather than promoting on a
    partial/guessed result. But when the call SUCCEEDS and merely OMITS one
    show's id from its returned mapping (round 4, #136 review -- that show's own
    ``/children`` lookup failed, e.g. a metadata row deleted mid-cycle or a
    persistently bad row), only THAT show's pending seasons are skipped for
    retry; every OTHER pending show in the same tick still promotes normally --
    a single bad Plex row must never starve unrelated shows at "Finalizing".

    PATH-BASED CONFIRMATION FALLBACK (issue #158): some titles are matched by
    Plex's metadata provider to an item carrying no (or the WRONG) tmdb guid --
    GUID confirmation can then never succeed, no matter how long this cycle
    waits. For a row the GUID batch above did NOT confirm, and which carries a
    ``library_path`` breadcrumb (the folder/directory THIS app placed the file
    into, ADR-0012), a SECOND, still-batched call
    (:meth:`~plex_manager.ports.library.LibraryPort.confirm_paths`) asks Plex
    whether the file is there by DIRECTORY-PREFIX instead -- at most one extra
    section crawl per tick (per media type), never one call per row, and GUID
    stays primary (path is only ever consulted for a GUID-miss row). A row still
    unconfirmed by EITHER check stays honestly ``completed`` -- never a new
    status -- and BOUNDED FINALIZING (:func:`_check_bounded_finalizing`) logs a
    low-duty-cycle WARNING once it has been stuck long enough, so "Finalizing"
    can no longer spin silently forever. That warning is only ever raised on a
    DEFINITIVE double-miss: if either check consulted for a row (the GUID
    batch, or the path fallback when a ``library_path`` breadcrumb makes it
    applicable) raised instead of answering -- Plex unreachable, an auth
    failure, etc. -- the row's bookkeeping is left untouched for the next
    tick's retry instead. A same-tick transport failure must never be
    misattributed to a library/GUID mismatch; the "batch availability check
    failed" warning already logged above names the real cause.
    """
    effective_now = now if now is not None else datetime.now(UTC)
    request_repo = SqlRequestRepository(session)
    completed_movies = [
        request
        for request in await request_repo.list_by_status(RequestStatus.completed.value)
        if request.media_type == "movie"
    ]
    present_movie_keys: frozenset[tuple[int, Literal["movie", "tv"]]] = frozenset()
    # Whether the GUID batch actually returned an answer this tick, as opposed
    # to defaulting to "nothing confirmed" because the call itself raised
    # (round-5 finding: a transport failure must never be mistaken for a
    # genuine "Plex doesn't have this" -- the "batch availability check
    # failed" warning above already surfaces the real cause).
    present_ids_succeeded = False
    if completed_movies:
        try:
            present_movie_keys = await library.present_ids(
                [(request.tmdb_id, "movie") for request in completed_movies],
                refresh_absent=True,
            )
            present_ids_succeeded = True
        except (PlexLibraryError, PlexAuthError, NotImplementedError):
            _logger.warning(
                "batch availability check failed for %d completed movie(s); will retry next cycle",
                len(completed_movies),
            )

    # Path-based fallback candidates (issue #158): every GUID-miss movie that
    # carries a library_path breadcrumb, resolved in ONE extra batched call for
    # the WHOLE tick -- never one ``confirm_paths`` call per row.
    movies_needing_path_check: list[tuple[RequestRecord, str]] = []
    for request in completed_movies:
        if (request.tmdb_id, "movie") in present_movie_keys:
            continue
        if request.library_path:
            movies_needing_path_check.append((request, request.library_path))
    path_confirmed_movie_paths: frozenset[str] = frozenset()
    # Same "did we actually get an answer" tracking as ``present_ids_succeeded``
    # above, for the path fallback's own transport call.
    path_check_succeeded = False
    if movies_needing_path_check:
        try:
            path_confirmed_movie_paths = await library.confirm_paths(
                "movie", [path for _request, path in movies_needing_path_check]
            )
            path_check_succeeded = True
        except (PlexLibraryError, PlexAuthError, NotImplementedError):
            _logger.warning(
                "path-based availability check failed for %d completed movie(s) with no "
                "confirming tmdb GUID; will retry next cycle",
                len(movies_needing_path_check),
            )

    for request in completed_movies:
        key = _movie_unconfirmed_key(request.id)
        guid_confirmed = (request.tmdb_id, "movie") in present_movie_keys
        path_confirmed = (
            not guid_confirmed
            and request.library_path is not None
            and request.library_path in path_confirmed_movie_paths
        )
        if not (guid_confirmed or path_confirmed):
            # Only treat this row as DEFINITIVELY unconfirmed -- and eligible
            # for the bounded-Finalizing warning -- when every confirmation
            # check consulted for it actually completed rather than raising.
            # A same-tick transport failure (Plex unreachable, etc.) must
            # never be misattributed to a library/GUID mismatch; the batch
            # warning(s) logged above already name the real cause, and this
            # row's bookkeeping is simply left untouched for the next tick's
            # retry (round-5 finding).
            checks_conclusive = present_ids_succeeded and (
                request.library_path is None or path_check_succeeded
            )
            if checks_conclusive:
                _check_bounded_finalizing(
                    key,
                    _unconfirmed_anchor(key, request.completed_at, now=effective_now),
                    request.title,
                    now=effective_now,
                )
            continue
        try:
            await request_repo.mark_available(request.id)
            await session.commit()
            _forget_unconfirmed(key)
        except (PlexLibraryError, PlexAuthError, NotImplementedError):
            await session.rollback()
            _logger.warning(
                "availability promotion failed; will retry next cycle",
                extra={"tmdb_id": request.tmdb_id, "request_id": request.id},
            )

    # TV: per-SEASON confirmation, mirroring the movie loop above but scoped to
    # SeasonRequest rows -- a show's OTHER seasons may still be mid-flight while
    # one season is ready to confirm, so this is never gated on the parent's
    # (computed rollup) status. GROUPED by show so the WHOLE tick's distinct
    # pending shows pay exactly ONE batch ``season_presence`` call, regardless of
    # how many shows or seasons are pending -- never one lookup per show, never
    # ``is_available``.
    season_repo = SqlSeasonRequestRepository(session)
    seasons_by_show: dict[int, list[SeasonRequestRecord]] = {}
    for season_request in await season_repo.list_by_status(RequestStatus.completed.value):
        seasons_by_show.setdefault(season_request.tmdb_id, []).append(season_request)

    present_seasons_by_show: Mapping[int, frozenset[int]] = {}
    season_presence_succeeded = False
    if seasons_by_show:
        try:
            present_seasons_by_show = await library.season_presence(seasons_by_show.keys())
            season_presence_succeeded = True
        except (PlexLibraryError, PlexAuthError, NotImplementedError):
            # A whole-pass TRANSPORT failure (mirroring the movie batch's failure
            # posture above) -- every pending season honestly stays ``completed``
            # for the next tick's retry rather than promoting on a
            # partial/guessed result.
            _logger.warning(
                "batch availability check failed for %d pending show(s); will retry next cycle",
                len(seasons_by_show),
            )

    # First pass: resolve GUID confirmation per season (unchanged from before
    # #158) and, for a GUID-miss season with a library_path breadcrumb, collect
    # it as a path-check candidate -- never per-row, one batched call below.
    season_guid_confirmed: dict[int, bool] = {}
    # Whether THIS show's GUID answer is a real, returned result -- as opposed
    # to a defaulted-empty ``frozenset()`` standing in for either a whole-pass
    # transport failure or a per-show lookup failure (see below). Either kind
    # of miss means we genuinely don't know this show's presence, so it must
    # never be conflated with "Plex was asked and said no" for the bounded-
    # Finalizing warning (round-5 finding, mirroring ``present_ids_succeeded``
    # for movies above).
    season_guid_check_conclusive: dict[int, bool] = {}
    seasons_needing_path_check: list[tuple[SeasonRequestRecord, str]] = []
    for tmdb_id, season_requests in seasons_by_show.items():
        present_seasons = present_seasons_by_show.get(tmdb_id)
        show_conclusive = present_seasons is not None
        if present_seasons is None:
            # Distinguish a per-show lookup failure (the batch call SUCCEEDED but
            # omitted this one id -- see ``LibraryPort.season_presence``) from the
            # whole-pass transport failure already warned about above -- only log
            # here when the call itself actually succeeded, so a single bad show
            # is named explicitly without a redundant warning on every pending
            # show when the whole pass failed instead (round 4, #136 review).
            # Either way, only THIS show's seasons are skipped for GUID
            # confirmation this tick -- every OTHER pending show still resolves
            # normally, and the path fallback below still gets a chance.
            if season_presence_succeeded:
                _logger.warning(
                    "season lookup failed for show; will retry next cycle",
                    extra={"tmdb_id": safe_int(tmdb_id)},
                )
            present_seasons = frozenset[int]()
        for season_request in season_requests:
            confirmed = season_request.season_number in present_seasons
            season_guid_confirmed[season_request.id] = confirmed
            season_guid_check_conclusive[season_request.id] = show_conclusive
            if not confirmed and season_request.library_path:
                seasons_needing_path_check.append((season_request, season_request.library_path))

    path_confirmed_season_paths: frozenset[str] = frozenset()
    # Same "did we actually get an answer" tracking as the movie path check
    # above, for TV's own ``confirm_paths`` call.
    season_path_check_succeeded = False
    if seasons_needing_path_check:
        try:
            path_confirmed_season_paths = await library.confirm_paths(
                "tv", [path for _season_request, path in seasons_needing_path_check]
            )
            season_path_check_succeeded = True
        except (PlexLibraryError, PlexAuthError, NotImplementedError):
            _logger.warning(
                "path-based availability check failed for %d completed season(s) with no "
                "confirming tmdb GUID; will retry next cycle",
                len(seasons_needing_path_check),
            )

    # Cache of media_request_id -> title, populated lazily: only a season that is
    # STILL unconfirmed after both checks needs its show's title (for the
    # bounded-Finalizing warning), so a show with every season confirmed never
    # pays this extra lookup.
    title_cache: dict[int, str] = {}

    async def _title_for(media_request_id: int) -> str:
        cached = title_cache.get(media_request_id)
        if cached is not None:
            return cached
        record = await request_repo.get(media_request_id)
        title = record.title if record is not None else f"request {media_request_id}"
        title_cache[media_request_id] = title
        return title

    for season_requests in seasons_by_show.values():
        for season_request in season_requests:
            key = _season_unconfirmed_key(
                season_request.media_request_id, season_request.season_number
            )
            guid_confirmed = season_guid_confirmed.get(season_request.id, False)
            path_confirmed = (
                not guid_confirmed
                and season_request.library_path is not None
                and season_request.library_path in path_confirmed_season_paths
            )
            if not (guid_confirmed or path_confirmed):
                # As with movies above: only warn when THIS show's GUID answer
                # was conclusive and, if a path check was needed, that check
                # also actually completed -- never on a same-tick transport
                # failure (round-5 finding).
                checks_conclusive = season_guid_check_conclusive.get(season_request.id, False) and (
                    season_request.library_path is None or season_path_check_succeeded
                )
                if checks_conclusive:
                    title = await _title_for(season_request.media_request_id)
                    _check_bounded_finalizing(
                        key,
                        # SeasonRequest carries no per-season ``completed_at``
                        # mirror (deliberately deferred -- see the module
                        # dict's docstring), so the anchor always falls back
                        # to the in-memory first-observed-miss timestamp for
                        # TV.
                        _unconfirmed_anchor(key, None, now=effective_now),
                        f"{title} season {season_request.season_number}",
                        now=effective_now,
                    )
                continue
            try:
                await season_request_service.mark_available(
                    session,
                    media_request_id=season_request.media_request_id,
                    season_number=season_request.season_number,
                )
                await session.commit()
                _forget_unconfirmed(key)
            except (PlexLibraryError, PlexAuthError, NotImplementedError):
                await session.rollback()
                _logger.warning(
                    "availability promotion failed for season %s; will retry next cycle",
                    season_request.season_number,
                    extra={
                        "tmdb_id": season_request.tmdb_id,
                        "request_id": season_request.media_request_id,
                    },
                )

    # Sweep the bounded-Finalizing bookkeeping: forget any previously-tracked row
    # that is no longer in THIS tick's completed set at all -- promoted through
    # some other path, re-armed by an operator, or deleted -- so the in-memory
    # dicts stay bounded to however many rows are ACTUALLY stuck completed now.
    current_keys = {_movie_unconfirmed_key(request.id) for request in completed_movies}
    current_keys.update(
        _season_unconfirmed_key(season_request.media_request_id, season_request.season_number)
        for season_requests in seasons_by_show.values()
        for season_request in season_requests
    )
    stale_keys = (
        _unconfirmed_warned_bucket.keys() | _unconfirmed_since_fallback.keys()
    ) - current_keys
    for key in stale_keys:
        _forget_unconfirmed(key)
