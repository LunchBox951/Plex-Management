"""Import orchestration — close the loop: validate, snapshot, scan -> Available.

When a completed torrent reaches ``ImportPending`` (the reconciler maps the
client's seeding/complete states there), this service validates the file against
the requested movie/show with the SAME decision brain the search uses, snapshots
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
import errno
import hashlib
import logging
import os
import secrets
import stat
import weakref
from collections.abc import Generator, Mapping
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Final, Literal, NamedTuple, cast

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from plex_manager.adapters.filesystem.local import (
    clear_stale_publish_locks,
    rename_exchange,
    rename_no_replace,
)
from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.adapters.qbittorrent import QbittorrentError
from plex_manager.domain.download_payload import (
    EMPTY_PAYLOAD_REJECTION_REASON,
    PAYLOAD_VALIDATION_POLICY_VERSION,
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
from plex_manager.ports.filesystem import (
    VIDEO_EXTENSIONS,
    FilePlacementIdentity,
    RootAnchoredFileSystemPort,
)
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
_MANUAL_CLEANUP_BREADCRUMB_REASON: Final = (
    "stored import breadcrumb requires manual cleanup before re-search"
)
_PAYLOAD_VALIDATED_IMPORT_HISTORY_PREFIX: Final = (
    f"payload-validated import policy={PAYLOAD_VALIDATION_POLICY_VERSION} "
)
_MANIFEST_OUTAGE_REASON_PREFIX: Final = "could not validate torrent payload manifest:"
_UNVALIDATED_BREADCRUMB_REASON_PREFIX: Final = (
    "stored import breadcrumb awaits download client manifest validation"
)
_UNVALIDATED_BREADCRUMB_REASON: Final = (
    f"{_UNVALIDATED_BREADCRUMB_REASON_PREFIX}; {_MANUAL_CLEANUP_BREADCRUMB_REASON}"
)
_OWNED_PLACEMENT_CLEANUP_FAILURE_FRAGMENT: Final = "could not remove stored import breadcrumb"
_PLACEMENT_IDENTITY_CHANGED_REASON: Final = (
    "stored import breadcrumb changed after payload validation; "
    f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
)
_SCOPED_BREADCRUMB_CLIENT_MISSING_REASON_PREFIX: Final = (
    "validated scoped import breadcrumb awaits download client status"
)
_SCOPED_BREADCRUMB_CLIENT_MISSING_REASON: Final = (
    f"{_SCOPED_BREADCRUMB_CLIENT_MISSING_REASON_PREFIX}; {_MANUAL_CLEANUP_BREADCRUMB_REASON}"
)
_NO_FAILED_REASON_PREDICATE: Final = object()

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
_PLEX_SCAN_FAILED_REASON_PREFIX: Final = "plex scan failed:"

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


class _SourceCandidate(NamedTuple):
    path: str
    size: int
    relative_path: str
    authority_root: str
    identity: FilePlacementIdentity


class _SourceAuthority(NamedTuple):
    root_fd: int
    root_abs: str


class _DirectoryIdentity(NamedTuple):
    device: int
    inode: int


def _placement_identity(observed: os.stat_result) -> FilePlacementIdentity:
    return FilePlacementIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        size=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
        ctime_ns=observed.st_ctime_ns,
        mode=observed.st_mode,
    )


def _source_stable_after_publication(
    expected: FilePlacementIdentity,
    observed: FilePlacementIdentity,
) -> bool:
    """A secure distinct-inode snapshot leaves every source field unchanged."""
    return expected == observed


def _placement_matches_validated_source(
    source: FilePlacementIdentity,
    placed: FilePlacementIdentity,
) -> bool:
    """Whether published bytes/metadata still match the validated source snapshot."""
    if not stat.S_ISREG(placed.mode) or source.size != placed.size:
        return False
    # A hardlink permanently shares the torrent writer's inode. Require an
    # independent snapshot so post-validation writes cannot mutate Plex data.
    # Cross-filesystem copies may normalize timestamp precision/permissions; the
    # trusted primitive verifies a complete size before atomic publication.
    return source.device != placed.device or source.inode != placed.inode


class _TvImportPlan(NamedTuple):
    target: _TvImportTarget
    season_dir: Path
    source_by_rel: dict[str, _SourceCandidate]
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


def _library_root_is_visible(library_root: str) -> bool:
    """Whether the configured root is a visible, non-symlink directory authority."""
    try:
        root_abs = os.path.abspath(os.path.normpath(library_root))
        root_real = os.path.realpath(root_abs)
        filesystem_prefix = os.path.realpath(os.sep)
        return (
            root_real != filesystem_prefix
            and root_real.startswith(filesystem_prefix)
            and root_real == root_abs
            and stat.S_ISDIR(Path(root_real).lstat().st_mode)
        )
    except (OSError, ValueError):
        return False


def _bind_expected_library_breadcrumb(
    download_path: str,
    library_root: str,
    expected_paths: tuple[Path, ...],
) -> Path | None:
    """Return a deterministic destination matching a persisted breadcrumb.

    Persisted paths are state, not filesystem authority. Resolve them only far
    enough to compare against the finite destinations derived from the request,
    and return that trusted destination rather than the stored spelling. Callers
    can then inspect/clean/fingerprint only a path rooted in current settings.
    """
    candidate_normalized = os.path.normcase(os.path.abspath(os.path.normpath(download_path)))
    for expected in expected_paths:
        expected_normalized = os.path.normcase(os.path.abspath(os.path.normpath(expected)))
        if candidate_normalized != expected_normalized:
            continue
        try:
            root_real = os.path.realpath(library_root)
            expected_real = os.path.realpath(expected)
        except (OSError, ValueError):
            return None
        if _is_within(root_real, expected_real):
            return expected
    return None


def _ensure_under_save_path(save_path: str, candidate: str) -> str:
    """Validate containment without erasing symlinks below the save root.

    The save-root-relative spelling is security-significant: the later dirfd
    traversal must see every literal component so ``O_NOFOLLOW`` can reject a
    symlink instead of silently importing its canonical target.  Canonical
    paths are still compared here to reject an escaping link, but only the save
    root itself is canonicalized in the returned pathname.
    """
    root_literal = os.path.abspath(os.path.normpath(save_path))
    candidate_literal = os.path.abspath(os.path.normpath(candidate))
    lexical_prefix = root_literal.rstrip(os.sep) + os.sep
    if candidate_literal == root_literal or not candidate_literal.startswith(lexical_prefix):
        raise _UnsafeContentPathError("download content path is outside download save path")
    relative_parts = Path(os.path.relpath(candidate_literal, root_literal)).parts
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        raise _UnsafeContentPathError("download content path is outside download save path")
    root_real = os.path.realpath(root_literal)
    candidate_real = os.path.realpath(candidate_literal)
    if not _is_within(root_real, candidate_real):
        raise _UnsafeContentPathError("download content path is outside download save path")
    return os.path.join(root_real, *relative_parts)


def _capture_directory_authority(path: str) -> tuple[str, _DirectoryIdentity] | None:
    """Capture one canonical directory pathname and exact inode identity."""
    try:
        root_abs = os.path.realpath(os.path.abspath(os.path.normpath(path)))
        filesystem_prefix = os.path.realpath(os.sep)
        if root_abs == filesystem_prefix or not root_abs.startswith(filesystem_prefix):
            return None
        observed = os.stat(root_abs, follow_symlinks=False)
        if not stat.S_ISDIR(observed.st_mode):
            return None
        return root_abs, _DirectoryIdentity(observed.st_dev, observed.st_ino)
    except (OSError, ValueError):
        return None


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
    save_identity: _DirectoryIdentity | None


class _VisibleContent(NamedTuple):
    """Container-visible content bound to one exact download-root inode."""

    path: str
    source_root: str
    source_root_identity: _DirectoryIdentity


def _resolved_live_content(save_path: str, candidate: str) -> _ResolvedContent:
    """Bind client content to the current canonical save-root observation."""
    save_literal = os.path.abspath(os.path.normpath(save_path))
    save_real = os.path.realpath(save_literal)
    if save_real != save_literal:
        raise _UnsafeContentPathError("download save path must not be a symlink")
    candidate_beneath_root = _ensure_under_save_path(save_path, candidate)
    captured = _capture_directory_authority(save_real)
    identity = captured[1] if captured is not None and captured[0] == save_real else None
    return _ResolvedContent(candidate_beneath_root, save_real, identity)


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
            return _resolved_live_content(status.save_path, status.content_path)
        raise _UnsafeContentPathError("download client reported content path without save path")
    if status is not None and status.save_path and status.name:
        if os.path.isabs(status.name):
            raise _UnsafeContentPathError("download content path is outside download save path")
        return _resolved_live_content(
            status.save_path,
            os.path.join(status.save_path, status.name),
        )
    if download_path:
        return _ResolvedContent(download_path, None, None)
    return None


async def _resolve_visible_content(
    qbt: DownloadClientPort, torrent_hash: str, resolved: _ResolvedContent
) -> _VisibleContent | None:
    """Container-visible content plus its bound save-root, or ``None``.

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
        if resolved.save_path is None or resolved.save_identity is None:
            return None
        captured = await asyncio.to_thread(_capture_directory_authority, resolved.save_path)
        if (
            captured is None
            or captured[0] != resolved.save_path
            or captured[1] != resolved.save_identity
        ):
            return None
        return _VisibleContent(resolved.path, captured[0], captured[1])
    if not resolved.save_path:
        # A stored crash-resume breadcrumb has no anchor to remap against: only
        # the verbatim path counts (a free suffix search would reintroduce the
        # stale-match hazard).
        return None
    files = await qbt.list_files(torrent_hash)
    expected = [(f.name, f.size_bytes) for f in files]
    visible_path = await asyncio.to_thread(
        path_visibility.remap_download_content, resolved.path, resolved.save_path, expected
    )
    if visible_path is None:
        return None
    relative_parts = Path(os.path.relpath(resolved.path, resolved.save_path)).parts
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        return None
    visible_abs = os.path.abspath(os.path.normpath(visible_path))
    visible_root = visible_abs
    for _part in relative_parts:
        visible_root = os.path.dirname(visible_root)
    if (
        os.path.abspath(os.path.normpath(os.path.join(visible_root, *relative_parts)))
        != visible_abs
    ):
        return None
    captured = await asyncio.to_thread(_capture_directory_authority, visible_root)
    if captured is None or captured[0] != visible_root:
        return None
    canonical_content = os.path.join(captured[0], *relative_parts)
    return _VisibleContent(canonical_content, captured[0], captured[1])


def _source_authority_is_current(authority: _SourceAuthority) -> bool:
    """Whether a held source root still occupies its canonical pathname."""
    try:
        held = os.fstat(authority.root_fd)
        current = os.stat(authority.root_abs, follow_symlinks=False)
        return (
            stat.S_ISDIR(held.st_mode)
            and stat.S_ISDIR(current.st_mode)
            and held.st_dev == current.st_dev
            and held.st_ino == current.st_ino
            and os.path.realpath(f"/proc/self/fd/{authority.root_fd}") == authority.root_abs
        )
    except (OSError, ValueError):
        return False


@contextlib.contextmanager
def _open_source_authority(
    authority_root: str,
    *,
    expected_identity: _DirectoryIdentity | None = None,
) -> Generator[_SourceAuthority]:
    """Pin a canonical directory authority without following its final entry."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    filesystem_prefix = os.path.realpath(os.sep)
    root_literal = os.path.abspath(os.path.normpath(authority_root))
    if (
        not nofollow
        or not directory
        or not os.path.isdir("/proc/self/fd")
        or root_literal == filesystem_prefix
        or not root_literal.startswith(filesystem_prefix)
    ):
        raise OSError("descriptor-anchored source authority is unavailable")
    before = os.stat(root_literal, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("source authority is not a directory")
    root_fd = os.open(root_literal, os.O_RDONLY | directory | nofollow | cloexec)
    try:
        opened = os.fstat(root_fd)
        root_abs = os.path.realpath(f"/proc/self/fd/{root_fd}")
        if (
            not stat.S_ISDIR(opened.st_mode)
            or before.st_dev != opened.st_dev
            or before.st_ino != opened.st_ino
            or (
                expected_identity is not None
                and _DirectoryIdentity(opened.st_dev, opened.st_ino) != expected_identity
            )
            or not os.path.isabs(root_abs)
        ):
            raise OSError("source authority changed while it was opened")
        authority = _SourceAuthority(root_fd, root_abs)
        if not _source_authority_is_current(authority):
            raise OSError("source authority changed while it was opened")
        yield authority
    finally:
        with contextlib.suppress(OSError):
            os.close(root_fd)


@contextlib.contextmanager
def _open_source_directory_beneath(
    authority: _SourceAuthority,
    relative_parts: tuple[str, ...],
) -> Generator[_SourceAuthority]:
    """Pin a no-follow descendant directory beneath an existing source root."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if (
        not relative_parts
        or any(part in {"", ".", ".."} for part in relative_parts)
        or not nofollow
        or not directory
    ):
        raise OSError("invalid source directory beneath download root")
    opened: list[int] = []
    try:
        current_fd = os.dup(authority.root_fd)
        opened.append(current_fd)
        for component in relative_parts:
            current_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=current_fd,
            )
            opened.append(current_fd)
        child_abs = os.path.join(authority.root_abs, *relative_parts)
        child = _SourceAuthority(current_fd, child_abs)
        if not _source_authority_is_current(authority) or not _source_authority_is_current(child):
            raise OSError("source directory authority changed")
        yield child
    finally:
        for fd in reversed(opened):
            with contextlib.suppress(OSError):
                os.close(fd)


