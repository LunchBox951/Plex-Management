"""Shared correction primitives (ADR-0014): the mechanical, best-effort building
blocks the correction verbs (report-issue, cancel) and the disk-pressure eviction
sweep all compose, so the load-bearing safety logic lives in ONE place.

Three primitives, each best-effort by design (a failure is logged, never silent,
and never raised) — the DB state change a caller commits around them is the
authoritative record; a client/Plex/FS hiccup here must never undo it:

* :func:`purge_library_path` — the root-guarded ``fs.delete`` of a stored
  ``library_path`` breadcrumb, plus the hardlink-aware reclaimable-bytes
  accounting (measured BEFORE the delete, since a file's link count can only be
  read while it still exists). Returns a :class:`PurgeResult` classifying the
  outcome (``deleted`` / ``refused`` by the containment guard / ``deferred`` to
  an active import / ``error``); the CALLER logs, so each keeps its own
  context-appropriate message and logger.
* :func:`trigger_library_scan` — the best-effort Plex refresh (delete-file-then-
  trigger_scan is how a title/season is removed from Plex; there is no
  ``LibraryPort`` delete API and none is needed).
* :func:`remove_torrent` — the best-effort ``qbt.remove(delete_files=True)`` that
  closes the "a blocklisted / cancelled download keeps seeding forever" leak.
  Removing an already-gone hash is a no-op success (qBittorrent's
  ``/torrents/delete`` tolerates unknown hashes).

Hardlink caveat (ADR-0014): a same-filesystem import ``hardlink_or_copy``-links
the library file to the download client's seed copy, so BOTH the torrent-with-data
AND the library file must be removed to actually reclaim the space and eliminate
the bad release — a correction verb calls :func:`remove_torrent` AND
:func:`purge_library_path`, never just one.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from plex_manager.adapters.filesystem.local import LocalFileSystemError
from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.services import path_visibility

if TYPE_CHECKING:
    from plex_manager.ports.download_client import DownloadClientPort, DownloadStatus
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort

__all__ = [
    "PurgeOutcome",
    "PurgeResult",
    "begin_placement",
    "end_placement",
    "end_purge",
    "purge_library_path",
    "remove_torrent",
    "trigger_library_scan",
]

_logger = logging.getLogger(__name__)

# Bounded poll for qBittorrent's own server-side, ASYNCHRONOUS on-disk file
# deletion to actually finish after a ``/torrents/delete?deleteFiles=true`` call
# has already ACKed (issue #240, residual left by PR #235's same-hash cancel-race
# guard). See :func:`_wait_for_content_path_gone`. Bounded rather than unbounded:
# this runs inline in an operator-facing request (cancel / mark-failed / report-
# issue) as well as the reconcile poll loop, so it must never block either
# indefinitely -- a still-present path after the bound is logged (honesty over
# silence) and the caller proceeds; the DB state change already committed.
_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS: Final = 5.0
_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS: Final = 0.25

# --------------------------------------------------------------------------- #
# In-process purge-vs-import path serialization (PR #117 round 9).
#
# During an eviction's committed-claim window a fast re-request (terminal-row
# reuse of a still-seeding torrent makes the re-import near-instant) can be
# PLACING the replacement into the same deterministic movie/season directory
# that the purge's rmtree is walking -- the purge would delete files the import
# just placed and committed. Both actors run in this one process (the same
# honest in-process scope as the eviction sweep latch), so a pair of refcounted
# path registries serializes them.
#
# ORDERING RULE (stated identically at the import site): FIRST-REGISTERED WINS,
# and the loser defers fast rather than waiting -- the check-and-register below
# has no ``await`` between them, so it is atomic on the single event loop:
#
# * ``purge_library_path`` defers (``PurgeOutcome.deferred`` with an explicit
#   detail) when an import is mid-placement into a conflicting path. That is NOT
#   an ordinary unlink failure for eviction: a replacement import may be about to
#   own the path, so the old eviction claim stays published with its breadcrumb
#   for the next recovery pass to adjudicate. Report-issue keeps the breadcrumb
#   like a retryable delete failure.
# * ``begin_placement`` (called by ``import_service`` before its ``Importing``
#   claim) refuses when a purge is mid-delete on a conflicting path: the import
#   attempt is skipped for THIS cycle and honestly retried on the next one (the
#   row stays ``ImportPending``, which every import cycle re-picks).
#
# Paths conflict on equality OR containment (a movie FILE placement vs a purge
# of its ``Title (Year)/`` directory; an episode file vs its season directory).
_ACTIVE_PURGE_PATHS: dict[str, int] = {}
_ACTIVE_PLACEMENT_PATHS: dict[str, int] = {}


def _normalize_guard_path(path: str) -> str:
    """Normalize for registry comparison (no symlink resolution: the stored
    breadcrumb and the import's computed destination share the same textual
    root, and the target may not exist yet)."""
    return os.path.abspath(os.path.normpath(path))


def _conflicts_with(path: str, registry: dict[str, int]) -> bool:
    """Whether ``path`` equals, contains, or is contained by any registered path."""
    norm = _normalize_guard_path(path)
    return any(
        norm == other or norm.startswith(other + os.sep) or other.startswith(norm + os.sep)
        for other in registry
    )


def _register(path: str, registry: dict[str, int]) -> None:
    norm = _normalize_guard_path(path)
    registry[norm] = registry.get(norm, 0) + 1


def _unregister(path: str, registry: dict[str, int]) -> None:
    norm = _normalize_guard_path(path)
    count = registry.get(norm, 0) - 1
    if count > 0:
        registry[norm] = count
    else:
        registry.pop(norm, None)


def begin_placement(library_path: str) -> bool:
    """Register an import placement into ``library_path``; ``False`` = refuse.

    ``False`` means a purge is mid-delete on a conflicting path (see the
    ordering rule above): the caller must SKIP this import attempt -- honestly
    retryable on the next import cycle -- rather than place files into a
    directory an rmtree is walking. On ``True`` the placement is registered and
    the caller MUST pair it with :func:`end_placement` (try/finally) once the
    placement + finalize commit are done, so a purge arriving mid-placement
    defers instead of deleting freshly placed files.
    """
    if _conflicts_with(library_path, _ACTIVE_PURGE_PATHS):
        return False
    _register(library_path, _ACTIVE_PLACEMENT_PATHS)
    return True


def end_placement(library_path: str) -> None:
    """Release a :func:`begin_placement` registration (refcounted)."""
    _unregister(library_path, _ACTIVE_PLACEMENT_PATHS)


def end_purge(library_path: str) -> None:
    """Release a held purge registration (refcounted)."""
    _unregister(library_path, _ACTIVE_PURGE_PATHS)


class PurgeOutcome(StrEnum):
    """How a :func:`purge_library_path` attempt resolved."""

    #: ``fs.delete`` ran (the path was removed, OR was already gone — an
    #: idempotent no-op success within a configured root).
    deleted = "deleted"
    #: The root-containment guard refused: the path resolves outside every
    #: configured library root (a stale/misconfigured breadcrumb). Nothing deleted.
    refused = "refused"
    #: A replacement import is actively placing into this path. Nothing deleted;
    #: eviction should leave the claim/breadcrumb for later recovery.
    deferred = "deferred"
    #: An ``OSError`` (permission denied, I/O error) while deleting. Nothing (or
    #: only part of a tree) deleted; the caller may retry later.
    error = "error"


@dataclass(frozen=True)
class PurgeResult:
    """The outcome of a :func:`purge_library_path` attempt.

    ``freed_bytes`` is the hardlink-aware reclaimable total measured before the
    delete (``0`` for anything but a successful ``deleted``). ``detail`` carries
    the guard message (``refused``) or the exception type name (``error``) so the
    caller can log an honest reason; ``None`` on success.
    """

    outcome: PurgeOutcome
    freed_bytes: int
    detail: str | None = None


async def _delete_to_settlement(fs: FileSystemPort, library_path: str) -> None:
    """Run ``fs.delete`` to completion, holding any caller cancellation until it does.

    ``await asyncio.to_thread(fs.delete, library_path)`` cancels by detaching
    the awaiting coroutine from the executor future — the ``rmtree``/unlink
    already handed to the thread pool keeps running in the background
    regardless, since a thread cannot be interrupted from outside. A bare
    ``to_thread`` await would let a cancelled :func:`purge_library_path`
    unwind through its ``finally`` (releasing its ``_ACTIVE_PURGE_PATHS``
    registration) — and, transitively, let a cancelled eviction sweep release
    ``_sweep_latch`` — while that background thread is still mutating the
    filesystem. That is exactly the window issue #128 exploits: a *later*
    sweep's crash-recovery pass can then stat the still-present path, restore
    the row to ``available``, and watch the file vanish out from under it a
    moment later when the orphaned delete finally finishes.

    The delete instead runs in its own child task that is never cancelled;
    this coroutine ``asyncio.shield``s a wait on that task's completion, so a
    cancellation delivered here is caught and RE-DELIVERED only once the
    worker has truly settled (success or error) — never before. Every caller's
    registration release / ``finally`` therefore always observes a settled
    filesystem, never a still-running delete.
    """
    worker: asyncio.Task[None] = asyncio.create_task(asyncio.to_thread(fs.delete, library_path))
    settled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    worker_error: BaseException | None = None

    def _consume_worker_result(done: asyncio.Task[None]) -> None:
        nonlocal worker_error
        if not done.cancelled():
            worker_error = done.exception()
        settled.set_result(None)

    worker.add_done_callback(_consume_worker_result)

    was_cancelled = False
    while not settled.done():
        try:
            await asyncio.shield(settled)
        except asyncio.CancelledError:
            was_cancelled = True
    if was_cancelled:
        if worker_error is not None:
            # Cancellation wins: the caller is already unwinding on
            # CancelledError and will never read a classified PurgeResult, so a
            # worker error that ALSO happened during that unwind is logged here
            # (honesty over silence) rather than raised over the cancellation.
            _logger.warning(
                "purge delete of %r failed (%s) while the caller was being "
                "cancelled; the delete did not complete",
                library_path,
                type(worker_error).__name__,
            )
        raise asyncio.CancelledError
    if worker_error is not None:
        raise worker_error


async def purge_library_path(
    fs: FileSystemPort, library_path: str, *, hold_purge_registration: bool = False
) -> PurgeResult:
    """Root-guarded delete of ``library_path`` + hardlink-aware freed-bytes accounting.

    Both the size accounting and the delete are real, synchronous disk I/O
    (``os.stat``/``os.walk``/``shutil.rmtree``), so each runs off the event loop
    via ``asyncio.to_thread`` — mirroring every other blocking FS primitive in the
    services layer (see ``eviction_service._size_bytes``/``_evict_one``). The
    delete specifically goes through :func:`_delete_to_settlement`, which shields
    the wait so a caller's cancellation is never observed until the underlying
    delete thread has genuinely finished (issue #128) — the registration this
    function holds in ``_ACTIVE_PURGE_PATHS`` (below) spans that full worker
    lifetime, including any cancellation settlement, so a concurrent
    ``begin_placement`` / a later sweep's crash-recovery can never see this
    path as free while a delete for it is still physically running.

    The delete goes through :meth:`FileSystemPort.delete`, whose implementation
    refuses (raises :class:`LocalFileSystemError`) any path resolving outside a
    configured library root and treats an already-gone in-root path as an
    idempotent no-op success. Classifies the result rather than logging it: the
    caller (eviction / report-issue) owns the context-specific message + logger.
    """
    # PURGE-vs-IMPORT ordering rule (see the registry block above): defer to an
    # import that is mid-placement into this path -- deleting under it would eat
    # files the import is about to (or just did) commit as placed. ``deferred`` is
    # distinct from a real unlink failure because eviction must not restore the
    # old owner while a replacement import may be finalizing that same path.
    # Check-and-register has no await between them.
    if _conflicts_with(library_path, _ACTIVE_PLACEMENT_PATHS):
        return PurgeResult(
            PurgeOutcome.deferred, 0, "deferred: an import is placing into this path"
        )
    _register(library_path, _ACTIVE_PURGE_PATHS)
    release_registration = True
    try:
        # Fail an out-of-root breadcrumb CLOSED and FAST, BEFORE the (potentially
        # huge, recursive) reclaimable_bytes walk. A stale/misconfigured breadcrumb
        # can point at an existing directory outside every configured root (an old
        # library root, or even ``/``); measuring it first would walk that whole
        # outside tree only for the delete below to then refuse it.
        # ``delete_guard_refuses`` is the same walk-free containment predicate
        # ``delete`` applies (the exact refusal decision, as a read-only query), so
        # this changes nothing for an in-root path -- it only short-circuits the
        # exact paths ``delete`` was always going to refuse.
        if await asyncio.to_thread(fs.delete_guard_refuses, library_path):
            return PurgeResult(
                PurgeOutcome.refused, 0, "path resolves outside every configured library root"
            )

        # Reclaimable bytes MUST be read before the delete: a file's link count is
        # only knowable while the path still exists (hardlink-aware accounting,
        # ADR-0012 / ADR-0014). A measurement failure is "unknown -> 0", never an
        # abort.
        try:
            freed_bytes = await asyncio.to_thread(fs.reclaimable_bytes, library_path)
        except OSError:
            freed_bytes = 0

        try:
            await _delete_to_settlement(fs, library_path)
        except LocalFileSystemError as exc:
            return PurgeResult(PurgeOutcome.refused, 0, str(exc))
        except OSError as exc:
            return PurgeResult(PurgeOutcome.error, 0, type(exc).__name__)
        if hold_purge_registration:
            release_registration = False
        return PurgeResult(PurgeOutcome.deleted, freed_bytes)
    finally:
        if release_registration:
            _unregister(library_path, _ACTIVE_PURGE_PATHS)


async def trigger_library_scan(
    library: LibraryPort,
    *,
    library_path: str,
    media_type: Literal["movie", "tv"],
    context: str,
    extra: dict[str, object] | None = None,
) -> None:
    """Best-effort Plex refresh so a removed title/season drops out of the library.

    Delete-file-then-trigger_scan is how the app removes an item from Plex (there
    is no ``LibraryPort`` delete API). Best-effort and symmetric with the import
    pipeline's post-place scan: the DB state change the caller committed already
    stands, so a Plex outage here is logged (Plex catches up on its next scheduled
    scan), never a failure that undoes the completed correction/eviction.

    ``context`` is a static description of the caller (e.g. ``"eviction"``,
    ``"report-issue"``) — logged verbatim, never an interpolated request-derived
    string, so the log-injection convention holds. Correlation ids go via
    ``extra``.
    """
    try:
        await library.trigger_scan(library_path, media_type)
    except (PlexLibraryError, PlexAuthError) as exc:
        _logger.warning(
            "post-%s Plex refresh failed (%s); Plex may briefly still report the "
            "item present until its next scheduled scan",
            context,
            type(exc).__name__,
            extra=extra,
        )


async def remove_torrent(
    qbt: DownloadClientPort,
    torrent_hash: str,
    *,
    context: str,
    extra: dict[str, object] | None = None,
) -> bool:
    """Best-effort ``qbt.remove(delete_files=True)`` — closes the seeding leak.

    A blocklisted / cancelled / reported download must not keep seeding and
    holding disk. Removing an already-gone hash is a no-op success (qBittorrent's
    ``/torrents/delete`` tolerates an unknown hash). A genuine failure is logged
    (honesty over silence) but never raised: the caller's blocklist/status writes
    have already committed and must not be undone by a client hiccup — the leak is
    made VISIBLE in the log rather than aborting the correction.

    Returns whether the client call SUCCEEDED (an already-gone hash counts as
    success). ``queue_service`` uses this to persist the removal outcome into its
    provenance marker (``remove=done``): a durable "already removed" record must
    only be written for a removal that actually happened, so a client hiccup
    returns ``False`` and the marker keeps saying the removal is still owed.
    Callers that only need the best-effort behaviour may ignore the result.

    ``context`` is a static caller description, logged verbatim (never an
    interpolated request-derived string — log-injection convention); the
    torrent hash and any correlation ids go via ``extra``. A torrent hash is not a
    secret; the grab source (which embeds a Prowlarr api key) is never logged here.

    Post-ack disk-deletion residual (issue #240): qBittorrent's own file removal
    for ``delete_files=True`` is ASYNCHRONOUS server-side — the ``/torrents/delete``
    call returns as soon as the delete is *accepted*, not once it has finished
    walking the content path. Every caller of this function releases its own
    removal-physics guard (``queue_service``'s ``_removals_in_flight`` /
    ``_operator_fail_claims``) once this call returns, on the assumption the data
    is actually gone by then — a same-hash re-grab (the exact same release
    re-requested) landing in the narrow window between the ACK and the real
    on-disk finish can have its freshly-written data clobbered by the TAIL of the
    old deletion still walking the same path. Before returning success, this
    function therefore snapshots the torrent's ``content_path`` BEFORE removing it
    (the client no longer reports it once the torrent itself is gone) and, if one
    was reported, polls (:func:`_wait_for_content_path_gone`) for that path to
    actually disappear from disk, bounded so this best-effort call can never block
    a caller indefinitely. A ``content_path`` that merely restates ``save_path``
    (the adapter nulls it in that case — see ``adapters/qbittorrent/adapter.py``)
    has nothing distinct to poll (``save_path`` is shared by other torrents) and
    is skipped honestly rather than polling the wrong thing.

    Container-visible remap (issue #240 residual, Codex review on PR #281): qBittorrent
    runs on the HOST, so the snapshotted ``content_path`` can be a HOST-namespace
    path (e.g. ``/srv/downloads/...``) this process only sees through the
    ``/downloads`` bind mount — polling the raw path would find it already
    "gone" (``os.path.exists`` on a path that never existed HERE) and release
    the guard immediately while qBittorrent may still be deleting the real,
    container-visible file. :func:`_visible_content_path` (called BEFORE the
    torrent is removed, since the remap's proof needs the client's own file
    list, which is unavailable once the torrent is gone) applies the exact same
    remap ``import_service._resolve_visible_content`` uses for imports.
    """
    content_path: str | None = None
    try:
        status = await qbt.get_status(torrent_hash)
    except Exception:
        # Best-effort snapshot only: a failure here just means the post-delete
        # poll below is skipped (nothing distinct to verify), never a reason to
        # abort the removal itself.
        status = None
    if status is not None:
        raw_content = _snapshot_content_path(status)
        if raw_content is not None:
            content_path = await _visible_content_path(
                qbt, torrent_hash, raw_content, status.save_path
            )
    try:
        await qbt.remove(torrent_hash, delete_files=True)
    except Exception:
        # Best-effort: surface (log), never abort the correction. Broad by design
        # -- any client-side failure (network, auth, 5xx) must not undo the
        # already-committed blocklist/status writes; mirrors grab_service's own
        # orphan-torrent cleanup on a lost parallel grab.
        _logger.warning(
            "failed to remove torrent after %s; it may keep seeding until removed manually",
            context,
            exc_info=True,
            extra=extra,
        )
        return False
    if content_path is not None:
        await _wait_for_content_path_gone(content_path, context=context, extra=extra)
    return True


def _snapshot_content_path(status: DownloadStatus) -> str | None:
    """The distinct on-disk content path to poll for a just-removed torrent.

    Prefer the client's ``content_path``. The adapter nulls it when it merely
    echoed ``save_path`` (a not-yet-resolved torrent); in that case
    ``save_path`` + ``name`` is the live content location the importer's
    ``import_service._resolve_content`` already uses, and it IS distinct --
    ``save_path`` ALONE is shared by sibling torrents and must never be polled.
    Without this fallback the post-ack poll is skipped entirely for that class of
    torrents, leaving the issue #240 same-hash race open for them (issue #290,
    finding #1).

    Returns ``None`` when nothing distinct is known, or when ``name`` would
    escape ``save_path`` -- an absolute ``name``, or a relative one whose ``..``
    components (or symlinks) resolve the join OUTSIDE ``save_path`` (e.g.
    ``save_path=/downloads/foo`` + ``name=../bar`` -> ``/downloads/bar``). Both
    mirror ``import_service._resolve_content``'s realpath containment guard, so
    the caller honestly skips the poll rather than watching an unrelated tree --
    which could release (or needlessly delay) the same-hash guard based on the
    wrong file. Strictly UNDER, not equal: a join that resolves back to
    ``save_path`` itself (e.g. a ``.`` name) is the shared save directory again,
    which must never be polled.
    """
    if status.content_path:
        return status.content_path
    if status.save_path and status.name and not os.path.isabs(status.name):
        candidate = os.path.join(status.save_path, status.name)
        # Realpath containment, exactly as import_service._ensure_under_save_path
        # (kept local for the same reason that helper keeps _is_within local: the
        # two services fail differently -- import raises a typed error, this
        # snapshot degrades to an honest "nothing distinct to poll").
        root_real = os.path.realpath(status.save_path)
        candidate_real = os.path.realpath(candidate)
        if candidate_real.startswith(root_real + os.sep):
            return candidate
        return None
    return None


async def _visible_content_path(
    qbt: DownloadClientPort, torrent_hash: str, content_path: str, save_path: str
) -> str | None:
    """Container-visible remap of a just-snapshotted torrent ``content_path``.

    Mirrors ``import_service._resolve_visible_content`` (issue #133): qBittorrent
    runs on the HOST, so a client-reported ``content_path`` can be a
    HOST-namespace path this container cannot see even though the file sits
    right there, one bind mount away (e.g. host ``/srv/downloads/...`` vs. this
    container's ``/downloads/...``). Returns ``content_path`` unchanged when it
    already exists here AND genuinely sits under a live ``/downloads`` mount (the
    common same-namespace fast path — no client call needed); otherwise anchors
    the remap on ``save_path`` and demands the torrent's OWN file list
    (:meth:`DownloadClientPort.list_files`) prove the remapped candidate exhibits
    that exact payload at the exact relative location and size — never an
    existence-only guess — via
    :func:`~plex_manager.services.path_visibility.remap_download_content`.

    The mount-aware fast path matters (issue #290, finding #2): a HOST-namespace
    ``content_path`` can coincidentally exist in this container as a stale/phantom
    tree OUTSIDE the ``/downloads`` mount, and short-circuiting to it would make
    the post-ack poll watch the WRONG location — releasing the same-hash guard
    while qBittorrent is still deleting the real, mounted file. So a phantom
    verbatim path falls through to the proof-gated remap, which prefers the real
    mounted path.

    Called BEFORE the torrent is removed (unlike the poll itself): the proof
    needs the client's own file list, which ``list_files`` can no longer answer
    once the torrent is gone. Returns ``None`` — the caller then skips the poll
    rather than checking the wrong path — when there is no live ``save_path``
    anchor, when ``list_files`` itself fails (a client hiccup here must not
    block the removal that's about to happen regardless), or when no candidate
    is proven.
    """
    if await asyncio.to_thread(os.path.exists, content_path) and await asyncio.to_thread(
        path_visibility.content_is_mounted, content_path
    ):
        return content_path
    if not save_path:
        # No live anchor to remap against (a torrent status with no save path):
        # only the verbatim path counts, exactly as ``_resolve_visible_content``
        # documents — a free suffix search would reintroduce the stale-match
        # hazard :func:`path_visibility.remap_download_content` exists to close.
        return None
    try:
        files = await qbt.list_files(torrent_hash)
    except Exception:
        # Best-effort: a failed file-list fetch just means there's nothing safely
        # provable to poll — never a reason to hold up the torrent removal itself.
        return None
    expected = [(entry.name, entry.size_bytes) for entry in files]
    return await asyncio.to_thread(
        path_visibility.remap_download_content, content_path, save_path, expected
    )


async def _wait_for_content_path_gone(
    content_path: str, *, context: str, extra: dict[str, object] | None
) -> None:
    """Bounded poll for a just-removed torrent's ``content_path`` to leave disk.

    Closes the post-ack residual documented on :func:`remove_torrent`: qBittorrent
    ACKs ``/torrents/delete`` before its own server-side file removal necessarily
    finishes, so a same-hash re-grab landing right after the ACK can start writing
    fresh data at ``content_path`` while the OLD deletion is still tearing it down
    — the tail of that deletion can then clobber the new data. Polls
    ``os.path.exists`` off the event loop (mirrors every other blocking FS probe in
    this module/``import_service``), bounded by
    ``_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS`` so this best-effort check can never
    hang a caller indefinitely (it runs inline in operator-facing correction
    endpoints, not just the reconcile background loop). A path still present once
    the bound elapses is logged (honesty over silence) and left as-is — the
    caller's DB state change already committed, and every actor's removal-physics
    guard release proceeds regardless; a client this slow to finish its own
    deletion is a pre-existing risk this poll narrows, not a new one it must fully
    eliminate.
    """
    deadline = time.monotonic() + _CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS
    while True:
        try:
            still_present = await asyncio.to_thread(os.path.exists, content_path)
        except OSError:
            # An unreadable path (e.g. a parent directory removed out from under
            # it) is as good as gone for this best-effort check.
            still_present = False
        if not still_present:
            return
        if time.monotonic() >= deadline:
            _logger.warning(
                "content path still present %.1fs after %s's torrent removal "
                "was acknowledged; a fast same-hash re-grab could still race the "
                "client's own asynchronous file deletion",
                _CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS,
                context,
                extra=extra,
            )
            return
        await asyncio.sleep(_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS)
