"""Shared correction primitives (ADR-0014): the mechanical, best-effort building
blocks the correction verbs (report-issue, cancel) and the disk-pressure eviction
sweep all compose, so the load-bearing safety logic lives in ONE place.

Four primitives: the first is infrastructure whose exceptions stay caller-owned;
the remaining three are best-effort by design (a failure is logged, never silent,
and never raised) — the DB state change a caller commits around them is the
authoritative record; a client/Plex/FS hiccup there must never undo it:

* :func:`run_abandonable_probe` — typed blocking, read-only filesystem work on a
  dedicated bounded daemon-thread substrate (its OWN permit budget, separate from
  deletes — see :data:`_ABANDONABLE_PROBE_THREAD_LIMIT`). Results and exceptions
  are delivered unchanged to the caller, and ordinary cancellation propagates
  PROMPTLY: the probe DETACHES, letting its daemon worker run to completion
  unobserved (it releases its own permit on that completion), rather than shielding
  the caller until physical settlement the way a destructive delete does.
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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal

from plex_manager.adapters.filesystem.local import LocalFileSystemError
from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.logsafe import safe_text
from plex_manager.services import path_visibility

if TYPE_CHECKING:
    from plex_manager.ports.download_client import DownloadClientPort, DownloadStatus
    from plex_manager.ports.filesystem import FileSystemPort
    from plex_manager.ports.library import LibraryPort

__all__ = [
    "PurgeOutcome",
    "PurgeResult",
    "abandon_active_settlements",
    "active_purge_paths",
    "active_settlement_tasks",
    "begin_placement",
    "end_placement",
    "end_purge",
    "purge_library_path",
    "remove_torrent",
    "run_abandonable_probe",
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

_ABANDONABLE_DELETE_THREAD_LIMIT: Final = 4
"""Maximum simultaneous DESTRUCTIVE delete workers on the abandonable substrate.