def _source_candidate_from_authority(
    path: str,
    relative_path: str,
    *,
    authority: _SourceAuthority,
) -> _SourceCandidate | None:
    """Capture a candidate through an already-pinned source-root descriptor."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not directory:
        return None
    root_abs = authority.root_abs
    candidate_abs = os.path.abspath(os.path.normpath(path))
    prefix = root_abs.rstrip(os.sep) + os.sep
    if candidate_abs == root_abs or not candidate_abs.startswith(prefix):
        return None
    relative_parts = Path(os.path.relpath(candidate_abs, root_abs)).parts
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        return None
    opened: list[int] = []
    source_fd: int | None = None
    try:
        current_fd = os.dup(authority.root_fd)
        opened.append(current_fd)
        for component in relative_parts[:-1]:
            current_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=current_fd,
            )
            opened.append(current_fd)
        name = relative_parts[-1]
        before = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            return None
        source_fd = os.open(
            name,
            os.O_RDONLY | nofollow | cloexec | nonblock,
            dir_fd=current_fd,
        )
        observed = os.fstat(source_fd)
        current_name = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(observed.st_mode)
            or _placement_identity(before) != _placement_identity(observed)
            or _placement_identity(before) != _placement_identity(current_name)
            or os.path.realpath(f"/proc/self/fd/{source_fd}") != candidate_abs
            or not _source_authority_is_current(authority)
        ):
            return None
        identity = _placement_identity(observed)
        return _SourceCandidate(
            candidate_abs,
            observed.st_size,
            relative_path,
            root_abs,
            identity,
        )
    except (OSError, ValueError):
        return None
    finally:
        if source_fd is not None:
            with contextlib.suppress(OSError):
                os.close(source_fd)
        for fd in reversed(opened):
            with contextlib.suppress(OSError):
                os.close(fd)


@contextlib.contextmanager
def _anchored_source_file(source: _SourceCandidate) -> Generator[tuple[int, Path]]:
    """Hold the validated source inode open through adoption and publication."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow or not os.path.isdir("/proc/self/fd"):
        raise OSError("descriptor-anchored source access is unavailable")
    candidate_real = os.path.realpath(os.path.abspath(os.path.normpath(source.path)))
    prefix = source.authority_root.rstrip(os.sep) + os.sep
    if candidate_real != source.path or not candidate_real.startswith(prefix):
        raise OSError("validated import source authority changed")
    source_fd = os.open(candidate_real, os.O_RDONLY | nofollow | cloexec | nonblock)
    try:
        observed = os.fstat(source_fd)
        current = _placement_identity(observed)
        if (
            current != source.identity
            or not stat.S_ISREG(observed.st_mode)
            or os.path.realpath(f"/proc/self/fd/{source_fd}") != source.path
        ):
            raise OSError("validated import source identity changed")
        yield source_fd, Path(f"/proc/self/fd/{source_fd}")
    finally:
        with contextlib.suppress(OSError):
            os.close(source_fd)


def _resolve_sources(
    fs: FileSystemPort,
    content_path: str,
    *,
    source_root: str,
    source_root_identity: _DirectoryIdentity,
) -> list[_SourceCandidate]:
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
    root_abs = os.path.abspath(os.path.normpath(source_root))
    content_abs = os.path.abspath(os.path.normpath(content_path))
    prefix = root_abs.rstrip(os.sep) + os.sep
    if content_abs == root_abs or not content_abs.startswith(prefix):
        return []
    content_parts = Path(os.path.relpath(content_abs, root_abs)).parts
    if not content_parts or any(part in {"", ".", ".."} for part in content_parts):
        return []
    try:
        with _open_source_authority(
            root_abs,
            expected_identity=source_root_identity,
        ) as root_authority:
            try:
                with _open_source_directory_beneath(
                    root_authority,
                    tuple(content_parts),
                ) as content_authority:
                    anchor = os.path.dirname(content_authority.root_abs)
                    sources: list[_SourceCandidate] = []
                    for abs_path, _size, literal_relative in fs.list_video_files(
                        content_authority.root_abs
                    ):
                        literal_path = os.path.abspath(
                            os.path.normpath(
                                os.path.join(content_authority.root_abs, literal_relative)
                            )
                        )
                        # Compare captured text, never a fresh realpath that could
                        # follow a replacement ancestor and re-authorize it.
                        reported_path = os.path.abspath(os.path.normpath(abs_path))
                        if reported_path != literal_path:
                            continue
                        relative = os.path.relpath(literal_path, anchor)
                        candidate = _source_candidate_from_authority(
                            literal_path,
                            relative,
                            authority=content_authority,
                        )
                        if candidate is not None:
                            sources.append(candidate)
                    if not _source_authority_is_current(content_authority):
                        return []
                    if not _source_authority_is_current(root_authority):
                        return []
                    return sources
            except OSError:
                # A single-file torrent: resolve the final entry directly from
                # the pinned save-root fd. Symlinks/FIFOs/special entries fail the
                # regular-file checks without ever becoming an authority.
                if Path(content_abs).suffix.lower() not in VIDEO_EXTENSIONS:
                    return []
                canonical_path = os.path.join(root_authority.root_abs, *content_parts)
                candidate = _source_candidate_from_authority(
                    canonical_path,
                    Path(content_abs).name,
                    authority=root_authority,
                )
                if not _source_authority_is_current(root_authority):
                    return []
                return [candidate] if candidate is not None else []
    except (OSError, ValueError):
        return []


def _place_file(
    fs: FileSystemPort,
    source: _SourceCandidate,
    dst: Path,
    *,
    allowed_root: str,
) -> tuple[bool, FilePlacementIdentity | None, str | None]:
    """Snapshot ``src`` to ``dst``, idempotently (sync I/O, run in a thread).

    Returns ``(created, publish_identity, adopted_identity)``. Exactly one identity
    is populated: the creation-bound primitive token for a new destination, or the
    descriptor-bound content identity for an already-supplied identical file. The
    caller rolls a created path back only while its identity still matches.

    A fully-imported destination with identical bytes is left untouched. A different
    file already at ``dst`` (a user's library file, or a stale partial) is NEVER
    blind-deleted — it is surfaced as a ``FileExistsError`` conflict for the operator
    to resolve, so a re-import never silently overwrites someone else's file.
    """
    with _anchored_source_file(source) as (source_fd, source_path):
        source_name = os.path.basename(source.path)
        # lexists, not exists: exists() follows a symlink and reads a DANGLING one as
        # absent, which would let hardlink_or_copy's rename fallback silently replace
        # the symlink entry (GHSA-8fj8) instead of surfacing the conflict below.
        if os.path.lexists(os.fspath(dst)):
            adopted_identity = _adopted_file_identity_if_same(
                os.fspath(source_path),
                dst,
                allowed_root=allowed_root,
            )
            if adopted_identity is not None:
                return False, None, adopted_identity
            # A differently-sized file is already at the destination: a user's
            # manually-managed library file, or a title Plex availability missed. NEVER
            # blind-delete it (that is data loss) — surface it as an import conflict the
            # operator resolves, instead of overwriting their file with the download.
            raise FileExistsError(f"destination already exists with different content: {dst}")
        try:
            if not isinstance(fs, RootAnchoredFileSystemPort):
                raise OSError("filesystem adapter lacks root-anchored placement support")
            placement_identity = fs.hardlink_or_copy_from_fd_beneath(
                source_fd,
                source_name,
                dst,
                destination_root=Path(allowed_root),
            )
        except FileExistsError:
            # Lost a placement race: a concurrent import (the reconcile loop racing the
            # operator's POST /queue/{id}/import retry) created ``dst`` between the
            # lexists check above and this link. Identical content is an
            # idempotent win for the other attempt, NOT a failure to block on; a
            # different content is a genuine conflict, surfaced like the pre-existing case.
            adopted_identity = (
                _adopted_file_identity_if_same(
                    os.fspath(source_path),
                    dst,
                    allowed_root=allowed_root,
                )
                if os.path.lexists(os.fspath(dst))
                else None
            )
            if adopted_identity is not None:
                return False, None, adopted_identity
            raise FileExistsError(
                f"destination already exists with different content: {dst}"
            ) from None
        source_after = _placement_identity(os.fstat(source_fd))
        if (
            not _source_stable_after_publication(
                source.identity,
                source_after,
            )
            or not _placement_matches_validated_source(source.identity, placement_identity)
            or not _published_snapshot_is_safe(
                dst,
                placement_identity,
                allowed_root=allowed_root,
            )
        ):
            _remove_quietly(
                dst,
                expected_identity=_placement_content_identity(placement_identity),
                allowed_root=allowed_root,
            )
            raise OSError("validated import source changed during publication")
        return True, placement_identity, None


def _file_digest(path: str | Path) -> bytes:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _adopted_file_identity_if_same(
    src: str,
    dst: Path,
    *,
    allowed_root: str,
) -> str | None:
    """Bind an idempotently adopted file to one descriptor-held observation."""
    return _adopted_file_identity_from_open_files(
        src,
        dst,
        allowed_root=allowed_root,
    )


class _PlacedPath(NamedTuple):
    path: Path
    identity: str


def _placement_content_identity(identity: FilePlacementIdentity) -> str:
    """Encode the creation-bound regular-file observation as payload identity."""
    digest = hashlib.sha256()
    digest.update(os.fsencode("."))
    digest.update(b"\0")
    digest.update(
        (
            f"{identity.device}:{identity.inode}:{identity.size}:"
            f"{identity.mtime_ns}:{identity.ctime_ns}:{stat.S_IFMT(identity.mode)}"
        ).encode()
    )
    digest.update(b"\0")
    return digest.hexdigest()


def _place_file_with_identity(
    fs: FileSystemPort,
    source: _SourceCandidate,
    dst: Path,
    *,
    allowed_root: str,
) -> tuple[bool, str | None]:
    """Place/adopt a file and fingerprint that exact observation in one worker."""
    placed, published_identity, adopted_identity = _place_file(
        fs,
        source,
        dst,
        allowed_root=allowed_root,
    )
    if placed:
        return (
            placed,
            _placement_content_identity(published_identity)
            if published_identity is not None
            else None,
        )
    return placed, adopted_identity


def _payload_identity_if_observations_match(
    download_path: str, observations: list[_PlacedPath], *, allowed_root: str
) -> str | None:
    """Fingerprint a tree only while every placed/adopted file is unchanged."""
    before = _payload_content_identity(download_path, allowed_root=allowed_root)
    if before is None:
        return None
    if any(
        _payload_content_identity(str(observed.path), allowed_root=allowed_root)
        != observed.identity
        for observed in observations
    ):
        return None
    after = _payload_content_identity(download_path, allowed_root=allowed_root)
    return before if after == before else None


class _AnchoredParent(NamedTuple):
    root_fd: int
    parent_fd: int
    name: str
    root_abs: str
    parent_parts: tuple[str, ...]


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _snapshot_permissions_are_safe(observed: os.stat_result, file_fd: int) -> bool:
    """Whether only this service UID retains write authority to a snapshot."""
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.geteuid()
        or observed.st_nlink != 1
        or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        return False
    try:
        names = os.listxattr(file_fd)
    except OSError as exc:
        # Filesystems without extended-attribute support cannot carry an ACL
        # xattr. Any other inspection failure is unknown authority: fail closed.
        return exc.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}
    return not any("acl" in name.lower() for name in names)


def _same_regular_file_after_rename(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare every regular-file field that rename(2) itself leaves stable."""
    return (
        _same_inode(left, right)
        and stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_mode == right.st_mode
    )


def _anchored_parent_is_current(anchor: _AnchoredParent) -> bool:
    """Whether held root/parent descriptors still occupy their configured path."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    opened: list[int] = []
    try:
        filesystem_prefix = os.path.realpath(os.sep)
        if (
            anchor.root_abs == filesystem_prefix
            or not anchor.root_abs.startswith(filesystem_prefix)
            or os.path.realpath(anchor.root_abs) != anchor.root_abs
        ):
            return False
        current_fd = os.open(
            anchor.root_abs,
            os.O_RDONLY | directory | nofollow | cloexec,
        )
        opened.append(current_fd)
        if not _same_inode(os.fstat(current_fd), os.fstat(anchor.root_fd)):
            return False
        if os.path.realpath(f"/proc/self/fd/{current_fd}") != anchor.root_abs:
            return False
        for component in anchor.parent_parts:
            current_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=current_fd,
            )
            opened.append(current_fd)
        return _same_inode(os.fstat(current_fd), os.fstat(anchor.parent_fd))
    except (OSError, ValueError):
        return False
    finally:
        for fd in reversed(opened):
            with contextlib.suppress(OSError):
                os.close(fd)


@contextlib.contextmanager
def _anchored_parent_beneath(allowed_root: str, path: Path) -> Generator[_AnchoredParent]:
    """Open ``path.parent`` root-relatively without following symlinks."""
    root_abs = os.path.abspath(os.path.normpath(allowed_root))
    path_abs = os.path.abspath(os.path.normpath(path))
    prefix = root_abs.rstrip(os.sep) + os.sep
    if path_abs == root_abs or not path_abs.startswith(prefix):
        raise OSError("path is outside configured library root")
    relative_parts = Path(os.path.relpath(path_abs, root_abs)).parts
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        raise OSError("invalid path beneath configured library root")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    filesystem_prefix = os.path.realpath(os.sep)
    if (
        not nofollow
        or not directory
        or not os.path.isdir("/proc/self/fd")
        or root_abs == filesystem_prefix
        or not root_abs.startswith(filesystem_prefix)
        or os.path.realpath(root_abs) != root_abs
    ):
        raise OSError("root-anchored cleanup is unavailable")

    opened: list[int] = []
    try:
        current_fd = os.open(root_abs, os.O_RDONLY | directory | nofollow | cloexec)
        opened.append(current_fd)
        for component in relative_parts[:-1]:
            current_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=current_fd,
            )
            opened.append(current_fd)
        anchor = _AnchoredParent(
            root_fd=opened[0],
            parent_fd=current_fd,
            name=relative_parts[-1],
            root_abs=root_abs,
            parent_parts=tuple(relative_parts[:-1]),
        )
        if not _anchored_parent_is_current(anchor):
            raise OSError("root-anchored cleanup authority changed")
        yield anchor
    finally:
        for fd in reversed(opened):
            with contextlib.suppress(OSError):
                os.close(fd)


def _published_snapshot_is_safe(
    dst: Path,
    placement: FilePlacementIdentity,
    *,
    allowed_root: str,
) -> bool:
    """Bind safe permissions/ownership to the exact newly published inode."""
    destination_fd: int | None = None
    try:
        with _anchored_parent_beneath(allowed_root, dst) as anchor:
            destination_fd = os.open(
                anchor.name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                dir_fd=anchor.parent_fd,
            )
            observed = os.fstat(destination_fd)
            current = os.stat(
                anchor.name,
                dir_fd=anchor.parent_fd,
                follow_symlinks=False,
            )
            return (
                _placement_identity(observed) == placement
                and _placement_identity(current) == placement
                and _snapshot_permissions_are_safe(observed, destination_fd)
                and _anchored_parent_is_current(anchor)
            )
    except (OSError, ValueError):
        return False
    finally:
        if destination_fd is not None:
            with contextlib.suppress(OSError):
                os.close(destination_fd)