A dead mount can block an OS thread permanently, so every ``shutil.rmtree``
delete worker shares this small process-wide budget. Four preserves modest
parallelism for unrelated operator corrections while putting a hard ceiling on
daemon threads. A :class:`concurrent.futures.ThreadPoolExecutor` is deliberately
forbidden because its non-daemon workers are joined at interpreter exit, which
would reintroduce the shutdown hang this substrate exists to prevent (issue
#417 / PR #406).
"""
_ABANDONABLE_PROBE_THREAD_LIMIT: Final = 4
"""Maximum simultaneous READ-ONLY probe workers on the abandonable substrate.

A SEPARATE budget from :data:`_ABANDONABLE_DELETE_THREAD_LIMIT` (issue #447):
read-only probes (``statvfs``/disk-usage, the delete-guard containment check,
the reclaimable-bytes walk) share the same dead-mount hazard as a delete, but
partitioning their permits means a burst of probes wedged against a hung root
can never hold every permit and starve a later report-issue / cancel / eviction
DELETE — the two never compete for the same tokens. Four mirrors the delete
budget: enough parallelism for the health dashboard's several roots plus the
ops-disk / eviction-preview / sweep / telemetry reads, with the same hard
daemon-thread ceiling. Because a cancelled probe DETACHES rather than shields
(issue #445), a wedged probe still holds its permit until its own worker
physically finishes, so this cap is what bounds concurrent wedged probes.
"""
_ABANDONABLE_THREAD_GATE_POLL_SECONDS: Final = 0.01


class _AbandonableThreadGate:
    """Thread-safe physical-worker permits with a cancellable asyncio wait.

    The permit store must outlive any one event loop: an abandoned daemon worker
    can finish after its originating loop closes, and its foreign thread cannot
    safely release an :class:`asyncio.Semaphore`. A bounded threading semaphore
    makes physical completion safe on that thread. The non-blocking poll keeps
    queued callers on ordinary cancellable asyncio awaits without consuming an
    executor thread that cancellation could strand (issue #417).
    """

    def __init__(self, limit: int) -> None:
        self._semaphore = threading.BoundedSemaphore(limit)

    async def acquire(self) -> _AbandonableThreadPermit:
        """Wait cancellably and return one idempotent permit token."""
        while not self._semaphore.acquire(blocking=False):
            await asyncio.sleep(_ABANDONABLE_THREAD_GATE_POLL_SECONDS)
        return _AbandonableThreadPermit(self)

    def release_permit(self) -> None:
        """Return a permit from either an event-loop or worker thread."""
        self._semaphore.release()


class _AbandonableThreadPermit:
    """Release one gate permit at most once across competing terminal paths.

    The worker-completion and thread-start-failure paths are mutually exclusive
    today. The lock is deliberate defense in depth: release can originate from a
    foreign daemon thread after loop teardown, so future control-flow changes
    must not turn an accidental second release into substrate-cap corruption.
    """

    def __init__(self, gate: _AbandonableThreadGate) -> None:
        self._gate = gate
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        """Return the permit exactly once; later release attempts are no-ops."""
        with self._lock:
            if self._released:
                return
            self._released = True
        self._gate.release_permit()


_ABANDONABLE_DELETE_THREAD_GATE = _AbandonableThreadGate(_ABANDONABLE_DELETE_THREAD_LIMIT)
_ABANDONABLE_PROBE_THREAD_GATE = _AbandonableThreadGate(_ABANDONABLE_PROBE_THREAD_LIMIT)

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
# Every shielded settlement registers its event-loop future here. Lifespan's
# bounded shutdown path resolves all of them at once, including settlements in
# uvicorn request/protocol tasks that are not app-owned background tasks. The
# loop is single-threaded, so register/snapshot/resolve is atomic without a
# lock; daemon workers only deliver through ``call_soon_threadsafe``.
_ACTIVE_SETTLEMENTS: dict[asyncio.Future[None], tuple[str, asyncio.Task[None]]] = {}
_ABANDONED_SETTLEMENTS: set[asyncio.Future[None]] = set()


def active_settlement_tasks() -> tuple[asyncio.Task[None], ...]:
    """Snapshot tasks awaiting abandonable filesystem-worker settlement."""
    return tuple(task for _path, task in _ACTIVE_SETTLEMENTS.values())


def abandon_active_settlements() -> None:
    """Wake active filesystem settlements without waiting for daemon workers.

    This process-shutdown escape hatch snapshots only currently active work.
    Resolving each settlement future makes background and request-scoped purge
    tasks genuinely finish before event-loop teardown. The daemon worker may
    still mutate disk afterward; issue #128's crash-recovery sweep owns
    reconciling that partial state on the next process start.
    """
    for settlement in tuple(_ACTIVE_SETTLEMENTS):
        _ABANDONED_SETTLEMENTS.add(settlement)
        if not settlement.done():
            settlement.set_result(None)


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


def active_purge_paths() -> tuple[str, ...]:
    """Snapshot of library paths with an in-flight purge delete (issue #401).

    Read-only view of :data:`_ACTIVE_PURGE_PATHS`'s keys. Used by
    ``web.app.lifespan``'s bounded shutdown wait to name which path(s) are
    still being deleted if that wait times out (honesty over silence) -- never
    used for the purge-vs-import serialization itself, which reads/writes the
    registry directly.
    """
    return tuple(_ACTIVE_PURGE_PATHS)


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


def _start_on_abandonable_thread[T](
    operation: Callable[[], T],
    *,
    thread_name: str,
    permit: _AbandonableThreadPermit,
) -> asyncio.Future[T]:
    """Synchronously start one abandonable daemon worker under an ALREADY-HELD permit.

    Split out of :func:`_run_on_abandonable_thread` (issue #431) so a caller that
    must keep the physical worker inside its OWN cancellation-cleanup coverage --
    see :func:`_delete_to_settlement` -- can acquire the gate permit and then
    create and store the worker future with NO ``await`` between the two. A
    cancellation delivered at that boundary would otherwise leave a live daemon
    thread with no future the caller can observe, either leaking the registration
    forever or releasing it while the thread still runs.

    ``permit`` is owned by the physical worker from here on: it is returned on the
    worker's physical-completion ``finally`` AND if ``Thread.start()`` itself
    raises, but NEVER on caller cancellation or settlement abandonment -- a wedged
    thread must not make room for another while it remains stuck (issue #417). The
    closed-loop late-delivery guard is preserved unchanged: a worker that finishes
    after its originating loop has torn down absorbs only the expected
    ``RuntimeError`` from ``call_soon_threadsafe``.
    """
    loop = asyncio.get_running_loop()
    outcome: asyncio.Future[T] = loop.create_future()

    def _deliver(result: T | None, error: BaseException | None) -> None:
        # Shutdown abandonment can leave the loop alive briefly while this
        # callback is already queued. Nobody remains entitled to consume a late
        # result, so a completed future is expected rather than an error.
        if outcome.done():
            return
        if error is None:
            outcome.set_result(result)  # pyright: ignore[reportArgumentType]
        else:
            outcome.set_exception(error)

    def _worker() -> None:
        result: T | None = None
        error: BaseException | None = None
        try:
            result = operation()
        except BaseException as exc:  # delivered to the awaiter, never swallowed
            error = exc
        # ``RuntimeError: Event loop is closed`` is the one expected late-
        # delivery failure after shutdown abandonment: nobody can consume the
        # outcome and crash recovery owns the disk state. Keep this guard at
        # the thread-to-loop boundary rather than suppressing worker failures,
        # which are delivered unchanged while the loop is live.
        try:
            loop.call_soon_threadsafe(_deliver, result, error)
        except RuntimeError:
            if not loop.is_closed():
                raise
        finally:
            permit.release()

    try:
        threading.Thread(target=_worker, name=thread_name, daemon=True).start()
    except BaseException:
        permit.release()
        raise
    return outcome


async def _run_on_abandonable_thread[T](
    operation: Callable[[], T], *, thread_name: str
) -> asyncio.Future[T]:
    """Run read-only blocking filesystem work on the bounded PROBE substrate.

    Deliberately NOT ``asyncio.to_thread`` (codex #406 P1 / issue #401): the
    default executor's non-daemon workers are joined during interpreter
    teardown. Any read-only filesystem operation that can touch a dead mount --
    the delete guard's containment check, reclaimable-bytes accounting, a
    ``statvfs`` disk-usage read -- must therefore use this abandonable substrate.
    Destructive deletes drive the SEPARATE delete gate themselves
    (:func:`_delete_to_settlement`), so they never contend for a probe permit
    (issue #447).

    The gate wait stays a plain cancellable await so a queued caller can unwind
    during shutdown without ever creating a physical worker. Once acquired, its
    permit belongs to that worker until its physical completion ``finally``
    releases the thread-safe token -- caller cancellation (a detached probe, issue
    #445) or settlement abandonment must never make room for another thread while
    the original remains wedged (issue #417). Permit acquisition and the
    synchronous worker start are split (:func:`_start_on_abandonable_thread`) so a
    caller needing the worker inside its own cleanup coverage can drive the two
    steps itself.
    """
    permit = await _ABANDONABLE_PROBE_THREAD_GATE.acquire()
    return _start_on_abandonable_thread(operation, thread_name=thread_name, permit=permit)


async def _await_worker_settlement[T](
    worker: asyncio.Future[T], library_path: str, *, operation: str
) -> T:
    """Shield one abandonable DELETE worker through real or abandoned settlement.

    The shielding here is the destructive-delete substrate property (issue #128 /
    PR #395): a caller's cancellation is not observed until the underlying delete
    thread has genuinely settled (or process-shutdown abandonment forces an early
    return via :func:`abandon_active_settlements`). Read-only probes deliberately
    do NOT use this path -- :func:`run_abandonable_probe` detaches promptly on
    cancellation (issue #445) because a read has no partial disk mutation to
    protect.
    """
    settled: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    current_task = asyncio.current_task()
    if current_task is None:  # pragma: no cover - running coroutine always owns a task
        raise RuntimeError("filesystem settlement requires an asyncio task")
    _ACTIVE_SETTLEMENTS[settled] = (library_path, current_task)
    worker_error: BaseException | None = None

    def _consume_worker_result(done: asyncio.Future[T]) -> None:
        nonlocal worker_error
        if not done.cancelled():
            worker_error = done.exception()
        if not settled.done():
            settled.set_result(None)

    worker.add_done_callback(_consume_worker_result)

    was_cancelled = False
    try:
        while not settled.done():
            try:
                await asyncio.shield(settled)
            except asyncio.CancelledError:
                was_cancelled = True
        if settled in _ABANDONED_SETTLEMENTS:
            # The daemon thread remains abandonable, but the asyncio task must
            # not remain pending for ``asyncio.run``'s final cancel-and-gather,
            # so this coroutine returns early via CancelledError. That early
            # return does NOT, by itself, release any ``_ACTIVE_PURGE_PATHS``
            # registration: for a delete, :func:`_delete_to_settlement` decides
            # hold-vs-release from its OWN outcome and defers the actual
            # unregister to the raw delete worker's physical completion (issue
            # #431), so the path stays claimed until the ``shutil.rmtree`` thread
            # genuinely finishes even though this settlement was abandoned. Only
            # a process that exits before the daemon thread finishes at all is
            # left to the next startup's crash-recovery sweep to reconcile,
            # exactly as after a crash mid-delete (#128).
            _logger.warning(
                "filesystem settlement abandoned during process shutdown while %s "
                "of %r was still active; process exit will reclaim the worker and "
                "crash recovery will reconcile any partial disk mutation on next startup",
                operation,
                safe_text(library_path),
            )
            raise asyncio.CancelledError
        if was_cancelled:
            if worker_error is not None:
                # Cancellation wins: the caller is already unwinding on
                # CancelledError and will never read a classified PurgeResult, so a
                # worker error that ALSO happened during that unwind is logged here
                # (honesty over silence) rather than raised over the cancellation.
                _logger.warning(
                    "filesystem operation %s on %r failed (%s) while the caller "
                    "was being cancelled; the operation did not complete",
                    operation,
                    safe_text(library_path),
                    type(worker_error).__name__,
                )
            raise asyncio.CancelledError
        if worker_error is not None:
            raise worker_error
        return worker.result()
    finally:
        was_abandoned = settled in _ABANDONED_SETTLEMENTS
        _ACTIVE_SETTLEMENTS.pop(settled, None)
        _ABANDONED_SETTLEMENTS.discard(settled)
        if worker.done():
            # Retrieve a late worker exception if abandonment raced completion;
            # otherwise the normal callback above already consumed it.
            if not worker.cancelled():
                worker.exception()
        elif not was_abandoned:
            worker.remove_done_callback(_consume_worker_result)


async def run_abandonable_probe[T](
    operation: Callable[[], T], path: str, *, operation_name: str
) -> T:
    """Run one blocking read-only filesystem probe that detaches on cancellation.

    This is the public, purge-agnostic entry point to the read-only PROBE
    substrate. Filesystem reads such as ``statvfs`` can wedge on a dead mount just
    as permanently as a delete; using ``asyncio.to_thread`` for them would strand a
    non-daemon default-executor worker that CPython rejoins during interpreter
    teardown, defeating the web lifespan's bounded shutdown wait. The physical
    worker therefore runs on the abandonable daemon-thread substrate, consuming a
    permit from the DEDICATED probe budget (:data:`_ABANDONABLE_PROBE_THREAD_LIMIT`,
    issue #447) so a wedged probe can never starve a destructive delete.

    Unlike a delete, a read has no partial disk mutation to protect, so this does
    NOT shield the caller through physical settlement (issue #445). On ORDINARY
    cancellation the probe DETACHES: :class:`asyncio.CancelledError` propagates to
    the caller promptly (``asyncio.shield`` protects the worker future while
    unwinding the await), the daemon worker runs to completion unobserved and
    releases its OWN permit on that completion (the permit is bound to physical
    thread completion in :func:`_start_on_abandonable_thread`, never to this
    coroutine), and ``asyncio.shield`` consumes the worker's eventual result or
    exception so a late delivery is never reported as "never retrieved". This is
    also what keeps process shutdown bounded WITHOUT any settlement registration:
    the lifespan's cancellation of a probe-bearing task unwinds it at once rather
    than blocking on a detached daemon worker.

    ``operation`` results and exceptions are delivered unchanged during ordinary
    operation. In particular, callers retain ownership of narrow classifications
    such as ``OSError``; this substrate adds no retries, fallback values, or broad
    exception conversion. ``path`` and ``operation_name`` exist only to make a
    wedged-probe detach honest and diagnosable without logging probe results.
    """
    worker = await _run_on_abandonable_thread(operation, thread_name="filesystem-probe")
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if not worker.done():
            # A read-only probe left running on a (likely wedged) mount: the caller
            # unwinds now, the daemon worker keeps going and returns its own permit
            # when it physically finishes. Log so an abandoned probe worker is
            # visible (honesty over silence) rather than a silent detach.
            _logger.warning(
                "%s of %r detached on caller cancellation; its daemon worker will "
                "run to completion unobserved and then release its probe permit",
                operation_name,
                safe_text(path),
            )
        raise


async def _delete_to_settlement(
    fs: FileSystemPort, library_path: str, *, hold_purge_registration: bool
) -> None:
    """Run delete to real settlement (except process-shutdown abandonment) and own
    the ``_ACTIVE_PURGE_PATHS`` release decision from this coroutine's OWN outcome.

    The physical delete worker is created INSIDE this coroutine's cleanup
    ``try``/``finally`` (issue #431): the gate permit is acquired first, then the
    worker future is created and stored with NO intervening ``await``
    (:func:`_start_on_abandonable_thread`), so every terminal path resolves the
    registration exactly once, right here -- cancelled while still queued for a
    permit (no worker yet), a ``Thread.start()`` failure (starter returned the
    permit, ``worker`` still ``None``), a worker error, ordinary cancellation,
    shutdown abandonment, or success.

    Hold-vs-release is decided from ``succeeded`` -- this coroutine's own
    definitive outcome, set ``True`` ONLY after ``_await_worker_settlement``
    returns without raising -- never from a snapshot taken inside a worker-done
    callback that races the caller's resumption. That was the #421 first-attempt
    trap: a callback reading ``Task.cancelling()`` can fire while that counter
    still reads 0, with the task's own ``CancelledError`` delivered only on its
    next resume, AFTER the premature decision to hold, permanently leaking the
    registration. A caller may keep the claim past a successful delete (releasing
    it itself via :func:`end_purge` after its own DB commit) ONLY when this
    coroutine actually observed that success; a failed, cancelled, or abandoned
    delete is always released here, exactly as :func:`purge_library_path`'s own
    pre-#421 ``finally`` behaved.

    The ``_unregister`` itself must not run before the daemon worker has
    physically finished touching disk: if the worker is already done at decision
    time (every non-abandonment path -- ``_await_worker_settlement`` only returns
    or raises once the worker settled), release happens immediately; otherwise
    (shutdown abandonment resolved the settlement early while the
    ``shutil.rmtree`` thread may still be running) release is deferred to a
    done-callback on the RAW worker future, which the single-threaded loop runs
    only once the worker genuinely settles -- closing the abandonment-to-exit
    window where a live :func:`begin_placement` caller could otherwise claim a
    path an abandoned delete is still tearing down. A worker still running when
    its originating loop closes is process-exit territory (issue #128 crash
    recovery), not an in-process lifecycle event.
    """
    worker: asyncio.Future[None] | None = None
    succeeded = False
    try:
        permit = await _ABANDONABLE_DELETE_THREAD_GATE.acquire()
        # No ``await`` between the permit acquisition above and storing ``worker``
        # below: the synchronous starter creates the daemon thread and hands back
        # its future atomically, so cancellation can never strand a live worker.
        worker = _start_on_abandonable_thread(
            lambda: fs.delete(library_path), thread_name="purge-delete", permit=permit
        )
        await _await_worker_settlement(worker, library_path, operation="delete")
        succeeded = True
    finally:
        if not (succeeded and hold_purge_registration):
            if worker is None or worker.done():
                _unregister(library_path, _ACTIVE_PURGE_PATHS)
            else:
                worker.add_done_callback(
                    lambda _done: _unregister(library_path, _ACTIVE_PURGE_PATHS)
                )


async def purge_library_path(
    fs: FileSystemPort, library_path: str, *, hold_purge_registration: bool = False
) -> PurgeResult:
    """Root-guarded delete of ``library_path`` + hardlink-aware freed-bytes accounting.

    Both the size accounting and the delete are real, synchronous disk I/O
    (``os.stat``/``os.walk``/``shutil.rmtree``), so all three filesystem phases
    (guard, accounting, delete) run on dedicated abandonable daemon threads.
    A dead mount can wedge any probe just as surely as the final delete, and a
    default-executor worker would be rejoined by ``asyncio.run`` at teardown.
    The delete specifically goes through :func:`_delete_to_settlement`, which
    shields the wait so a caller's cancellation is never observed until the
    underlying delete thread has genuinely finished (issue #128). Once this
    function hands the registration off to that helper (the one-way
    ``registration_handed_off`` flag set immediately before the ``await`` below),
    the helper owns EVERY terminal path of the ``_ACTIVE_PURGE_PATHS`` claim:
    success (held or released), a guard/OS failure, ordinary cancellation, AND
    process-shutdown abandonment all resolve it inside the helper's own
    ``finally``, tied to the delete worker's PHYSICAL completion — so a
    concurrent ``begin_placement`` / a later sweep's crash-recovery can never see
    this path as free while a delete for it is still physically running.

    CLOSED (issue #431): that invariant now holds THROUGH process-shutdown
    abandonment too. :func:`abandon_active_settlements` (PR #406) can force
    :func:`_delete_to_settlement`'s shielded wait to resolve without the daemon
    ``shutil.rmtree`` thread ever finishing, but the helper does NOT release the
    registration on that early resolution: with the worker not yet physically
    done, it defers the ``_unregister`` to a done-callback on the raw delete
    worker future, which the single-threaded loop runs only once the thread
    genuinely stops touching disk. The regression test
    ``test_begin_placement_refuses_while_an_abandoned_delete_still_runs`` in
    ``tests/web/test_shutdown_wait.py`` drives exactly that abandonment for a
    real, still-running delete and proves :func:`begin_placement` keeps refusing
    the path until the daemon thread physically completes, then succeeds once it
    does. Issue #128's crash-recovery sweep on the *next* startup remains the
    backstop only for the genuinely-abandoned case where the process exits before
    the daemon thread finishes at all — this fix closes the in-process
    abandonment-to-exit ``begin_placement`` race, not the OS-level abandonment.
    (A separate, accepted process-exit-only gap remains: ``eviction_service``'s
    ``_sweep_latch`` still clears on that same abandoned-cancellation unwind; it
    was never a physical-completion tracker, and reconciling it is out of scope.)

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
    registration_handed_off = False
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
        if await run_abandonable_probe(
            lambda: fs.delete_guard_refuses(library_path),
            library_path,
            operation_name="delete-guard probe",
        ):
            return PurgeResult(
                PurgeOutcome.refused, 0, "path resolves outside every configured library root"
            )

        # Reclaimable bytes MUST be read before the delete: a file's link count is
        # only knowable while the path still exists (hardlink-aware accounting,
        # ADR-0012 / ADR-0014). A measurement failure is "unknown -> 0", never an
        # abort.
        try:
            freed_bytes = await run_abandonable_probe(
                lambda: fs.reclaimable_bytes(library_path),
                library_path,
                operation_name="reclaimable-bytes probe",
            )
        except OSError:
            freed_bytes = 0

        # HANDOFF (issue #431): from here :func:`_delete_to_settlement` owns
        # EVERY terminal path of this ``_ACTIVE_PURGE_PATHS`` registration --
        # success (held or released), a guard/OS delete failure, ordinary
        # cancellation, and shutdown abandonment all resolve it inside that
        # coroutine's own ``finally``, tied to the delete worker's physical
        # completion. Set the one-way flag BEFORE the ``await`` so this
        # function's own ``finally`` never double-releases once the callee has
        # taken ownership. Everything ABOVE this point (the placement-conflict
        # deferral, the read-only guard/reclaim probes) started no destructive
        # work, so a return/cancellation there still releases via this
        # ``finally``; only the delete's registration is handed off.
        registration_handed_off = True
        try:
            await _delete_to_settlement(
                fs, library_path, hold_purge_registration=hold_purge_registration
            )
        except LocalFileSystemError as exc:
            return PurgeResult(PurgeOutcome.refused, 0, str(exc))
        except OSError as exc:
            return PurgeResult(PurgeOutcome.error, 0, type(exc).__name__)
        return PurgeResult(PurgeOutcome.deleted, freed_bytes)
    finally:
        if not registration_handed_off:
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