def _adopted_file_identity_from_open_files(
    src: str,
    dst: Path,
    *,
    allowed_root: str,
) -> str | None:
    """Compare source/destination through stable fds and bind the exact dst inode."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nofollow:
        return None
    destination_fd: int | None = None
    try:
        with _anchored_parent_beneath(allowed_root, dst) as anchor:
            destination_fd = os.open(
                anchor.name,
                os.O_RDONLY | nofollow | cloexec | nonblock,
                dir_fd=anchor.parent_fd,
            )
            destination_before = os.fstat(destination_fd)
            source_before = os.stat(src)
            if not stat.S_ISREG(destination_before.st_mode) or not stat.S_ISREG(
                source_before.st_mode
            ):
                return None
            if not _snapshot_permissions_are_safe(destination_before, destination_fd):
                # Never inherit a foreign owner's, writable mode's, external
                # link's, or access ACL's authority over adopted library bytes.
                return None
            if _same_inode(source_before, destination_before):
                # Any external hardlink preserves write authority through an
                # unknown pathname (including a legacy torrent source). Never
                # adopt that inode into the distinct-snapshot trust model.
                return None
            if not _anchored_parent_is_current(anchor):
                return None
            same_content = source_before.st_size == destination_before.st_size and _file_digest(
                src
            ) == _file_digest(Path(f"/proc/self/fd/{destination_fd}"))
            if not same_content:
                return None
            source_after = os.stat(src)
            destination_after = os.fstat(destination_fd)
            current_name = os.stat(
                anchor.name,
                dir_fd=anchor.parent_fd,
                follow_symlinks=False,
            )
            if (
                _placement_identity(source_before) != _placement_identity(source_after)
                or _placement_identity(destination_before) != _placement_identity(destination_after)
                or _placement_identity(destination_before) != _placement_identity(current_name)
                or not _snapshot_permissions_are_safe(destination_after, destination_fd)
                or not _anchored_parent_is_current(anchor)
            ):
                return None
            return _payload_entries_identity([(".", destination_before)])
    except (OSError, ValueError):
        return None
    finally:
        if destination_fd is not None:
            with contextlib.suppress(OSError):
                os.close(destination_fd)


def _remove_quietly(path: Path, *, expected_identity: str, allowed_root: str) -> bool:
    """Best-effort unlink; return whether the path is confirmed absent.

    ``lexists``-style verification matters here: a dangling symlink still exists,
    while an inaccessible parent can make a convenience ``exists()`` check look
    false.  Treat every stat error except an actual ``FileNotFoundError`` as an
    unverified removal so callers that own durable cleanup breadcrumbs fail closed.
    The mode-0700 quarantine protects against other UIDs; processes already
    running as the service UID are inside the filesystem authority boundary.
    """
    try:
        with _anchored_parent_beneath(allowed_root, path) as anchor:
            held_fd: int | None = None
            quarantine_fd: int | None = None
            captured_fd: int | None = None
            placeholder_fd: int | None = None
            quarantine_dir = f".plex-manager-cleanup-{secrets.token_hex(16)}"
            exchanged = False
            captured_removed = False
            try:
                held_fd = os.open(
                    anchor.name,
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=anchor.parent_fd,
                )
            except FileNotFoundError:
                return True
            try:
                before = os.fstat(held_fd)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or _payload_entries_identity([(".", before)]) != expected_identity
                ):
                    return False
                os.mkdir(quarantine_dir, mode=0o700, dir_fd=anchor.parent_fd)
                quarantine_fd = os.open(
                    quarantine_dir,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=anchor.parent_fd,
                )
                os.fchmod(quarantine_fd, 0o700)
                placeholder_fd = os.open(
                    "slot",
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=quarantine_fd,
                )
                placeholder_before = os.fstat(placeholder_fd)
                rename_exchange(
                    anchor.name,
                    "slot",
                    left_dir_fd=anchor.parent_fd,
                    right_dir_fd=quarantine_fd,
                )
                exchanged = True
                captured_fd = os.open(
                    "slot",
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=quarantine_fd,
                )
                captured = os.fstat(captured_fd)
                if (
                    not _same_regular_file_after_rename(before, captured)
                    or _placement_identity(os.fstat(captured_fd)) != _placement_identity(captured)
                    or _placement_identity(
                        os.stat("slot", dir_fd=quarantine_fd, follow_symlinks=False)
                    )
                    != _placement_identity(captured)
                ):
                    rename_exchange(
                        anchor.name,
                        "slot",
                        left_dir_fd=anchor.parent_fd,
                        right_dir_fd=quarantine_fd,
                    )
                    exchanged = False
                    # The restored private placeholder is no longer needed;
                    # remove it so the quarantine directory can be reclaimed.
                    os.unlink("slot", dir_fd=quarantine_fd)
                    return False
                os.unlink("slot", dir_fd=quarantine_fd)
                captured_removed = True
                rename_no_replace(
                    anchor.name,
                    "placeholder",
                    src_dir_fd=anchor.parent_fd,
                    dst_dir_fd=quarantine_fd,
                )
                placeholder_after = os.stat(
                    "placeholder",
                    dir_fd=quarantine_fd,
                    follow_symlinks=False,
                )
                if not _same_regular_file_after_rename(placeholder_before, placeholder_after):
                    rename_no_replace(
                        "placeholder",
                        anchor.name,
                        src_dir_fd=quarantine_fd,
                        dst_dir_fd=anchor.parent_fd,
                    )
                    return False
                os.unlink("placeholder", dir_fd=quarantine_fd)
            except (OSError, ValueError):
                if exchanged and not captured_removed and quarantine_fd is not None:
                    try:
                        rename_exchange(
                            anchor.name,
                            "slot",
                            left_dir_fd=anchor.parent_fd,
                            right_dir_fd=quarantine_fd,
                        )
                        os.unlink("slot", dir_fd=quarantine_fd)
                    except OSError:
                        pass
                raise
            finally:
                for fd in (placeholder_fd, captured_fd, quarantine_fd, held_fd):
                    if fd is not None:
                        with contextlib.suppress(OSError):
                            os.close(fd)
                with contextlib.suppress(OSError):
                    os.rmdir(quarantine_dir, dir_fd=anchor.parent_fd)
            if not _anchored_parent_is_current(anchor):
                return False
            try:
                os.stat(
                    anchor.name,
                    dir_fd=anchor.parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return True
            return False
    except (OSError, ValueError):
        return False


def _remove_quietly_many(paths: list[_PlacedPath], *, allowed_root: str) -> bool:
    """Best-effort unlink every owned path and confirm whether all are absent.

    Unlike the movie path's single ``dst``, a season import can place several
    episode files before its one combined scan fails; each is rolled back the
    same best-effort way.
    """
    all_removed = True
    for placed in paths:
        if not _remove_quietly(
            placed.path,
            expected_identity=placed.identity,
            allowed_root=allowed_root,
        ):
            all_removed = False
    return all_removed


def _classify_stored_path(
    path: Path,
) -> Literal["missing", "file", "directory", "other", "unverified"]:
    """Classify a breadcrumb without collapsing access errors into absence.

    ``Path.exists`` / ``is_dir`` return false for several uninspectable states.
    Cleanup pointers may be cleared only when ``lstat`` proves FileNotFound; a
    dangling symlink or inaccessible mount remains an unresolved obligation.
    """
    try:
        link_stat = path.lstat()
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "unverified"
    if stat.S_ISLNK(link_stat.st_mode):
        return "unverified"
    try:
        mode = path.stat().st_mode
    except (OSError, ValueError):
        return "unverified"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return "other"


def _trusted_breadcrumb_cleanup_state(
    library_root: str,
    path: Path,
    *,
    allow_empty_directory: bool = False,
) -> Literal["absent", "defer", "protected", "root_unavailable"]:
    """Classify cleanup only while its configured library authority is visible."""
    if not _library_root_is_visible(library_root):
        return "root_unavailable"
    observed_existing = False
    try:
        with _anchored_parent_beneath(library_root, path) as anchor:
            try:
                observed = os.stat(
                    anchor.name,
                    dir_fd=anchor.parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return "absent"
            observed_existing = True
            if not stat.S_ISDIR(observed.st_mode) or not allow_empty_directory:
                return "protected"
            directory_fd = os.open(
                anchor.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=anchor.parent_fd,
            )
            try:
                if not _same_inode(observed, os.fstat(directory_fd)):
                    return "protected"
                lock_state = clear_stale_publish_locks(Path(f"/proc/self/fd/{directory_fd}"))
                if lock_state == "pending":
                    return "defer"
                if lock_state == "protected":
                    return "protected"
                quarantine = f".plex-manager-empty-{secrets.token_hex(16)}"
                rename_no_replace(
                    anchor.name,
                    quarantine,
                    src_dir_fd=anchor.parent_fd,
                    dst_dir_fd=anchor.parent_fd,
                )
                try:
                    captured = os.stat(
                        quarantine,
                        dir_fd=anchor.parent_fd,
                        follow_symlinks=False,
                    )
                    if not _same_inode(observed, captured) or not _anchored_parent_is_current(
                        anchor
                    ):
                        rename_no_replace(
                            quarantine,
                            anchor.name,
                            src_dir_fd=anchor.parent_fd,
                            dst_dir_fd=anchor.parent_fd,
                        )
                        return "protected"
                    os.rmdir(quarantine, dir_fd=anchor.parent_fd)
                except (OSError, ValueError):
                    with contextlib.suppress(OSError):
                        rename_no_replace(
                            quarantine,
                            anchor.name,
                            src_dir_fd=anchor.parent_fd,
                            dst_dir_fd=anchor.parent_fd,
                        )
                    raise
            finally:
                with contextlib.suppress(OSError):
                    os.close(directory_fd)
            if not _anchored_parent_is_current(anchor):
                return "protected"
            try:
                os.stat(
                    anchor.name,
                    dir_fd=anchor.parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return "absent"
            return "protected"
    except FileNotFoundError:
        return "protected" if observed_existing else "absent"
    except (OSError, ValueError):
        return "protected"


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
    download_path: str | None = None,
    expected_failed_reason: str | None | object = _NO_FAILED_REASON_PREDICATE,
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
    if expected_failed_reason is _NO_FAILED_REASON_PREDICATE:
        blocked = await download_repo.update_status_if_in(
            download_id,
            DownloadState.ImportBlocked.value,
            _RESUMABLE,
            failed_reason=reason,
            clear_download_path=clear_download_path,
            download_path=download_path,
        )
    else:
        blocked = await download_repo.update_status_if_in(
            download_id,
            DownloadState.ImportBlocked.value,
            _RESUMABLE,
            failed_reason=reason,
            clear_download_path=clear_download_path,
            download_path=download_path,
            require_failed_reason=cast(str | None, expected_failed_reason),
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
    sources: list[_SourceCandidate],
    parser: ParserPort,
    profile: QualityProfile,
    tv_root: str,
) -> _TvImportPlan | _TvImportFailure:
    validation = validate_season_import(
        [
            VideoFile(relative_path=source.relative_path, size_bytes=source.size)
            for source in sources
        ],
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

    source_by_rel = {source.relative_path: source for source in sources}
    by_relative: dict[PurePosixPath, EpisodeImportResult] = {}
    for result in validation.accepted:
        source = source_by_rel[result.video.relative_path]
        ext = os.path.splitext(source.path)[1].lstrip(".")
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
    return _TvImportPlan(target, season_dir, source_by_rel, by_relative, validation.accepted)


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
    seasons: tuple[int, ...] = (),
    owned_placement: Path | None = None,
    owned_placement_identity: str | None = None,
    owned_placement_root: str | None = None,
) -> DownloadRecord | None:
    queue_service._begin_reconcile_removal_guard(download_id)  # pyright: ignore[reportPrivateUsage]
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

        # Winning the CAS out of a resumable state atomically claims this failure: a
        # racing operator ``mark_failed`` either loses its own resumable-state CAS or
        # blocks on this row's write lock until this transaction commits and then loses.
        # For a crash-resumed movie whose ``download_path`` breadcrumb points at an
        # already-placed library file, remove that file and clear the breadcrumb in the
        # SAME transaction as the ``failed_pending`` transition. Committing the
        # transition first and unlinking in a later commit (the prior ordering) left a
        # window where a crash stranded a ``failed_pending`` row whose Phase-C heal
        # completes and re-arms the request while an unmanaged copy is still in the
        # library — the completion path never consumes ``download_path``. Folding both
        # into one commit closes that window: a crash before it rolls the row back to
        # ``Importing`` (still resumable), so a later cycle re-detects the unsafe payload
        # and retries the idempotent unlink.
        placement_removed = False
        if (
            owned_placement is not None
            and owned_placement_identity is not None
            and owned_placement_root is not None
        ):
            placement_removed = await asyncio.to_thread(
                _remove_quietly,
                owned_placement,
                expected_identity=owned_placement_identity,
                allowed_root=owned_placement_root,
            )
        if owned_placement is not None:
            if not placement_removed:
                # The failed-pending CAS is still uncommitted. Roll it back before
                # parking the row so the durable breadcrumb and unsafe torrent both
                # remain available for an operator retry after permissions/mount state
                # are repaired. Never clear the only cleanup pointer or delete the
                # torrent while the placed library file may still exist.
                await session.rollback()
                await _block(
                    session,
                    download_repo,
                    download_id,
                    (
                        f"{reason}; {_OWNED_PLACEMENT_CLEANUP_FAILURE_FRAGMENT}; "
                        f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                    ),
                    request_id=request_id,
                    season=season,
                    seasons=seasons,
                )
                return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
            _invalidate_payload_placement_ownership(
                session,
                torrent_hash,
                os.fspath(owned_placement),
                tmdb_id=None,
            )
            cleared = await download_repo.update_status_if_in(
                download_id,
                DownloadState.FailedPending.value,
                frozenset({DownloadState.FailedPending.value}),
                clear_download_path=True,
                require_failed_reason=reason,
            )
            if not cleared:
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

        removed_ok = await purge_service.remove_torrent(
            qbt,
            torrent_hash,
            context="an unsafe torrent payload rejection",
            extra={
                "torrent_hash": safe_text(torrent_hash),
                "download_id": safe_int(download_id),
                "request_id": safe_int(request_id),
            },
        )
        if not removed_ok:
            # Security rejections fail closed: keep the durable failed_pending row
            # active so reconcile retries the owed torrent/data removal. Terminalizing
            # here would drop the row from active reconciliation and re-arm a
            # replacement while the unsafe payload remains in qBittorrent.
            _logger.warning(
                "keeping unsafe payload download %s pending until torrent removal succeeds",
                safe_int(download_id),
            )
            return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
        observed_failed_reason = reason
        done_marker = queue_service._reconcile_removal_done_marker(reason)  # pyright: ignore[reportPrivateUsage]
        restamped = await download_repo.update_status_if_in(
            download_id,
            DownloadState.FailedPending.value,
            frozenset({DownloadState.FailedPending.value}),
            failed_reason=done_marker,
            require_failed_reason=reason,
        )
        if not restamped:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
        try:
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            _logger.warning(
                "could not persist the unsafe payload removal outcome for download %s; "
                "the final failure commit will retry with the original payload reason",
                safe_int(download_id),
            )
        else:
            observed_failed_reason = done_marker

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
            require_failed_reason=observed_failed_reason,
        )
        if not completed:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
        # Mirror queue_service Phase C: resolve every still-unresolved durable scope row
        # to a terminal ``failed`` in the same transaction as the terminal advance, so a
        # multi-season (#173) row never leaves stale ``active`` scopes behind.
        await queue_service._mark_download_scopes_terminal(  # pyright: ignore[reportPrivateUsage]
            session, download_id, RequestStatus.failed.value
        )

        await SqlBlocklistRepository(session).create(
            source_title=source_title,
            reason=BlocklistReason.failed.value,
            tmdb_id=request.tmdb_id if request is not None else None,
            torrent_hash=torrent_hash,
            indexer=indexer,
            media_type=(
                request.media_type
                if request is not None
                else ("tv" if season is not None or seasons else "movie")
            ),
        )
        if request is not None:
            # ``seasons`` (multi-scope #173 rows) takes precedence over the scalar
            # ``season``: every attached season is re-armed, matching queue_service
            # Phase C's per-scope re-arm.
            target_seasons = seasons or ((season,) if season is not None else ())
            if target_seasons:
                season_repo = SqlSeasonRequestRepository(session)
                for target_season in target_seasons:
                    row = await season_repo.ensure(
                        request_id, target_season, status=RequestStatus.pending.value
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
        queue_service._end_reconcile_removal_guard(download_id)  # pyright: ignore[reportPrivateUsage]


def _expected_movie_breadcrumb_path(
    download_path: str, movies_root: str, title: str, year: int | None
) -> Path | None:
    """Bind a stored movie breadcrumb to its one deterministic destination."""
    path = Path(download_path)
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return None
    expected = Path(movies_root) / plex_movie_relative_path(title, year, path.suffix.lstrip("."))
    return _bind_expected_library_breadcrumb(download_path, movies_root, (expected,))


def _expected_tv_breadcrumb_path(
    download_path: str,
    tv_root: str,
    title: str,
    year: int | None,
    seasons: tuple[int, ...],
) -> Path | None:
    """Bind a stored TV breadcrumb to one attached deterministic season path."""
    expected = tuple(
        Path(tv_root) / plex_tv_season_relative_dir(title, year, season) for season in seasons
    )
    return _bind_expected_library_breadcrumb(download_path, tv_root, expected)


def _expected_movie_breadcrumb_for_unsafe_rollback(
    status: str,
    download_path: str | None,
    movies_root: str,
    title: str,
    year: int | None,
) -> Path | None:
    if download_path is None or status not in _RESUMABLE:
        return None
    # Only a breadcrumb at the exact expected destination is provably OUR placement.
    # Deleting anything else under the (possibly re-pointed) movies root risks
    # destroying an unrelated title's file — nothing beats maybe-deleting someone
    # else's file, so an unprovable breadcrumb is left on disk for the operator.
    return _expected_movie_breadcrumb_path(download_path, movies_root, title, year)


async def _resume_breadcrumbed_tv_import(
    *,
    session: AsyncSession,
    download_repo: SqlDownloadRepository,
    request: RequestRecord,
    season: int,
    download_id: int,
    torrent_hash: str,
    fs: FileSystemPort,
    library: LibraryPort,
    parser: ParserPort,
    profile: QualityProfile,
    tv_root: str,
    download_path: str,
) -> DownloadRecord | None:
    expected_season_dir = Path(tv_root) / plex_tv_season_relative_dir(
        request.title, request.year, season
    )
    season_dir = Path(download_path)
    root_real = os.path.realpath(tv_root)
    season_dir_real = os.path.realpath(season_dir)
    expected_real = os.path.realpath(expected_season_dir)
    if not _is_within(root_real, season_dir_real) or season_dir_real != expected_real:
        await _block(
            session,
            download_repo,
            download_id,
            "stored import breadcrumb is not the expected tv season directory",
            request_id=request.id,
            season=season,
        )
        return await download_repo.get_by_hash(torrent_hash)
    breadcrumb_kind = await asyncio.to_thread(_classify_stored_path, season_dir)
    if breadcrumb_kind == "missing":
        await _block(
            session,
            download_repo,
            download_id,
            "stored import breadcrumb is not visible inside the container",
            request_id=request.id,
            season=season,
            clear_download_path=True,
        )
        return await download_repo.get_by_hash(torrent_hash)
    if breadcrumb_kind != "directory":
        reason = (
            "stored import breadcrumb could not be verified inside the container"
            if breadcrumb_kind == "unverified"
            else "stored import breadcrumb is not a tv season directory"
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

    if not purge_service.begin_placement(str(season_dir)):
        _logger.info(
            "deferring import of download %s: a purge is deleting this path; "
            "will retry next import cycle",
            safe_int(download_id),
            extra={"request_id": safe_int(request.id), "season": safe_int(season)},
        )
        return await download_repo.get_by_hash(torrent_hash)
    try:
        imported = [
            (os.path.basename(abs_path), PurePosixPath(os.path.relpath(abs_path, tv_root)))
            for abs_path, _size, _rel in await asyncio.to_thread(
                fs.list_video_files, os.fspath(season_dir)
            )
        ]
        if not imported:
            await _block(
                session,
                download_repo,
                download_id,
                "stored import breadcrumb contains no visible video files",
                request_id=request.id,
                season=season,
            )
            return await download_repo.get_by_hash(torrent_hash)

        try:
            await library.trigger_scan(str(season_dir), "tv")
        except (PlexLibraryError, PlexAuthError) as exc:
            await _block(
                session,
                download_repo,
                download_id,
                f"plex scan failed: {type(exc).__name__}",
                request_id=request.id,
                season=season,
            )
            return await download_repo.get_by_hash(torrent_hash)

        # Plex scanning is an awaited external call. Re-bind the durable proof to
        # what is on disk now so a file deletion/replacement during that gap cannot
        # turn a previously validated season into a status-less finalization.
        if not await _payload_manifest_was_validated(
            session,
            torrent_hash,
            str(season_dir),
            allowed_root=tv_root,
        ):
            await _block(
                session,
                download_repo,
                download_id,
                _PLACEMENT_IDENTITY_CHANGED_REASON,
                request_id=request.id,
                season=season,
            )
            return await download_repo.get_by_hash(torrent_hash)

        finalized = await download_repo.update_status_if_in(
            download_id,
            DownloadState.Imported.value,
            frozenset({DownloadState.Importing.value}),
            download_path=str(season_dir),
            clear_failed_reason=True,
        )
        if not finalized:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash)
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
        await season_request_service.set_library_path(
            session,
            media_request_id=request.id,
            season_number=season,
            library_path=str(season_dir),
        )
        # Persist installed quality like the normal TV finalize paths do. The placed
        # episode filenames are Plex-shaped (``Series - SxxEyy.ext``) and carry no
        # quality tokens, so re-parsing them cannot recover the source quality; the
        # grabbed ``release_title`` is the same string the grab decision ranked and is
        # the truthful basis available at crash-resume time. When it is absent the
        # column is honestly left null (as before) rather than stamped with a guess.
        download_row = await session.get(Download, download_id)
        release_title = download_row.release_title if download_row is not None else None
        if release_title:
            parsed = parser.parse(release_title)
            installed_quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
            await season_request_service.set_installed_quality(
                session,
                media_request_id=request.id,
                season_number=season,
                quality_id=installed_quality.id,
                profile_index=profile.get_index(installed_quality.id),
            )
        await season_request_service.mark_completed(
            session, media_request_id=request.id, season_number=season
        )
        await session.commit()
        return await download_repo.get_by_hash(torrent_hash)
    finally:
        purge_service.end_placement(str(season_dir))


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
    seasons: tuple[int, ...] = (),
    owned_placement: Path | None = None,
    owned_placement_identity: str | None = None,
    owned_placement_root: str | None = None,
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
        # Match queue-side validation: a transient manifest endpoint outage defers
        # without changing durable state. ImportPending/Importing remains auto-
        # drainable, while an existing manual-cleanup block keeps its stronger reason.
        # Turning a one-call outage into ImportBlocked permanently stranded otherwise
        # safe unattended imports because the background cycle does not drain generic
        # blocked rows.
        _logger.warning(
            "download %s: could not validate torrent payload manifest (%s); deferring import",
            safe_int(download_id),
            type(exc).__name__,
            extra={"request_id": safe_int(request_id), "torrent_hash": safe_text(torrent_hash)},
        )
        await session.rollback()
        return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
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
            f"{reason}; {_MANUAL_CLEANUP_BREADCRUMB_REASON}",
            request_id=request_id,
            season=season,
            seasons=seasons,
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
        seasons=seasons,
        owned_placement=owned_placement,
        owned_placement_identity=owned_placement_identity,
        owned_placement_root=owned_placement_root,
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
    dst = _expected_movie_breadcrumb_path(download_path, movies_root, request.title, request.year)
    # Require the breadcrumb to be the SAME deterministic destination the normal movie
    # import would place this request at — not merely any video under the root. A bare
    # ``_is_within`` check would finalize the request against an unrelated title if the
    # movies root were later widened to a parent path, or against a stale breadcrumb
    # pointing at another file under the root, scanning/completing the wrong media. This
    # mirrors the TV resume path, which requires the exact expected season directory.
    if dst is None:
        await _block(
            session,
            download_repo,
            download_id,
            "stored import breadcrumb is not the expected movie destination",
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)
    breadcrumb_kind = await asyncio.to_thread(_classify_stored_path, dst)
    if breadcrumb_kind == "missing":
        _invalidate_payload_placement_ownership(
            session,
            torrent_hash,
            str(dst),
            tmdb_id=request.tmdb_id,
        )
        await _block(
            session,
            download_repo,
            download_id,
            "stored import breadcrumb is not visible inside the container",
            request_id=request.id,
            clear_download_path=True,
        )
        return await download_repo.get_by_hash(torrent_hash)
    if breadcrumb_kind != "file":
        reason = (
            "stored import breadcrumb could not be verified inside the container"
            if breadcrumb_kind == "unverified"
            else "stored import breadcrumb is not a regular movie file"
        )
        await _block(
            session,
            download_repo,
            download_id,
            reason,
            request_id=request.id,
        )
        return await download_repo.get_by_hash(torrent_hash)

    if not purge_service.begin_placement(str(dst)):
        _logger.info(
            "deferring import of download %s: a purge is deleting this path; "
            "will retry next import cycle",
            safe_int(download_id),
            extra={"request_id": safe_int(request.id)},
        )
        return await download_repo.get_by_hash(torrent_hash)
    try:
        try:
            await library.trigger_scan(str(dst.parent), "movie")
        except (PlexLibraryError, PlexAuthError) as exc:
            # qBittorrent is already missing, so this breadcrumbed library file may
            # be the only recoverable copy. Preserve it and its durable pointer on a
            # transient Plex outage; an ImportBlocked retry below re-enters this scan
            # path, matching the TV breadcrumb-resume posture.
            await _block(
                session,
                download_repo,
                download_id,
                f"plex scan failed: {type(exc).__name__}",
                request_id=request.id,
            )
            return await download_repo.get_by_hash(torrent_hash)

        # The attested file may have been replaced while Plex was scanning. Never
        # finalize a status-less breadcrumb unless its identity still matches the
        # current lifecycle's validation proof after that awaited boundary.
        if not await _payload_manifest_was_validated(
            session,
            torrent_hash,
            str(dst),
            allowed_root=movies_root,
        ):
            await _block(
                session,
                download_repo,
                download_id,
                _PLACEMENT_IDENTITY_CHANGED_REASON,
                request_id=request.id,
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
    finally:
        purge_service.end_placement(str(dst))


def _payload_placement_fingerprint(download_path: str) -> str:
    normalized = os.path.normcase(os.path.abspath(os.path.normpath(download_path)))
    return hashlib.sha256(os.fsencode(normalized)).hexdigest()


def _payload_entries_identity(entries: list[tuple[str, os.stat_result]]) -> str:
    digest = hashlib.sha256()
    for relative, entry_stat in entries:
        digest.update(os.fsencode(relative))
        digest.update(b"\0")
        digest.update(
            (
                f"{entry_stat.st_dev}:{entry_stat.st_ino}:{entry_stat.st_size}:"
                f"{entry_stat.st_mtime_ns}:{entry_stat.st_ctime_ns}:"
                f"{stat.S_IFMT(entry_stat.st_mode)}"
            ).encode()
        )
        digest.update(b"\0")
    return digest.hexdigest()


def _payload_content_identity(download_path: str, *, allowed_root: str) -> str | None:
    """Fingerprint the current non-symlink file/tree identity without reading media.

    Device/inode/size/mtime plus the complete relative tree catches replacement,
    deletion, addition, and in-place modification while avoiding a multi-gigabyte
    digest on every resume. Any access error or special/symlink entry fails closed.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    path_only = getattr(os, "O_PATH", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory or not path_only or not os.path.isdir("/proc/self/fd"):
        return None
    try:
        allowed_root_abs = os.path.abspath(os.path.normpath(allowed_root))
        candidate_abs = os.path.abspath(os.path.normpath(download_path))
        lexical_prefix = allowed_root_abs.rstrip(os.sep) + os.sep
        if candidate_abs == allowed_root_abs or not candidate_abs.startswith(lexical_prefix):
            return None
        relative_parts = Path(os.path.relpath(candidate_abs, allowed_root_abs)).parts
        if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
            return None
        allowed_root_real = os.path.realpath(allowed_root_abs)
        candidate_real = os.path.realpath(candidate_abs)
        expected_real = os.path.normpath(os.path.join(allowed_root_real, *relative_parts))
    except (OSError, ValueError):
        return None
    allowed_prefix = allowed_root_real.rstrip(os.sep) + os.sep
    if (
        allowed_root_real != allowed_root_abs
        or candidate_real != expected_real
        or candidate_real == allowed_root_real
        or not candidate_real.startswith(allowed_prefix)
    ):
        return None

    root_fd: int | None = None
    try:
        root_fd = os.open(candidate_real, path_only | nofollow | cloexec)
        root_stat = os.fstat(root_fd)
        if os.path.realpath(f"/proc/self/fd/{root_fd}") != candidate_real:
            return None
        entries: list[tuple[str, os.stat_result]] = [(".", root_stat)]
        if stat.S_ISREG(root_stat.st_mode):
            pass
        elif stat.S_ISDIR(root_stat.st_mode):
            visited: set[tuple[int, int]] = {(root_stat.st_dev, root_stat.st_ino)}

            def _walk(directory_path_fd: int, relative_parent: PurePosixPath) -> bool:
                read_fd = os.open(
                    ".",
                    os.O_RDONLY | directory | nofollow | cloexec,
                    dir_fd=directory_path_fd,
                )
                try:
                    with os.scandir(read_fd) as iterator:
                        names = sorted(entry.name for entry in iterator)
                    child_prefix = f"/proc/self/fd/{read_fd}/"
                    for name in names:
                        child_path = os.path.normpath(os.path.join(child_prefix, name))
                        if not child_path.startswith(child_prefix):
                            return False
                        child_fd = os.open(child_path, path_only | nofollow | cloexec)
                        try:
                            child_stat = os.fstat(child_fd)
                            relative = relative_parent / name
                            if stat.S_ISREG(child_stat.st_mode):
                                entries.append((relative.as_posix(), child_stat))
                                continue
                            if not stat.S_ISDIR(child_stat.st_mode):
                                return False
                            key = (child_stat.st_dev, child_stat.st_ino)
                            if key in visited:
                                return False
                            visited.add(key)
                            entries.append((relative.as_posix(), child_stat))
                            if not _walk(child_fd, relative):
                                return False
                        finally:
                            os.close(child_fd)
                    return True
                finally:
                    os.close(read_fd)

            if not _walk(root_fd, PurePosixPath()):
                return None
        else:
            return None

        # The traversal stayed on stable descriptors. Re-bind those descriptors to
        # the configured namespace before granting durable attestation authority.
        if (
            os.path.realpath(allowed_root_abs) != allowed_root_real
            or os.path.realpath(candidate_abs) != candidate_real
            or os.path.realpath(f"/proc/self/fd/{root_fd}") != candidate_real
        ):
            return None
        return _payload_entries_identity(entries)
    except (OSError, ValueError):
        return None
    finally:
        if root_fd is not None:
            with contextlib.suppress(OSError):
                os.close(root_fd)


def _payload_validation_attestation(
    download_path: str,
    *,
    placement_owned: bool,
    content_identity: str,
) -> str:
    """Format a caller-proven validation marker without touching the filesystem."""
    if not content_identity:
        raise ValueError("content_identity must not be empty")
    ownership = "placed" if placement_owned else "adopted"
    return (
        f"{_PAYLOAD_VALIDATED_IMPORT_HISTORY_PREFIX}"
        f"placement={_payload_placement_fingerprint(download_path)} "
        f"identity={content_identity} "
        f"ownership={ownership}:"
    )


async def _latest_payload_attestation(
    session: AsyncSession, torrent_hash: str, download_path: str
) -> str | None:
    latest_grab_id = (
        await session.execute(
            select(func.max(DownloadHistory.id)).where(
                DownloadHistory.torrent_hash == torrent_hash,
                DownloadHistory.event_type == DownloadHistoryEvent.grabbed,
            )
        )
    ).scalar_one_or_none()
    path_prefix = (
        f"{_PAYLOAD_VALIDATED_IMPORT_HISTORY_PREFIX}"
        f"placement={_payload_placement_fingerprint(download_path)} "
    )
    stmt = (
        select(DownloadHistory.message)
        .where(
            DownloadHistory.torrent_hash == torrent_hash,
            DownloadHistory.event_type == DownloadHistoryEvent.import_started,
            DownloadHistory.message.startswith(path_prefix),
        )
        .order_by(DownloadHistory.id.desc())
        .limit(1)
    )
    if latest_grab_id is not None:
        stmt = stmt.where(DownloadHistory.id > latest_grab_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _payload_manifest_was_validated(
    session: AsyncSession,
    torrent_hash: str,
    download_path: str,
    *,
    allowed_root: str,
) -> bool:
    """Whether this torrent/path pair durably passed the current payload gate.

    The proof is written only after the destination was placed or content-adopted,
    in the same commit as its breadcrumb. It is bound to both the current policy
    version, current grab lifecycle, exact destination, and current file/tree
    identity; a pre-security crash row, older-policy proof, replaced file, missing
    episode, or prior lifecycle therefore cannot authorize a status-less resume.
    History also avoids abusing ``failed_reason``, which the UI renders as an error.
    """
    latest = await _latest_payload_attestation(session, torrent_hash, download_path)
    current_identity = await asyncio.to_thread(
        _payload_content_identity,
        download_path,
        allowed_root=allowed_root,
    )
    return (
        latest is not None
        and current_identity is not None
        and latest.startswith(
            f"{_PAYLOAD_VALIDATED_IMPORT_HISTORY_PREFIX}"
            f"placement={_payload_placement_fingerprint(download_path)} "
            f"identity={current_identity} "
        )
    )


async def _payload_placement_was_owned(
    session: AsyncSession,
    torrent_hash: str,
    download_path: str,
    *,
    current_identity: str,
) -> bool:
    """Whether current identity matches this lifecycle's latest owned placement."""
    latest = await _latest_payload_attestation(session, torrent_hash, download_path)
    return latest is not None and latest.startswith(
        _payload_validation_attestation(
            download_path,
            placement_owned=True,
            content_identity=current_identity,
        )
    )


def _invalidate_payload_placement_ownership(
    session: AsyncSession,
    torrent_hash: str,
    download_path: str,
    *,
    tmdb_id: int | None,
) -> None:
    """Make a confirmed rollback the latest path-specific ownership fact."""
    invalidation = _payload_validation_attestation(
        download_path,
        placement_owned=False,
        content_identity="cleared",
    )
    session.add(
        DownloadHistory(
            tmdb_id=tmdb_id,
            torrent_hash=torrent_hash,
            event_type=DownloadHistoryEvent.import_started,
            source_title=None,
            message=(f"{invalidation} placement ownership cleared after confirmed rollback"),
        )
    )


def _can_resume_breadcrumb_without_client_status(
    download_status: str, failed_reason: str | None, *, payload_validated: bool
) -> bool:
    # A breadcrumb alone is not proof that its torrent manifest passed the new
    # payload gate: an Importing row can predate this security release. Only rows
    # durably attested after a successful manifest check may finalize without client state.
    if not payload_validated:
        return False
    if download_status == DownloadState.Importing.value:
        return True
    return (
        download_status == DownloadState.ImportBlocked.value
        and failed_reason is not None
        and failed_reason.startswith(_PLEX_SCAN_FAILED_REASON_PREFIX)
    )


def _is_manual_cleanup_breadcrumb(
    download_status: str, download_path: str | None, failed_reason: str | None
) -> bool:
    return (
        download_status == DownloadState.ImportBlocked.value
        and download_path is not None
        and failed_reason is not None
        and not failed_reason.startswith(_UNVALIDATED_BREADCRUMB_REASON_PREFIX)
        and _MANUAL_CLEANUP_BREADCRUMB_REASON in failed_reason
    )


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
        all_scope_records = [
            scope
            for scope in await download_repo.list_scopes(download_id)
            if scope.media_request_id == request.id and scope.season is not None
        ]
        scope_records = [scope for scope in all_scope_records if scope.status != "imported"]
        known_scope_seasons = tuple(
            dict.fromkeys(cast(int, scope.season) for scope in all_scope_records)
        )
        imported_scope_seasons = tuple(
            dict.fromkeys(
                cast(int, scope.season) for scope in all_scope_records if scope.status == "imported"
            )
        )
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
                download_status=row.status,
                download_path=row.download_path,
                failed_reason=row.failed_reason,
                known_scope_seasons=known_scope_seasons,
                imported_scope_seasons=imported_scope_seasons,
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
            failed_reason=row.failed_reason,
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
    row_status = row.status
    row_failed_reason = row.failed_reason
    expected_breadcrumb = (
        _expected_movie_breadcrumb_path(
            download_path,
            effective_movies_root,
            request.title,
            request.year,
        )
        if download_path is not None
        else None
    )
    if expected_breadcrumb is not None:
        # Filesystem work uses the deterministic destination, never the persisted
        # spelling that merely proved lexically equal to it.
        download_path = os.fspath(expected_breadcrumb)
    breadcrumb_cleanup: Literal["absent", "defer", "protected", "root_unavailable"] | None = (
        await asyncio.to_thread(
            _trusted_breadcrumb_cleanup_state,
            effective_movies_root,
            expected_breadcrumb,
        )
        if expected_breadcrumb is not None
        else None
    )
    if breadcrumb_cleanup == "absent" and expected_breadcrumb is not None:
        _invalidate_payload_placement_ownership(
            session,
            torrent_hash,
            os.fspath(expected_breadcrumb),
            tmdb_id=request.tmdb_id,
        )
        cleared = await download_repo.update_status_if_in(
            download_id,
            row_status,
            frozenset({row_status}),
            clear_download_path=True,
            require_failed_reason=row_failed_reason,
        )
        if not cleared:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
        await session.commit()
        download_path = None
        expected_breadcrumb = None
        breadcrumb_cleanup = None
    if status is None:
        if download_path is not None and expected_breadcrumb is None:
            if _is_manual_cleanup_breadcrumb(row.status, download_path, row.failed_reason):
                # An unbound cleanup pointer remains parked without a filesystem probe.
                return await download_repo.get_by_hash(torrent_hash)
            await _block(
                session,
                download_repo,
                download_id,
                "stored import breadcrumb is not the expected movie destination",
                request_id=request.id,
                expected_failed_reason=row.failed_reason,
            )
            return await download_repo.get_by_hash(torrent_hash)
        if breadcrumb_cleanup == "root_unavailable":
            return await download_repo.get_by_hash(torrent_hash)
        if _is_manual_cleanup_breadcrumb(row.status, download_path, row.failed_reason):
            # The trusted destination still exists, so retain its cleanup breadcrumb.
            return await download_repo.get_by_hash(torrent_hash)
        payload_validated = (
            expected_breadcrumb is not None
            and await _payload_manifest_was_validated(
                session,
                torrent_hash,
                os.fspath(expected_breadcrumb),
                allowed_root=effective_movies_root,
            )
        )
        if expected_breadcrumb is not None and _can_resume_breadcrumb_without_client_status(
            row.status, row.failed_reason, payload_validated=payload_validated
        ):
            if row.status == DownloadState.ImportBlocked.value:
                resumed = await download_repo.update_status_if_in(
                    download_id,
                    DownloadState.Importing.value,
                    frozenset({DownloadState.ImportBlocked.value}),
                    clear_failed_reason=True,
                    require_failed_reason=row.failed_reason,
                )
                if not resumed:
                    await session.rollback()
                    return await download_repo.get_by_hash(torrent_hash)
                await session.commit()
            return await _resume_breadcrumbed_movie_import(
                session=session,
                download_repo=download_repo,
                request_repo=request_repo,
                library=library,
                download_id=download_id,
                torrent_hash=torrent_hash,
                request=request,
                movies_root=effective_movies_root,
                download_path=os.fspath(expected_breadcrumb),
            )
        if download_path is not None and not payload_validated:
            # A pre-security-release breadcrumb has no durable proof that its torrent
            # manifest passed this gate. Park visibly (preserving the path) instead of
            # lying as perpetually Downloading; run_import_cycle auto-retries this exact
            # reason so a returning client can validate and resume, while mark-failed
            # remains a legal operator escape hatch from ImportBlocked.
            await _block(
                session,
                download_repo,
                download_id,
                _UNVALIDATED_BREADCRUMB_REASON,
                request_id=request.id,
                expected_failed_reason=row.failed_reason,
            )
            return await download_repo.get_by_hash(torrent_hash)
        if download_path is not None:
            # A current-policy proof does not make every blocked state resumable.
            # Preserve an accurate destination-conflict/validation reason rather
            # than relabeling it as unvalidated and auto-retrying it forever.
            return await download_repo.get_by_hash(torrent_hash)
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no status for payload validation",
            request_id=request.id,
            expected_failed_reason=row.failed_reason,
        )
        return await download_repo.get_by_hash(torrent_hash)
    if _payload_manifest_is_complete(status):
        owned_candidate = _expected_movie_breadcrumb_for_unsafe_rollback(
            row_status,
            download_path,
            effective_movies_root,
            request.title,
            request.year,
        )
        owned_placement = None
        owned_placement_identity = None
        if owned_candidate is not None:
            candidate_identity = await asyncio.to_thread(
                _payload_content_identity,
                os.fspath(owned_candidate),
                allowed_root=effective_movies_root,
            )
            if candidate_identity is not None and await _payload_placement_was_owned(
                session,
                torrent_hash,
                os.fspath(owned_candidate),
                current_identity=candidate_identity,
            ):
                owned_placement = owned_candidate
                owned_placement_identity = candidate_identity
        has_placement_breadcrumb = download_path is not None
        rejected = await _reject_unsafe_payload_if_reported(
            session=session,
            download_repo=download_repo,
            qbt=qbt,
            download_id=download_id,
            torrent_hash=torrent_hash,
            status=status,
            request_id=request.id,
            owned_placement=owned_placement,
            owned_placement_identity=owned_placement_identity,
            owned_placement_root=effective_movies_root,
            block_existing_breadcrumb=has_placement_breadcrumb and owned_placement is None,
        )
        if rejected is not None:
            return rejected
        if breadcrumb_cleanup == "root_unavailable":
            return await download_repo.get_by_hash(torrent_hash)
    if not _is_settled_for_import(status):
        if download_path is not None:
            # ``download_path`` is a durable Plex-library placement, not a qBt
            # source path. Keep every placement-bearing row in its protected import
            # state until the immutable manifest can be adjudicated; demoting it to
            # Downloading would expose it to automatic torrent removal/re-search.
            return await download_repo.get_by_hash(torrent_hash)
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
    if download_path is not None and expected_breadcrumb is None:
        await _block(
            session,
            download_repo,
            download_id,
            (
                "stored movie import breadcrumb does not match the current destination; "
                f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
            ),
            request_id=request.id,
            expected_failed_reason=row.failed_reason,
        )
        return await download_repo.get_by_hash(torrent_hash)
    try:
        resolved = _resolve_content(status, download_path)
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
    content = visible_content.path
    sources = await asyncio.to_thread(
        _resolve_sources,
        fs,
        content,
        source_root=visible_content.source_root,
        source_root_identity=visible_content.source_root_identity,
    )
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
        [
            VideoFile(relative_path=source.relative_path, size_bytes=source.size)
            for source in sources
        ],
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

    # Map the validator's chosen feature back to its identity-bound source — exactly
    # the ``source_by_rel`` shape the TV path uses — so placement copies the file the
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
    source_by_rel = {source.relative_path: source for source in sources}
    source = source_by_rel[validation.video.relative_path]
    ext = os.path.splitext(source.path)[1].lstrip(".")
    relative = plex_movie_relative_path(request.title, request.year, ext)
    dst = Path(effective_movies_root) / relative
    if download_path is not None and Path(download_path) != dst:
        await _block(
            session,
            download_repo,
            download_id,
            (
                "stored movie import breadcrumb does not match the current destination; "
                f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
            ),
            request_id=request.id,
            expected_failed_reason=row.failed_reason,
        )
        return await download_repo.get_by_hash(torrent_hash)

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
            download_path=str(dst),
        )
        if not claimed:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash)
        import_started_history = DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=torrent_hash,
            event_type=DownloadHistoryEvent.import_started,
            # NULL on purpose: queue_service._source_title_for returns the latest
            # non-null history source_title for the blocklist, and the import file
            # basename must NOT shadow the grabbed RELEASE title. Keep the basename in
            # ``message`` only.
            source_title=None,
            message=f"importing {os.path.basename(source.path)} to {relative}",
        )
        session.add(import_started_history)
        await session.commit()

        try:
            placed, observed_identity = await asyncio.to_thread(
                _place_file_with_identity,
                fs,
                source,
                dst,
                allowed_root=effective_movies_root,
            )
        except FileExistsError as exc:
            # A pre-existing, differently-sized file at the destination (a user's file,
            # or a stale partial) — surfaced as a conflict, never overwritten.
            await _block(session, download_repo, download_id, str(exc), request_id=request.id)
            return await download_repo.get_by_hash(torrent_hash)
        except OSError as exc:
            failed_destination_kind = await asyncio.to_thread(_classify_stored_path, dst)
            reason = f"import copy failed: {type(exc).__name__}"
            if failed_destination_kind != "missing":
                reason = (
                    f"{reason}; {_OWNED_PLACEMENT_CLEANUP_FAILURE_FRAGMENT}; "
                    f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                )
            await _block(
                session,
                download_repo,
                download_id,
                reason,
                request_id=request.id,
            )
            return await download_repo.get_by_hash(torrent_hash)

        # Bind the current-policy manifest proof to this exact destination only AFTER
        # placement/content-adoption succeeds. The breadcrumb and proof share one
        # commit, so a crash between the earlier claim and the content check cannot
        # make a stale path eligible for status-less resume. ``ownership=placed`` is
        # distinct from an idempotently adopted identical file: only the former can
        # later authorize automatic rollback deletion.
        await download_repo.update_status(
            download_id, DownloadState.Importing.value, download_path=str(dst)
        )
        content_identity = await asyncio.to_thread(
            _payload_content_identity,
            str(dst),
            allowed_root=effective_movies_root,
        )
        if (
            observed_identity is None
            or content_identity is None
            or content_identity != observed_identity
        ):
            await _block(
                session,
                download_repo,
                download_id,
                _PLACEMENT_IDENTITY_CHANGED_REASON,
                request_id=request.id,
                download_path=str(dst),
            )
            return await download_repo.get_by_hash(torrent_hash)
        placement_owned = placed or (
            await _payload_placement_was_owned(
                session,
                torrent_hash,
                str(dst),
                current_identity=content_identity,
            )
        )
        validation_attestation = _payload_validation_attestation(
            str(dst),
            placement_owned=placement_owned,
            content_identity=content_identity,
        )
        session.add(
            DownloadHistory(
                tmdb_id=request.tmdb_id,
                torrent_hash=torrent_hash,
                event_type=DownloadHistoryEvent.import_started,
                source_title=None,
                message=(
                    f"{validation_attestation} "
                    f"validated placement for {os.path.basename(source.path)} at {relative}"
                ),
            )
        )
        await session.commit()

        # Targeted Plex scan of the movie folder — the partial scan the prototype never
        # did. movies_root is a Plex library location (the picker guarantees the path↔
        # section match), so a scan failure here is a transient Plex error, not a wrong
        # path. Roll the file back before blocking so a later reject / re-search can't
        # orphan it (the retry re-places it).
        #
        # OWNERSHIP RULE (Codex PR #21): a file at dst may be rolled back ONLY on proof
        # it is ours — THIS invocation placed it (``placed``), or the latest
        # path-bound attestation says a prior attempt placed it. The breadcrumb alone
        # is only a cleanup obligation, never deletion authority. NEVER infer ownership
        # from content-match alone: a
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
            # The latest path-specific attestation distinguishes a placement this
            # importer owns from an identical user file it merely adopted. A confirmed
            # rollback appends an invalidation before clearing the breadcrumb, so stale
            # historical ownership can never authorize a later deletion.
            current_identity = await asyncio.to_thread(
                _payload_content_identity,
                str(dst),
                allowed_root=effective_movies_root,
            )
            owns_placement = current_identity is not None and await _payload_placement_was_owned(
                session,
                torrent_hash,
                str(dst),
                current_identity=current_identity,
            )
            placement_removed = False
            if owns_placement and current_identity is not None:
                placement_removed = await asyncio.to_thread(
                    _remove_quietly,
                    dst,
                    expected_identity=current_identity,
                    allowed_root=effective_movies_root,
                )
                if placement_removed:
                    _invalidate_payload_placement_ownership(
                        session,
                        torrent_hash,
                        str(dst),
                        tmdb_id=request.tmdb_id,
                    )
            reason = f"plex scan failed: {type(exc).__name__}"
            if owns_placement and not placement_removed:
                reason = (
                    f"{reason}; {_OWNED_PLACEMENT_CLEANUP_FAILURE_FRAGMENT}; "
                    f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                )
            elif placement_owned and not owns_placement:
                # This attempt (or a prior current-lifecycle attempt) owned the
                # attested file, but the pathname changed while Plex was awaited.
                # Preserve the pointer and require review; the replacement is never
                # deletion-authorized merely because it reused our destination.
                reason = f"{reason}; {_MANUAL_CLEANUP_BREADCRUMB_REASON}"
            await _block(
                session,
                download_repo,
                download_id,
                reason,
                request_id=request.id,
                clear_download_path=owns_placement and placement_removed,
            )
            return await download_repo.get_by_hash(torrent_hash)

        if not await _payload_manifest_was_validated(
            session,
            torrent_hash,
            str(dst),
            allowed_root=effective_movies_root,
        ):
            await _block(
                session,
                download_repo,
                download_id,
                _PLACEMENT_IDENTITY_CHANGED_REASON,
                request_id=request.id,
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
                message=f"imported {os.path.basename(source.path)} to {relative}",
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
    download_status: str,
    download_path: str | None,
    failed_reason: str | None,
    known_scope_seasons: tuple[int, ...],
    imported_scope_seasons: tuple[int, ...],
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
    breadcrumb_cleanup: Literal["absent", "defer", "protected", "root_unavailable"] | None = None
    if download_path is not None:
        expected_breadcrumb = _expected_tv_breadcrumb_path(
            download_path,
            tv_root,
            request.title,
            request.year,
            known_scope_seasons,
        )
        if expected_breadcrumb is None:
            if status is None and _is_manual_cleanup_breadcrumb(
                download_status, download_path, failed_reason
            ):
                # An unbound cleanup pointer remains parked without a filesystem probe.
                return await download_repo.get_by_hash(torrent_hash)
            await _block(
                session,
                download_repo,
                download_id,
                (
                    "stored scoped-tv import breadcrumb does not match any attached season; "
                    f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                ),
                request_id=request.id,
                seasons=target_seasons,
                expected_failed_reason=failed_reason,
            )
            return await download_repo.get_by_hash(torrent_hash)
        download_path = os.fspath(expected_breadcrumb)
        breadcrumb_cleanup = await asyncio.to_thread(
            _trusted_breadcrumb_cleanup_state,
            tv_root,
            expected_breadcrumb,
            allow_empty_directory=True,
        )
        if breadcrumb_cleanup == "root_unavailable" and status is None:
            return await download_repo.get_by_hash(torrent_hash)
        if breadcrumb_cleanup == "defer":
            return await download_repo.get_by_hash(torrent_hash)
        if breadcrumb_cleanup == "absent":
            _invalidate_payload_placement_ownership(
                session,
                torrent_hash,
                download_path,
                tmdb_id=request.tmdb_id,
            )
            cleared = await download_repo.update_status_if_in(
                download_id,
                download_status,
                frozenset({download_status}),
                clear_download_path=True,
                require_failed_reason=failed_reason,
            )
            if not cleared:
                await session.rollback()
                return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
            await session.commit()
            download_path = None
    if status is None and _is_manual_cleanup_breadcrumb(
        download_status, download_path, failed_reason
    ):
        # The trusted destination still exists, so retain its cleanup breadcrumb.
        return await download_repo.get_by_hash(torrent_hash)
    if status is None and download_path is not None:
        # This multi-scope path has no single safe status-less resume transaction.
        # Park it visibly while retaining the placement; current-policy proof
        # distinguishes a client outage from an old/unvalidated crash row.
        payload_validated = await _payload_manifest_was_validated(
            session,
            torrent_hash,
            download_path,
            allowed_root=tv_root,
        )
        await _block(
            session,
            download_repo,
            download_id,
            (
                _SCOPED_BREADCRUMB_CLIENT_MISSING_REASON
                if payload_validated
                else _UNVALIDATED_BREADCRUMB_REASON
            ),
            request_id=request.id,
            seasons=target_seasons,
            expected_failed_reason=failed_reason,
        )
        return await download_repo.get_by_hash(torrent_hash)
    if status is not None and _payload_manifest_is_complete(status):
        # Scoped TV rows return through THIS helper, so without this gate they would
        # bypass the unsafe-payload rejection entirely (the reconcile-side validator
        # does not cover ``importing`` rows). A row whose breadcrumb proves placed
        # library files (crash-resumed Importing, or an existing manual-cleanup block)
        # is parked for manual cleanup instead of failed, so the torrent removal can
        # never orphan those files; ``seasons`` re-arms every attached scope.
        rejected = await _reject_unsafe_payload_if_reported(
            session=session,
            download_repo=download_repo,
            qbt=qbt,
            download_id=download_id,
            torrent_hash=torrent_hash,
            status=status,
            request_id=request.id,
            seasons=target_seasons,
            block_existing_breadcrumb=download_path is not None,
        )
        if rejected is not None:
            return rejected
        if breadcrumb_cleanup == "root_unavailable":
            return await download_repo.get_by_hash(torrent_hash)
    if status is not None and not _is_settled_for_import(status):
        if download_path is not None:
            return await download_repo.get_by_hash(torrent_hash)
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

    sources = await asyncio.to_thread(
        _resolve_sources,
        fs,
        visible_content.path,
        source_root=visible_content.source_root,
        source_root_identity=visible_content.source_root_identity,
    )
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
    unresolved_breadcrumb_real: str | None = None
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

    if download_path is not None:
        breadcrumb_real = os.path.realpath(download_path)
        matching_plan_index = next(
            (
                index
                for index, plan in enumerate(plans)
                if os.path.realpath(plan.season_dir) == breadcrumb_real
            ),
            None,
        )
        imported_scope_dirs = {
            os.path.realpath(
                Path(tv_root)
                / plex_tv_season_relative_dir(request.title, request.year, imported_season)
            )
            for imported_season in imported_scope_seasons
        }
        if matching_plan_index is not None and matching_plan_index > 0:
            # Re-adopt/scan the breadcrumbed placement before any later plan can
            # replace the download row's single durable pointer.
            plans.insert(0, plans.pop(matching_plan_index))
        elif matching_plan_index is None and breadcrumb_real not in imported_scope_dirs:
            await _block(
                session,
                download_repo,
                download_id,
                (
                    "stored scoped-tv import breadcrumb has no importable attached scope; "
                    f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                ),
                request_id=request.id,
                seasons=target_seasons,
                expected_failed_reason=failed_reason,
            )
            return await download_repo.get_by_hash(torrent_hash)
        if matching_plan_index is not None:
            unresolved_breadcrumb_real = breadcrumb_real

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
        cleanup_breadcrumb: Path | None = None
        for plan_index, plan in enumerate(plans):
            plan_has_existing_breadcrumb = unresolved_breadcrumb_real == os.path.realpath(
                plan.season_dir
            )
            # Persist a non-authoritative planned destination before the first
            # external write. A process death after any copy or failed rollback
            # therefore leaves the exact cleanup obligation durable; only the
            # post-placement attestation below grants validation/ownership proof.
            await download_repo.update_status(
                download_id,
                DownloadState.Importing.value,
                download_path=str(plan.season_dir),
            )
            await session.commit()
            placed_paths: list[_PlacedPath] = []
            observed_paths: list[_PlacedPath] = []
            imported: list[tuple[str, PurePosixPath]] = []
            for relative, result in plan.by_relative.items():
                source = plan.source_by_rel[result.video.relative_path]
                dst = Path(tv_root) / relative
                try:
                    placed, observed_identity = await asyncio.to_thread(
                        _place_file_with_identity,
                        fs,
                        source,
                        dst,
                        allowed_root=tv_root,
                    )
                except (FileExistsError, OSError) as exc:
                    rollback_complete = await asyncio.to_thread(
                        _remove_quietly_many,
                        placed_paths,
                        allowed_root=tv_root,
                    )
                    failed_destination_kind = await asyncio.to_thread(_classify_stored_path, dst)
                    reason = (
                        str(exc)
                        if isinstance(exc, FileExistsError)
                        else f"import copy failed: {type(exc).__name__}"
                    )
                    cleanup_incomplete = (
                        plan_has_existing_breadcrumb
                        or not rollback_complete
                        or failed_destination_kind != "missing"
                    )
                    if cleanup_incomplete:
                        reason = (
                            f"{reason}; {_OWNED_PLACEMENT_CLEANUP_FAILURE_FRAGMENT}; "
                            f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                        )
                        cleanup_breadcrumb = plan.season_dir
                    failures.append(_TvImportFailure(plan.target, reason))
                    break
                observed_path = _PlacedPath(dst, observed_identity or "unverified")
                observed_paths.append(observed_path)
                if placed:
                    placed_paths.append(observed_path)
                imported.append((os.path.basename(source.path), relative))
            else:
                await download_repo.update_status(
                    download_id,
                    DownloadState.Importing.value,
                    download_path=str(plan.season_dir),
                )
                content_identity = await asyncio.to_thread(
                    _payload_identity_if_observations_match,
                    str(plan.season_dir),
                    observed_paths,
                    allowed_root=tv_root,
                )
                if content_identity is None:
                    await asyncio.to_thread(
                        _remove_quietly_many,
                        placed_paths,
                        allowed_root=tv_root,
                    )
                    cleanup_breadcrumb = plan.season_dir
                    failures.append(
                        _TvImportFailure(
                            plan.target,
                            _PLACEMENT_IDENTITY_CHANGED_REASON,
                        )
                    )
                    for deferred_plan in plans[plan_index + 1 :]:
                        failures.append(
                            _TvImportFailure(
                                deferred_plan.target,
                                "import deferred while stored placement cleanup remains unresolved",
                            )
                        )
                    break
                validation_attestation = _payload_validation_attestation(
                    str(plan.season_dir),
                    placement_owned=bool(placed_paths),
                    content_identity=content_identity,
                )
                session.add(
                    DownloadHistory(
                        tmdb_id=plan.target.request.tmdb_id,
                        torrent_hash=torrent_hash,
                        event_type=DownloadHistoryEvent.import_started,
                        source_title=None,
                        message=(
                            f"{validation_attestation} "
                            f"validated scoped season {plan.target.season} placement"
                        ),
                    )
                )
                await session.commit()
                scan_succeeded = True
                try:
                    await library.trigger_scan(str(plan.season_dir), "tv")
                except (PlexLibraryError, PlexAuthError) as exc:
                    scan_succeeded = False
                    rollback_complete = await asyncio.to_thread(
                        _remove_quietly_many,
                        placed_paths,
                        allowed_root=tv_root,
                    )
                    reason = f"plex scan failed: {type(exc).__name__}"
                    cleanup_incomplete = plan_has_existing_breadcrumb or not rollback_complete
                    if cleanup_incomplete:
                        reason = (
                            f"{reason}; {_OWNED_PLACEMENT_CLEANUP_FAILURE_FRAGMENT}; "
                            f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                        )
                        cleanup_breadcrumb = plan.season_dir
                    failures.append(_TvImportFailure(plan.target, reason))
                if scan_succeeded and not await _payload_manifest_was_validated(
                    session,
                    torrent_hash,
                    str(plan.season_dir),
                    allowed_root=tv_root,
                ):
                    scan_succeeded = False
                    await asyncio.to_thread(
                        _remove_quietly_many,
                        placed_paths,
                        allowed_root=tv_root,
                    )
                    cleanup_breadcrumb = plan.season_dir
                    failures.append(
                        _TvImportFailure(
                            plan.target,
                            _PLACEMENT_IDENTITY_CHANGED_REASON,
                        )
                    )
                if scan_succeeded:
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
                    if plan_has_existing_breadcrumb:
                        unresolved_breadcrumb_real = None

            if cleanup_breadcrumb is not None:
                for deferred_plan in plans[plan_index + 1 :]:
                    failures.append(
                        _TvImportFailure(
                            deferred_plan.target,
                            "import deferred while stored placement cleanup remains unresolved",
                        )
                    )
                break

        if cleanup_breadcrumb is not None:
            for failure in failures:
                await _mark_tv_scope_blocked(session, download_id=download_id, failure=failure)
            await download_repo.align_scalar_scope_with_active(download_id)
            cleanup_reason = _failure_summary(failures)
            if _MANUAL_CLEANUP_BREADCRUMB_REASON not in cleanup_reason:
                # The capped multi-scope summary may omit the later physical
                # cleanup failure. Force the durable/operator-visible marker
                # outside that cap so status-less retries remain protected.
                cleanup_reason = f"{cleanup_reason}; {_MANUAL_CLEANUP_BREADCRUMB_REASON}"
            finalized = await download_repo.update_status_if_in(
                download_id,
                DownloadState.ImportBlocked.value,
                frozenset({DownloadState.Importing.value}),
                download_path=str(cleanup_breadcrumb),
                failed_reason=cleanup_reason,
            )
            if not finalized:
                await session.rollback()
                return await download_repo.get_by_hash(torrent_hash)
            await session.commit()
            return await download_repo.get_by_hash(torrent_hash)

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
    failed_reason: str | None,
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
    placement is independently idempotent (:func:`_place_file` adopts an already-
    identical-content destination), so only the files THIS invocation itself placed
    are rolled back on a scan failure -- a single column cannot represent "N
    placed files" across invocations, so cross-invocation ownership tracking is
    not attempted here (a scoped follow-up, like true per-episode completeness).
    """
    download_repo = SqlDownloadRepository(session)

    status = await qbt.get_status(torrent_hash)
    breadcrumb_cleanup: Literal["absent", "defer", "protected", "root_unavailable"] | None = None
    if download_path is not None:
        expected_breadcrumb = _expected_tv_breadcrumb_path(
            download_path,
            tv_root,
            request.title,
            request.year,
            (season,),
        )
        if expected_breadcrumb is None:
            if status is None and _is_manual_cleanup_breadcrumb(
                download_status, download_path, failed_reason
            ):
                # An unbound cleanup pointer remains parked without a filesystem probe.
                return await download_repo.get_by_hash(torrent_hash)
            await _block(
                session,
                download_repo,
                download_id,
                (
                    "stored tv import breadcrumb does not match the current season; "
                    f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                ),
                request_id=request.id,
                season=season,
                expected_failed_reason=failed_reason,
            )
            return await download_repo.get_by_hash(torrent_hash)
        download_path = os.fspath(expected_breadcrumb)
        breadcrumb_cleanup = await asyncio.to_thread(
            _trusted_breadcrumb_cleanup_state,
            tv_root,
            expected_breadcrumb,
            allow_empty_directory=True,
        )
        if breadcrumb_cleanup == "root_unavailable" and status is None:
            return await download_repo.get_by_hash(torrent_hash)
        if breadcrumb_cleanup == "defer":
            return await download_repo.get_by_hash(torrent_hash)
        if breadcrumb_cleanup == "absent":
            _invalidate_payload_placement_ownership(
                session,
                torrent_hash,
                download_path,
                tmdb_id=request.tmdb_id,
            )
            cleared = await download_repo.update_status_if_in(
                download_id,
                download_status,
                frozenset({download_status}),
                clear_download_path=True,
                require_failed_reason=failed_reason,
            )
            if not cleared:
                await session.rollback()
                return await download_repo.get_by_hash(torrent_hash, populate_existing=True)
            await session.commit()
            download_path = None
    if status is None:
        if _is_manual_cleanup_breadcrumb(download_status, download_path, failed_reason):
            # The trusted destination still exists, so retain its cleanup breadcrumb.
            return await download_repo.get_by_hash(torrent_hash)
        payload_validated = download_path is not None and await _payload_manifest_was_validated(
            session,
            torrent_hash,
            download_path,
            allowed_root=tv_root,
        )
        if download_path is not None and _can_resume_breadcrumb_without_client_status(
            download_status, failed_reason, payload_validated=payload_validated
        ):
            if download_status == DownloadState.ImportBlocked.value:
                resumed = await download_repo.update_status_if_in(
                    download_id,
                    DownloadState.Importing.value,
                    frozenset({DownloadState.ImportBlocked.value}),
                    clear_failed_reason=True,
                    require_failed_reason=failed_reason,
                )
                if not resumed:
                    await session.rollback()
                    return await download_repo.get_by_hash(torrent_hash)
                await session.commit()
            return await _resume_breadcrumbed_tv_import(
                session=session,
                download_repo=download_repo,
                request=request,
                season=season,
                download_id=download_id,
                torrent_hash=torrent_hash,
                fs=fs,
                library=library,
                parser=parser,
                profile=profile,
                tv_root=tv_root,
                download_path=download_path,
            )
        if download_path is not None and not payload_validated:
            await _block(
                session,
                download_repo,
                download_id,
                _UNVALIDATED_BREADCRUMB_REASON,
                request_id=request.id,
                season=season,
                expected_failed_reason=failed_reason,
            )
            return await download_repo.get_by_hash(torrent_hash)
        if download_path is not None:
            return await download_repo.get_by_hash(torrent_hash)
        await _block(
            session,
            download_repo,
            download_id,
            "download client reported no status for payload validation",
            request_id=request.id,
            season=season,
            expected_failed_reason=failed_reason,
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
            season=season,
            block_existing_breadcrumb=download_path is not None,
        )
        if rejected is not None:
            return rejected
        if breadcrumb_cleanup == "root_unavailable":
            return await download_repo.get_by_hash(torrent_hash)
    if not _is_settled_for_import(status):
        if download_path is not None:
            return await download_repo.get_by_hash(torrent_hash)
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
    content = visible_content.path

    sources = await asyncio.to_thread(
        _resolve_sources,
        fs,
        content,
        source_root=visible_content.source_root,
        source_root_identity=visible_content.source_root_identity,
    )
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
        [
            VideoFile(relative_path=source.relative_path, size_bytes=source.size)
            for source in sources
        ],
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
    source_by_rel = {source.relative_path: source for source in sources}
    by_relative: dict[PurePosixPath, EpisodeImportResult] = {}
    for result in validation.accepted:
        source = source_by_rel[result.video.relative_path]
        ext = os.path.splitext(source.path)[1].lstrip(".")
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
    preserve_existing_breadcrumb = download_path is not None
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
            clear_failed_reason=True,
            download_path=str(season_dir),
        )
        if not claimed:
            await session.rollback()
            return await download_repo.get_by_hash(torrent_hash)

        import_started_history = DownloadHistory(
            tmdb_id=request.tmdb_id,
            torrent_hash=torrent_hash,
            event_type=DownloadHistoryEvent.import_started,
            source_title=None,  # never shadow the grabbed release title
            message=f"importing {len(by_relative)} episode(s) to {season_dir}",
        )
        session.add(import_started_history)
        await session.commit()

        # ``imported`` history rows are staged (basename, relative) here rather than
        # written immediately: they are committed to ``session`` ONLY after the scan
        # AND the finalize CAS below have both succeeded (mirrors the movie path,
        # which likewise writes its ``imported`` row only once the scan succeeds). A
        # later file's copy failure OR a scan failure both roll placed files back via
        # ``_remove_quietly_many`` / ``_block`` — writing history eagerly here would let
        # the audit trail claim an episode was imported when it was in fact deleted
        # moments later (honesty over silence: history must never lie about a rollback).
        placed_paths: list[_PlacedPath] = []
        observed_paths: list[_PlacedPath] = []
        imported: list[tuple[str, PurePosixPath]] = []
        for relative, result in by_relative.items():
            source = source_by_rel[result.video.relative_path]
            dst = Path(tv_root) / relative
            try:
                placed, observed_identity = await asyncio.to_thread(
                    _place_file_with_identity,
                    fs,
                    source,
                    dst,
                    allowed_root=tv_root,
                )
            except (FileExistsError, OSError) as exc:
                rollback_complete = await asyncio.to_thread(
                    _remove_quietly_many,
                    placed_paths,
                    allowed_root=tv_root,
                )
                failed_destination_kind = await asyncio.to_thread(_classify_stored_path, dst)
                reason = (
                    str(exc)
                    if isinstance(exc, FileExistsError)
                    else f"import copy failed: {type(exc).__name__}"
                )
                cleanup_incomplete = (
                    preserve_existing_breadcrumb
                    or not rollback_complete
                    or failed_destination_kind != "missing"
                )
                if cleanup_incomplete:
                    reason = (
                        f"{reason}; {_OWNED_PLACEMENT_CLEANUP_FAILURE_FRAGMENT}; "
                        f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                    )
                await _block(
                    session,
                    download_repo,
                    download_id,
                    reason,
                    request_id=request.id,
                    season=season,
                    clear_download_path=not cleanup_incomplete,
                    download_path=str(season_dir) if cleanup_incomplete else None,
                )
                return await download_repo.get_by_hash(torrent_hash)
            observed_path = _PlacedPath(dst, observed_identity or "unverified")
            observed_paths.append(observed_path)
            if placed:
                placed_paths.append(observed_path)
            imported.append((os.path.basename(source.path), relative))

        # download_path is stamped with the SEASON folder (not one file) purely for
        # queue-display observability -- unlike the movie path, it is never consulted
        # to decide scan-failure rollback ownership (see the docstring above).
        await download_repo.update_status(
            download_id, DownloadState.Importing.value, download_path=str(season_dir)
        )
        content_identity = await asyncio.to_thread(
            _payload_identity_if_observations_match,
            str(season_dir),
            observed_paths,
            allowed_root=tv_root,
        )
        if content_identity is None:
            await asyncio.to_thread(
                _remove_quietly_many,
                placed_paths,
                allowed_root=tv_root,
            )
            await _block(
                session,
                download_repo,
                download_id,
                _PLACEMENT_IDENTITY_CHANGED_REASON,
                request_id=request.id,
                season=season,
                download_path=str(season_dir),
            )
            return await download_repo.get_by_hash(torrent_hash)
        validation_attestation = _payload_validation_attestation(
            str(season_dir),
            placement_owned=bool(placed_paths),
            content_identity=content_identity,
        )
        session.add(
            DownloadHistory(
                tmdb_id=request.tmdb_id,
                torrent_hash=torrent_hash,
                event_type=DownloadHistoryEvent.import_started,
                source_title=None,
                message=(
                    f"{validation_attestation} "
                    f"validated {len(by_relative)} episode placement(s) at {season_dir}"
                ),
            )
        )
        await session.commit()

        # ONE targeted scan of the whole season directory, never one per episode.
        try:
            await library.trigger_scan(str(season_dir), "tv")
        except (PlexLibraryError, PlexAuthError) as exc:
            rollback_complete = await asyncio.to_thread(
                _remove_quietly_many,
                placed_paths,
                allowed_root=tv_root,
            )
            reason = f"plex scan failed: {type(exc).__name__}"
            cleanup_incomplete = preserve_existing_breadcrumb or not rollback_complete
            if cleanup_incomplete:
                reason = (
                    f"{reason}; {_OWNED_PLACEMENT_CLEANUP_FAILURE_FRAGMENT}; "
                    f"{_MANUAL_CLEANUP_BREADCRUMB_REASON}"
                )
            await _block(
                session,
                download_repo,
                download_id,
                reason,
                request_id=request.id,
                season=season,
                clear_download_path=rollback_complete and not preserve_existing_breadcrumb,
            )
            return await download_repo.get_by_hash(torrent_hash)

        if not await _payload_manifest_was_validated(
            session,
            torrent_hash,
            str(season_dir),
            allowed_root=tv_root,
        ):
            await asyncio.to_thread(
                _remove_quietly_many,
                placed_paths,
                allowed_root=tv_root,
            )
            await _block(
                session,
                download_repo,
                download_id,
                _PLACEMENT_IDENTITY_CHANGED_REASON,
                request_id=request.id,
                season=season,
                download_path=str(season_dir),
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
        if row.status in _AUTO_DRAIN or (
            row.status == DownloadState.ImportBlocked.value
            and row.failed_reason is not None
            and row.failed_reason.startswith(
                (
                    _MANIFEST_OUTAGE_REASON_PREFIX,
                    _UNVALIDATED_BREADCRUMB_REASON_PREFIX,
                    _SCOPED_BREADCRUMB_CLIENT_MISSING_REASON_PREFIX,
                )
            )
        ):
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
