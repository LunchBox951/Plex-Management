"""Shared correction primitives (ADR-0014): root-guarded purge, scan, torrent remove.

Uses the REAL ``LocalFileSystem`` against ``tmp_path`` so the root-containment
guard is genuinely exercised (the same posture as ``test_eviction_service``).
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import cast

import pytest

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.ports.download_client import (
    AddResult,
    DownloadClientPort,
    DownloadedFile,
    DownloadStatus,
    FailureDetail,
)
from plex_manager.services import path_visibility, purge_service
from plex_manager.services.purge_service import PurgeOutcome
from tests.support import assert_task_raises
from tests.web.fakes import FakeLibrary, FakeQbittorrent


async def test_purge_deletes_an_in_root_file_and_reports_freed_bytes(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    target = root / "Some Movie (2020).mkv"
    target.write_bytes(b"x" * 2048)
    fs = LocalFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(target))

    assert result.outcome is PurgeOutcome.deleted
    assert result.freed_bytes == 2048
    assert not target.exists()


class _BlockedDeleteFileSystem(LocalFileSystem):
    """A real, root-guarded :class:`LocalFileSystem` whose ``delete`` blocks (in
    its worker thread) until the test releases it -- lets a test cancel the
    AWAITING coroutine while the underlying delete thread is still running, the
    exact window issue #128 needs exercised."""

    def __init__(self, root: Path) -> None:
        super().__init__([str(root)])
        self.started = threading.Event()
        self.release = threading.Event()
        self.deleted: list[str] = []

    def delete(self, path: str) -> None:
        self.started.set()
        self.release.wait(timeout=5)
        super().delete(path)
        self.deleted.append(path)


class _FailingBlockedDeleteFileSystem(_BlockedDeleteFileSystem):
    """Like :class:`_BlockedDeleteFileSystem`, but the delete worker fails
    (``OSError``) once released instead of succeeding."""

    def delete(self, path: str) -> None:
        self.started.set()
        self.release.wait(timeout=5)
        raise OSError("blocked delete failed")


class _BlockedGuardFileSystem(LocalFileSystem):
    """Block every delete-guard probe and expose physical worker progress."""

    def __init__(self, root: Path, *, expected_starts: int) -> None:
        super().__init__([str(root)])
        self.release = threading.Event()
        self.expected_started = threading.Event()
        self.finished = threading.Event()
        self._expected_starts = expected_starts
        self._lock = threading.Lock()
        self._started_count = 0
        self._finished_count = 0

    @property
    def started_count(self) -> int:
        with self._lock:
            return self._started_count

    def delete_guard_refuses(self, path: str) -> bool:
        with self._lock:
            self._started_count += 1
            call_number = self._started_count
            if self._started_count == self._expected_starts:
                self.expected_started.set()
        self.release.wait(timeout=5)
        try:
            return super().delete_guard_refuses(path)
        finally:
            with self._lock:
                self._finished_count += 1
                if self._finished_count == self._expected_starts:
                    self.finished.set()
            self._after_guard(call_number)

    def _after_guard(self, _call_number: int) -> None:
        pass


class _FailingFirstBlockedGuardFileSystem(_BlockedGuardFileSystem):
    """Raise from the first physical guard worker after its gate opens."""

    def _after_guard(self, call_number: int) -> None:
        if call_number == 1:
            raise OSError("blocked guard failed")


def _install_abandonable_probe_gate(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    """Install a test-local PROBE physical-worker limit (issue #447 split the
    probe budget from the delete budget; the guard/reclaim probes these tests
    drive run on the probe gate)."""
    monkeypatch.setattr(
        purge_service,
        "_ABANDONABLE_PROBE_THREAD_GATE",
        purge_service._AbandonableThreadGate(limit),  # pyright: ignore[reportPrivateUsage]
        raising=False,
    )


def _install_abandonable_delete_gate(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    """Install a test-local DELETE physical-worker limit (the separate destructive
    budget, issue #447)."""
    monkeypatch.setattr(
        purge_service,
        "_ABANDONABLE_DELETE_THREAD_GATE",
        purge_service._AbandonableThreadGate(limit),  # pyright: ignore[reportPrivateUsage]
        raising=False,
    )


class _CountingThreadGate:
    """Wrap a real gate and record every ``acquire`` so a test can prove which
    budget a given path actually draws from (issue #447): probes and deletes must
    draw from SEPARATE gates, so routing a delete back through the probe gate has
    to be demonstrably detectable, not merely tolerated by a spare shared permit.
    """

    def __init__(self, limit: int) -> None:
        self._inner = purge_service._AbandonableThreadGate(limit)  # pyright: ignore[reportPrivateUsage]
        self.acquired = 0

    async def acquire(self) -> purge_service._AbandonableThreadPermit:  # pyright: ignore[reportPrivateUsage]
        self.acquired += 1
        return await self._inner.acquire()

    def release_permit(self) -> None:
        self._inner.release_permit()


async def test_abandonable_thread_cap_queues_the_next_worker_until_one_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #417: N physical workers consume the whole substrate; N+1 stays
    queued without creating another OS thread until one of those workers really
    finishes and returns its permit."""
    worker_limit = 2
    _install_abandonable_probe_gate(monkeypatch, worker_limit)
    root = tmp_path / "movies"
    root.mkdir()
    targets = [root / f"Blocked {index}.mkv" for index in range(worker_limit + 1)]
    for target in targets:
        target.write_bytes(b"x")
    fs = _BlockedGuardFileSystem(root, expected_starts=worker_limit)
    tasks = [
        asyncio.create_task(purge_service.purge_library_path(fs, str(target))) for target in targets
    ]
    try:
        assert await asyncio.to_thread(fs.expected_started.wait, 2.0)
        await asyncio.sleep(0)
        assert fs.started_count == worker_limit
    finally:
        fs.release.set()
        results = await asyncio.gather(*tasks)

    assert all(result.outcome is PurgeOutcome.deleted for result in results)
    assert fs.started_count == worker_limit + 1


async def test_probe_permit_is_held_by_the_detached_worker_after_caller_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #445/#447: a cancelled probe awaiter DETACHES promptly (its task
    finishes on ``CancelledError`` at once, unlike a shielded delete), but the
    probe's permit stays bound to its daemon worker's PHYSICAL completion -- so a
    second probe queued behind it on the limit-1 probe gate cannot start until the
    wedged worker actually finishes and returns its own permit."""
    _install_abandonable_probe_gate(monkeypatch, 1)
    root = tmp_path / "movies"
    root.mkdir()
    first = root / "First.mkv"
    second = root / "Second.mkv"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    fs = _BlockedGuardFileSystem(root, expected_starts=1)
    first_task = asyncio.create_task(purge_service.purge_library_path(fs, str(first)))
    second_task: asyncio.Task[purge_service.PurgeResult] | None = None
    second_result: purge_service.PurgeResult | None = None
    try:
        assert await asyncio.to_thread(fs.expected_started.wait, 2.0)
        first_task.cancel()
        # The probe detaches instead of shielding, so cancellation unwinds the
        # caller PROMPTLY -- the worker thread stays wedged in the background.
        await assert_task_raises(first_task, asyncio.CancelledError)

        second_task = asyncio.create_task(purge_service.purge_library_path(fs, str(second)))
        await asyncio.sleep(0)
        # The detached worker still holds the only permit, so the second probe is
        # queued and no second physical worker has started.
        assert fs.started_count == 1
    finally:
        fs.release.set()
        if second_task is not None:
            second_result = await second_task

    assert second_result is not None
    assert second_result.outcome is PurgeOutcome.deleted
    assert fs.started_count == 2


async def test_wedged_probe_budget_does_not_starve_a_delete_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #447: probes and deletes draw from SEPARATE permit budgets, so a
    probe worker wedged on a dead mount holding the ENTIRE probe budget cannot
    block a delete from acquiring a delete permit.

    Both gates are limit-1 spies. The wedged probe saturates the probe gate, then
    the delete must complete by drawing from its OWN gate. The spies assert the
    routing, not just the outcome: routing the delete back through the probe gate
    (un-partitioning) would both TIME OUT the wait below AND leave
    ``probe_gate.acquired == 2`` -- so this test demonstrably fails without the
    partition, rather than passing on a spare permit from an unrelated shared gate.
    """
    probe_gate = _CountingThreadGate(1)
    delete_gate = _CountingThreadGate(1)
    monkeypatch.setattr(purge_service, "_ABANDONABLE_PROBE_THREAD_GATE", probe_gate)
    monkeypatch.setattr(purge_service, "_ABANDONABLE_DELETE_THREAD_GATE", delete_gate)
    root = tmp_path / "movies"
    root.mkdir()
    probe_target = root / "Wedged Probe.mkv"
    probe_target.write_bytes(b"x")
    probe_fs = _BlockedGuardFileSystem(root, expected_starts=1)
    probe_task = asyncio.create_task(purge_service.purge_library_path(probe_fs, str(probe_target)))
    try:
        assert await asyncio.to_thread(probe_fs.expected_started.wait, 2.0)
        # Detach the probe: the caller unwinds, the worker stays wedged holding
        # the ONLY probe permit.
        probe_task.cancel()
        await assert_task_raises(probe_task, asyncio.CancelledError)
        assert probe_fs.started_count == 1
        assert probe_gate.acquired == 1  # only the wedged probe drew a probe permit

        # A delete must still acquire its own (separate) permit and complete,
        # despite the probe gate being fully saturated by the wedged worker.
        delete_target = root / "Deleted.mkv"
        delete_target.write_bytes(b"x")
        delete_fs = LocalFileSystem([str(root)])
        await asyncio.wait_for(
            purge_service._delete_to_settlement(  # pyright: ignore[reportPrivateUsage]
                delete_fs, str(delete_target), hold_purge_registration=False
            ),
            timeout=2.0,
        )
        assert not delete_target.exists()
        # The delete drew from its OWN gate and never touched the saturated probe
        # gate -- the concrete partition guarantee, not merely "a delete finished".
        assert delete_gate.acquired == 1
        assert probe_gate.acquired == 1
    finally:
        probe_fs.release.set()
        assert await asyncio.to_thread(probe_fs.finished.wait, 2.0)


async def test_detached_probe_worker_failure_is_retrieved_not_reported_unretrieved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #445: a probe that detached on cancellation whose daemon worker LATER
    fails must have that exception RETRIEVED. ``asyncio.shield`` drops its observer
    on the inner worker future when the caller's await is cancelled, so without an
    explicit done-callback the eventual failure surfaces on the loop as
    "Future exception was never retrieved" -- a swallowed-error violation of the
    honesty north star.

    Driven deterministically via a hand-controlled worker future: the guard probe
    detaches on cancel, then the future is failed with no awaiter. Without the fix
    the loop's exception handler records the unretrieved-exception context; with it
    the attached callback consumes the exception and the handler stays silent.
    """
    root = tmp_path / "movies"
    root.mkdir()
    target = root / "Wedged.mkv"
    target.write_bytes(b"x")
    fs = LocalFileSystem([str(root)])

    loop = asyncio.get_running_loop()
    # Held in a list so the test can drop its ONLY strong reference to the worker
    # future (``worker_box.clear()``) and let it be collected -- an unretrieved
    # exception is reported by ``Future.__del__``, so the future must be
    # collectible for the no-fix case to surface the violation.
    worker_box: list[asyncio.Future[object]] = [loop.create_future()]
    reached_probe = asyncio.Event()
    original_start = purge_service._start_on_abandonable_thread  # pyright: ignore[reportPrivateUsage]

    def _fake_start(
        operation: Callable[[], object],
        *,
        thread_name: str,
        permit: purge_service._AbandonableThreadPermit,  # pyright: ignore[reportPrivateUsage]
    ) -> asyncio.Future[object]:
        # Only the read-only guard probe is replaced with a hand-driven future;
        # nothing physical holds its permit, so return it to the gate immediately.
        if thread_name != "filesystem-probe":
            return original_start(operation, thread_name=thread_name, permit=permit)
        permit.release()
        reached_probe.set()
        return worker_box[0]

    monkeypatch.setattr(purge_service, "_start_on_abandonable_thread", _fake_start)

    loop_errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        purge_task = asyncio.create_task(purge_service.purge_library_path(fs, str(target)))
        assert await asyncio.wait_for(reached_probe.wait(), timeout=2.0)
        purge_task.cancel()
        await assert_task_raises(purge_task, asyncio.CancelledError)
        # The detached worker now fails; its exception must be consumed by the
        # callback run_abandonable_probe attached, not left for the loop to report.
        worker_box[0].set_exception(OSError("wedged probe finally failed"))
        worker_box.clear()
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert loop_errors == []


async def test_abandonable_thread_worker_exception_releases_its_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A physically settled exception is still a settlement: its permit must be
    returned so the next queued worker can start."""
    _install_abandonable_probe_gate(monkeypatch, 1)
    root = tmp_path / "movies"
    root.mkdir()
    first = root / "Failing.mkv"
    second = root / "Following.mkv"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    fs = _FailingFirstBlockedGuardFileSystem(root, expected_starts=1)
    first_task = asyncio.create_task(purge_service.purge_library_path(fs, str(first)))
    second_task = asyncio.create_task(purge_service.purge_library_path(fs, str(second)))
    assert await asyncio.to_thread(fs.expected_started.wait, 2.0)
    assert fs.started_count == 1

    fs.release.set()
    with pytest.raises(OSError, match="blocked guard failed"):
        _ = await first_task
    second_result = await asyncio.wait_for(second_task, timeout=2.0)

    assert second_result.outcome is PurgeOutcome.deleted
    assert fs.started_count == 2


def test_abandonable_thread_releases_permit_after_originating_loop_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that physically finishes after its originating loop closes must
    return its process-wide permit for work submitted by a replacement loop."""
    _install_abandonable_probe_gate(monkeypatch, 1)
    root = tmp_path / "movies"
    root.mkdir()
    target = root / "Late.mkv"
    target.write_bytes(b"x")
    fs = _BlockedGuardFileSystem(root, expected_starts=1)

    old_loop = asyncio.new_event_loop()
    late_worker = old_loop.run_until_complete(
        purge_service._run_on_abandonable_thread(  # pyright: ignore[reportPrivateUsage]
            lambda: fs.delete_guard_refuses(str(target)), thread_name="purge-test-late"
        )
    )
    assert fs.expected_started.wait(timeout=2.0)
    late_worker.cancel()
    old_loop.close()

    fs.release.set()
    assert fs.finished.wait(timeout=2.0)

    async def _run_after_loop_restart() -> None:
        worker = await purge_service._run_on_abandonable_thread(  # pyright: ignore[reportPrivateUsage]
            lambda: None, thread_name="purge-test-restarted"
        )
        _ = await worker

    asyncio.run(asyncio.wait_for(_run_after_loop_restart(), timeout=2.0))


async def test_saturated_probe_gate_leaves_active_and_queued_callers_promptly_cancellable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #445/#447: with the probe gate saturated, a later caller waits on a
    plain cancellable gate await and cancel unwinds it at once (no worker ever
    created); the active probe likewise DETACHES promptly on cancel instead of
    shielding until settlement. Neither needs shutdown abandonment, and both
    release their pre-delete ``_ACTIVE_PURGE_PATHS`` registration on unwind."""
    _install_abandonable_probe_gate(monkeypatch, 1)
    root = tmp_path / "movies"
    root.mkdir()
    first = root / "Active.mkv"
    second = root / "Queued.mkv"
    first.write_bytes(b"x")
    second.write_bytes(b"x")
    fs = _BlockedGuardFileSystem(root, expected_starts=1)
    active = asyncio.create_task(purge_service.purge_library_path(fs, str(first)))
    queued = asyncio.create_task(purge_service.purge_library_path(fs, str(second)))
    try:
        assert await asyncio.to_thread(fs.expected_started.wait, 2.0)
        active.cancel()
        queued.cancel()
        # No abandonment call: the active probe detaches and the queued caller
        # (parked on the saturated gate with no worker) both unwind promptly.
        await assert_task_raises(queued, asyncio.CancelledError)
        await assert_task_raises(active, asyncio.CancelledError)
        assert fs.started_count == 1
        assert purge_service.active_purge_paths() == ()
    finally:
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)


async def test_cancelled_purge_keeps_path_registered_until_delete_worker_settles(
    tmp_path: Path,
) -> None:
    """Issue #128: cancelling ``purge_library_path`` mid-delete must NOT release
    its ``_ACTIVE_PURGE_PATHS`` registration (observed here via
    ``begin_placement``) until the background delete thread genuinely finishes
    -- otherwise a concurrent import placement (or, in ``eviction_service``, a
    later sweep's crash-recovery) can act on the path while a delete is still
    physically running against it."""
    target = tmp_path / "movies" / "Blocked Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)
    purge_task = asyncio.create_task(
        purge_service.purge_library_path(fs, str(target), hold_purge_registration=True)
    )
    cancelled = False
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        purge_task.cancel()
        # Give the cancellation a chance to be delivered; the purge must NOT
        # have completed (nor released its registration) yet -- the worker
        # thread is still blocked on ``fs.release``.
        await asyncio.sleep(0)
        assert not purge_task.done()
        assert purge_service.begin_placement(str(target)) is False
        assert target.exists()
    finally:
        fs.release.set()
        try:
            await purge_task
        except asyncio.CancelledError:
            cancelled = True

    assert cancelled is True
    assert fs.deleted == [str(target)]
    assert not target.exists()
    # Only NOW -- after the worker thread actually finished -- is the
    # registration released.
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))


async def test_cancelled_purge_with_a_failing_worker_raises_cancelled_not_the_worker_error(
    tmp_path: Path,
) -> None:
    """A cancellation racing an ALSO-failing delete must surface as the
    cancellation (the caller is already unwinding on it and will never read a
    classified ``PurgeResult``), never leak the worker's ``OSError`` as an
    unhandled exception on the event loop."""
    target = tmp_path / "movies" / "Failing Blocked Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _FailingBlockedDeleteFileSystem(target.parent)
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        purge_task = asyncio.create_task(purge_service.purge_library_path(fs, str(target)))
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        purge_task.cancel()
        await asyncio.sleep(0)
        assert not purge_task.done()
        fs.release.set()
        await assert_task_raises(purge_task, asyncio.CancelledError)
        # Let any deferred "exception never retrieved" callback run.
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert loop_errors == []
    assert target.exists()  # the (swallowed) delete never actually succeeded


async def test_uncancelled_delete_worker_error_propagates_as_an_error_outcome(
    tmp_path: Path,
) -> None:
    """Without any cancellation racing it, a genuine delete failure is still
    classified and returned as ``PurgeOutcome.error`` exactly as before -- the
    settlement shield changes nothing about the ordinary failure path."""
    target = tmp_path / "movies" / "Failing Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _FailingBlockedDeleteFileSystem(target.parent)
    fs.release.set()

    result = await purge_service.purge_library_path(fs, str(target))

    assert result.outcome is PurgeOutcome.error
    assert target.exists()


async def test_delete_to_settlement_propagates_a_worker_oserror_when_not_cancelled(
    tmp_path: Path,
) -> None:
    """Direct unit coverage of the settlement helper: an uncancelled worker
    failure is raised as itself, not swallowed."""
    target = tmp_path / "movies" / "Failing Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _FailingBlockedDeleteFileSystem(target.parent)
    fs.release.set()
    delete_to_settlement = cast(
        Callable[..., Awaitable[None]],
        purge_service.__dict__["_delete_to_settlement"],
    )

    with pytest.raises(OSError, match="blocked delete failed"):
        await delete_to_settlement(fs, str(target), hold_purge_registration=False)

    assert target.exists()


async def test_hold_registration_released_when_cancel_lands_after_worker_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #431 (the #421 first-attempt trap): a cancellation delivered AFTER
    the delete worker settles but BEFORE the caller resumes must still release the
    held registration -- the hold decision comes from the caller's real
    resumption, never a pre-resumption ``Task.cancelling()`` snapshot.

    Drives the exact interleaving deterministically via a hand-controlled worker
    future: ``set_result`` schedules the settlement callback FIFO, ``call_soon``
    queues the caller's ``cancel()`` behind it, and the settlement callback then
    queues the task wakeup behind that cancel -- so cancellation is applied after
    any premature hold snapshot would have been taken, but before the task
    actually resumes. The first #421 attempt held (and permanently leaked) the
    registration here; this must release it.
    """
    target = tmp_path / "movies" / "Raced Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = LocalFileSystem([str(target.parent)])

    loop = asyncio.get_running_loop()
    worker: asyncio.Future[None] = loop.create_future()
    reached_settlement = asyncio.Event()
    original_start = purge_service._start_on_abandonable_thread  # pyright: ignore[reportPrivateUsage]

    def _fake_start(
        operation: Callable[[], object],
        *,
        thread_name: str,
        permit: purge_service._AbandonableThreadPermit,  # pyright: ignore[reportPrivateUsage]
    ) -> asyncio.Future[object]:
        # Read-only guard/reclaim probes run on the real substrate; only the
        # destructive delete is replaced with a future the test drives by hand.
        if thread_name != "purge-delete":
            return original_start(operation, thread_name=thread_name, permit=permit)
        permit.release()  # nothing physical holds this permit in the fake
        reached_settlement.set()
        return cast("asyncio.Future[object]", worker)

    monkeypatch.setattr(purge_service, "_start_on_abandonable_thread", _fake_start)

    purge_task = asyncio.create_task(
        purge_service.purge_library_path(fs, str(target), hold_purge_registration=True)
    )
    assert await asyncio.wait_for(reached_settlement.wait(), timeout=2.0)
    await asyncio.sleep(0)
    # The delete registration blocks placement while the worker is unsettled.
    assert purge_service.begin_placement(str(target)) is False

    worker.set_result(None)
    loop.call_soon(purge_task.cancel)

    await assert_task_raises(purge_task, asyncio.CancelledError)

    # Released, not leaked, despite hold_purge_registration=True.
    assert purge_service.active_purge_paths() == ()
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))


class _BlockDeleteAcquireGate:
    """A real DELETE gate that parks the delete's ``acquire`` on a never-resolving
    (but cancellable) await -- so a caller can be cancelled while queued for a
    delete permit with NO worker yet. The guard/reclaim probes use the SEPARATE
    probe gate (issue #447), so this delete gate sees only the delete's single
    ``acquire``."""

    def __init__(self) -> None:
        self._inner = purge_service._AbandonableThreadGate(4)  # pyright: ignore[reportPrivateUsage]
        self.delete_acquire_reached = asyncio.Event()

    async def acquire(self) -> purge_service._AbandonableThreadPermit:  # pyright: ignore[reportPrivateUsage]
        self.delete_acquire_reached.set()
        await asyncio.get_running_loop().create_future()  # blocks until cancelled
        return await self._inner.acquire()  # unreachable: the await above never resolves

    def release_permit(self) -> None:
        self._inner.release_permit()


async def test_cancel_while_queued_for_delete_permit_releases_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #431: cancelling a purge that is still queued for the delete's gate
    permit (no physical worker created yet) releases the registration promptly."""
    target = tmp_path / "movies" / "Queued Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = LocalFileSystem([str(target.parent)])
    gate = _BlockDeleteAcquireGate()
    monkeypatch.setattr(purge_service, "_ABANDONABLE_DELETE_THREAD_GATE", gate)

    purge_task = asyncio.create_task(
        purge_service.purge_library_path(fs, str(target), hold_purge_registration=True)
    )
    assert await asyncio.wait_for(gate.delete_acquire_reached.wait(), timeout=2.0)
    # No delete worker exists; the task is parked on the saturated gate, and the
    # registration is held.
    assert purge_service.begin_placement(str(target)) is False

    purge_task.cancel()
    await assert_task_raises(purge_task, asyncio.CancelledError)

    assert purge_service.active_purge_paths() == ()
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))
    assert target.exists()  # nothing was deleted -- no worker ever ran


async def test_delete_thread_start_failure_releases_permit_and_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #431: if ``Thread.start()`` fails for the delete worker, the
    exception propagates, the gate permit is returned, the registration is
    released even under ``hold_purge_registration``, and the gate stays usable."""
    _install_abandonable_delete_gate(monkeypatch, 1)
    target = tmp_path / "movies" / "Unstartable Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = LocalFileSystem([str(target.parent)])

    class _StartFails:
        def start(self) -> None:
            raise RuntimeError("delete thread failed to start")

    original_thread = threading.Thread

    def _make_thread(*, target: Callable[[], None], name: str, daemon: bool) -> threading.Thread:
        if name == "purge-delete":
            return cast(threading.Thread, _StartFails())
        return original_thread(target=target, name=name, daemon=daemon)

    monkeypatch.setattr(purge_service.threading, "Thread", _make_thread)

    with pytest.raises(RuntimeError, match="delete thread failed to start"):
        await purge_service.purge_library_path(fs, str(target), hold_purge_registration=True)

    assert purge_service.active_purge_paths() == ()
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))

    # Gate still usable: the returned permit lets a second (real) purge complete.
    monkeypatch.setattr(purge_service.threading, "Thread", original_thread)
    result = await purge_service.purge_library_path(fs, str(target))
    assert result.outcome is PurgeOutcome.deleted
    assert not target.exists()


async def test_successful_held_purge_retains_registration_until_end_purge(
    tmp_path: Path,
) -> None:
    """A successful held purge keeps the path claimed after it returns; only the
    caller's ``end_purge`` releases it (the eviction finalize-commit contract)."""
    target = tmp_path / "movies" / "Held Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = LocalFileSystem([str(target.parent)])

    result = await purge_service.purge_library_path(fs, str(target), hold_purge_registration=True)

    assert result.outcome is PurgeOutcome.deleted
    assert not target.exists()
    assert purge_service.active_purge_paths() == (os.path.abspath(os.path.normpath(str(target))),)
    assert purge_service.begin_placement(str(target)) is False
    purge_service.end_purge(str(target))
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))


async def test_failed_held_purge_does_not_retain_registration(tmp_path: Path) -> None:
    """A held purge whose delete FAILS is never held: it returns
    ``PurgeOutcome.error`` and releases the registration without an ``end_purge``,
    so placement is immediately allowed again."""
    target = tmp_path / "movies" / "Failing Held Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _FailingBlockedDeleteFileSystem(target.parent)
    fs.release.set()  # let the delete run (and fail) without blocking

    result = await purge_service.purge_library_path(fs, str(target), hold_purge_registration=True)

    assert result.outcome is PurgeOutcome.error
    assert target.exists()
    assert purge_service.active_purge_paths() == ()
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))


async def test_double_cancellation_still_waits_for_physical_settlement(
    tmp_path: Path,
) -> None:
    """Cancelling twice while the delete is blocked still defers the caller's
    return until the daemon thread physically settles; cancellation wins and the
    registration is released exactly once (its coroutine ``finally`` runs once)."""
    target = tmp_path / "movies" / "Twice Cancelled Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)
    purge_task = asyncio.create_task(
        purge_service.purge_library_path(fs, str(target), hold_purge_registration=True)
    )
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        purge_task.cancel()
        await asyncio.sleep(0)
        assert not purge_task.done()
        purge_task.cancel()  # a second cancel while the worker is still blocked
        await asyncio.sleep(0)
        assert not purge_task.done(), "must still wait for physical settlement"
        assert purge_service.begin_placement(str(target)) is False
        assert target.exists()
    finally:
        fs.release.set()
        await assert_task_raises(purge_task, asyncio.CancelledError)

    assert fs.deleted == [str(target)]
    assert not target.exists()
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))


async def test_purge_refuses_a_path_outside_every_configured_root(tmp_path: Path) -> None:
    # The root-guard rejection: a breadcrumb resolving OUTSIDE the configured roots
    # must be refused, never deleted -- the load-bearing safety guard.
    root = tmp_path / "movies"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "victim.mkv"
    outside.parent.mkdir()
    outside.write_bytes(b"keep me")
    fs = LocalFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(outside))

    assert result.outcome is PurgeOutcome.refused
    assert result.freed_bytes == 0
    assert result.detail is not None
    assert outside.exists()  # never touched


async def test_purge_refuses_an_outside_root_symlink_entry_pointing_inside_the_root(
    tmp_path: Path,
) -> None:
    """Issue #141, purge-level: a symlink ENTRY outside every configured root
    whose target resolves inside one must refuse -- ``delete_guard_refuses``
    (the same predicate ``purge_library_path`` checks up front) must not be
    fooled into treating the dereferenced target's containment as clearance to
    unlink the outside-root entry itself."""
    root = tmp_path / "movies"
    root.mkdir()
    real_target = root / "movie.mkv"
    real_target.write_bytes(b"x" * 100)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_link = outside / "link.mkv"
    outside_link.symlink_to(real_target)
    fs = LocalFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(outside_link))

    assert result.outcome is PurgeOutcome.refused
    assert result.freed_bytes == 0
    assert outside_link.is_symlink()  # untouched
    assert real_target.exists()  # untouched


class _RecordingReclaimFileSystem(LocalFileSystem):
    """A :class:`LocalFileSystem` that records every ``reclaimable_bytes`` call, so a
    test can prove the (potentially huge, recursive) measurement is SKIPPED for an
    out-of-root breadcrumb the containment guard refuses."""

    def __init__(self, library_roots: list[str]) -> None:
        super().__init__(library_roots=library_roots)
        self.reclaim_calls: list[str] = []

    def reclaimable_bytes(self, path: str) -> int:
        self.reclaim_calls.append(path)
        return super().reclaimable_bytes(path)


async def test_purge_refuses_out_of_root_path_without_measuring_it(tmp_path: Path) -> None:
    # An out-of-root breadcrumb pointing at a real (possibly enormous) directory must
    # fail CLOSED and FAST -- containment is checked BEFORE reclaimable_bytes, so the
    # recursive walk never runs on a tree the delete was only going to refuse anyway.
    root = tmp_path / "movies"
    root.mkdir()
    outside_dir = tmp_path / "elsewhere"
    outside_dir.mkdir()
    (outside_dir / "victim.mkv").write_bytes(b"keep me")
    fs = _RecordingReclaimFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(outside_dir))

    assert result.outcome is PurgeOutcome.refused
    assert fs.reclaim_calls == []  # measurement was never entered for the out-of-root path
    assert outside_dir.exists()

    # Sanity: an IN-root path DOES get measured (the guard only short-circuits bad paths).
    target = root / "Some Movie (2020).mkv"
    target.write_bytes(b"x" * 2048)
    ok = await purge_service.purge_library_path(fs, str(target))
    assert ok.outcome is PurgeOutcome.deleted
    assert fs.reclaim_calls == [str(target)]


async def test_purge_deletes_a_path_under_an_anime_root_when_guard_includes_it(
    tmp_path: Path,
) -> None:
    """ADR-0015: an anime title's ``library_path`` lives under its own anime
    root, not ``movies_root``/``tv_root``. The delete-guard must include the
    anime root too, or the purge is silently refused and the bad file stays on
    disk after a "successful" blocklist + re-search (the regression the
    routing feature would otherwise introduce)."""
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()
    target = anime_root / "Some Anime Movie (2020).mkv"
    target.write_bytes(b"x" * 2048)
    fs = LocalFileSystem(library_roots=[str(movies_root), str(anime_root)])

    result = await purge_service.purge_library_path(fs, str(target))

    assert result.outcome is PurgeOutcome.deleted
    assert result.freed_bytes == 2048
    assert not target.exists()


async def test_purge_refuses_an_anime_path_when_the_guard_omits_the_anime_root(
    tmp_path: Path,
) -> None:
    """The regression this ADR-0015 fix prevents, encoded directly: without the
    anime root in the guard's allowlist, an in-anime-root breadcrumb is
    indistinguishable from any other out-of-root path and is refused, not
    deleted."""
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    anime_root = tmp_path / "anime-movies"
    anime_root.mkdir()
    target = anime_root / "Some Anime Movie (2020).mkv"
    target.write_bytes(b"x" * 2048)
    # Anime root deliberately OMITTED from library_roots.
    fs = LocalFileSystem(library_roots=[str(movies_root)])

    result = await purge_service.purge_library_path(fs, str(target))

    assert result.outcome is PurgeOutcome.refused
    assert result.freed_bytes == 0
    assert target.exists()  # never touched -- the bad file would silently remain


async def test_purge_already_gone_in_root_path_is_an_idempotent_deleted(tmp_path: Path) -> None:
    # A breadcrumb pointing at an already-removed (but in-root) path is an honest,
    # idempotent success -- not an error (a retried purge must not fail).
    root = tmp_path / "movies"
    root.mkdir()
    gone = root / "already-removed.mkv"
    fs = LocalFileSystem(library_roots=[str(root)])

    result = await purge_service.purge_library_path(fs, str(gone))

    assert result.outcome is PurgeOutcome.deleted
    assert result.freed_bytes == 0


async def test_remove_torrent_records_delete_with_data() -> None:
    qbt = FakeQbittorrent()
    await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert qbt.removed == [("a" * 40, True)]


class _RaisingRemoveQbt(FakeQbittorrent):
    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        raise RuntimeError("qbt is down")


async def test_remove_torrent_is_best_effort_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Honesty over silence: a genuine client failure is LOGGED (the leak is made
    # visible) but never raised -- a correction must not be undone by a client hiccup.
    qbt: DownloadClientPort = _RaisingRemoveQbt()
    with caplog.at_level(logging.WARNING, logger="plex_manager.services.purge_service"):
        await purge_service.remove_torrent(qbt, "b" * 40, context="a test")
    assert "failed to remove torrent" in caplog.text


async def test_remove_torrent_skips_the_poll_when_content_path_is_unknown() -> None:
    # No content_path reported at all (e.g. a metadata-only torrent) -- nothing
    # distinct to verify, so remove_torrent returns immediately.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(info_hash="a" * 40, name="x", raw_state="downloading", content_path=None)
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True
    assert qbt.removed == [("a" * 40, True)]


async def test_remove_torrent_returns_immediately_when_content_path_already_gone(
    tmp_path: Path,
) -> None:
    # Issue #240: the common case -- the client's own deletion already finished
    # (or the path never existed) -- must not pay the poll's sleep interval.
    content = tmp_path / "already-gone"
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40, name="x", raw_state="downloading", content_path=str(content)
            )
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True


async def test_remove_torrent_polls_until_content_path_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #240: qBittorrent's own file deletion is ASYNCHRONOUS after its ACK
    # -- remove_torrent must poll for the content path to actually leave disk
    # before returning, so a caller's guard release genuinely means "gone".
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.05)
    content = tmp_path / "still-here.mkv"
    content.write_bytes(b"x")
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40, name="x", raw_state="downloading", content_path=str(content)
            )
        ]
    )

    async def _delete_shortly_after() -> None:
        await asyncio.sleep(0.15)
        content.unlink()

    deleter = asyncio.create_task(_delete_shortly_after())
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    await asyncio.gather(deleter)  # reap the helper task cleanly before the test ends
    assert ok is True
    assert not content.exists()


async def test_remove_torrent_logs_and_proceeds_when_the_poll_bound_elapses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A client so slow to finish its own deletion that the bound elapses is
    # LOGGED (honesty over silence), not silently swallowed -- but the
    # best-effort call still reports the removal itself as successful (the
    # client DID ack the delete) rather than blocking forever.
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.02)
    content = tmp_path / "never-goes-away.mkv"
    content.write_bytes(b"x")
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40, name="x", raw_state="downloading", content_path=str(content)
            )
        ]
    )

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.purge_service"):
        ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")

    assert ok is True
    assert "content path still present" in caplog.text
    assert content.exists()  # untouched -- this function never deletes anything itself


class _RaisingStatusQbt(FakeQbittorrent):
    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        raise RuntimeError("qbt is down")


async def test_remove_torrent_tolerates_a_failed_status_snapshot() -> None:
    # A best-effort SNAPSHOT failure must not abort the removal itself -- it just
    # means there is nothing distinct to poll afterwards.
    qbt: DownloadClientPort = _RaisingStatusQbt()
    ok = await purge_service.remove_torrent(qbt, "c" * 40, context="a test")
    assert ok is True


async def test_remove_torrent_remaps_a_host_namespace_content_path_before_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Codex review (PR #281): qBittorrent runs on the HOST, so the reported
    # content_path (e.g. /srv/downloads/...) usually does not exist inside this
    # container -- polling it raw would find it "already gone" and release the
    # guard immediately even while qBittorrent is still deleting the real,
    # container-visible file under the /downloads bind mount. Prove the remap
    # happens: the visible file is still on disk under the remapped mount, so
    # the poll must actually run against IT, not the never-existing host path.
    mount = tmp_path / "downloads"
    mount.mkdir()
    video = mount / "Some.Movie.2020.1080p-GRP.mkv"
    video.write_bytes(b"x" * 1024)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.05)
    host_save_path = "/srv/downloads"
    host_content_path = f"{host_save_path}/{video.name}"
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name=video.name,
                raw_state="stalledUP",
                save_path=host_save_path,
                content_path=host_content_path,
            )
        ],
        files={("a" * 40): [DownloadedFile(name=video.name, size_bytes=1024)]},
    )

    async def _delete_shortly_after() -> None:
        await asyncio.sleep(0.15)
        video.unlink()

    deleter = asyncio.create_task(_delete_shortly_after())
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    await asyncio.gather(deleter)

    assert ok is True
    assert not video.exists()  # the poll genuinely waited on the REMAPPED path


async def test_remove_torrent_skips_the_poll_when_the_host_path_cannot_be_remapped(
    tmp_path: Path,
) -> None:
    # No live download mount matches, and the torrent's own file list can't prove
    # any candidate -- an honest "not visible" skip, never a guess that would
    # release the guard against the wrong (or a stale) path.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name="x",
                raw_state="stalledUP",
                save_path="/srv/downloads",
                content_path="/srv/downloads/Some.Movie.2020.1080p-GRP.mkv",
            )
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True


async def test_remove_torrent_polls_save_path_plus_name_when_content_path_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #290, finding #1: qBittorrent's adapter NULLS content_path when it
    # merely echoes save_path (a not-yet-resolved torrent). save_path + name is
    # then the live content location (exactly as import_service._resolve_content
    # treats it) and IS distinct -- so the post-ack poll must still run against it,
    # not be skipped, leaving the issue #240 same-hash race open for that class.
    mount = tmp_path / "downloads"
    mount.mkdir()
    video = mount / "Some.Movie.2020.1080p-GRP.mkv"
    video.write_bytes(b"x" * 1024)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.05)
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name=video.name,
                raw_state="stalledUP",
                save_path=str(mount),  # host==container here; content_path nulled by the adapter
                content_path=None,
            )
        ],
        files={("a" * 40): [DownloadedFile(name=video.name, size_bytes=1024)]},
    )

    async def _delete_shortly_after() -> None:
        await asyncio.sleep(0.15)
        video.unlink()

    deleter = asyncio.create_task(_delete_shortly_after())
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    await asyncio.gather(deleter)

    assert ok is True
    assert not video.exists()  # the poll genuinely waited on save_path/name


async def test_remove_torrent_skips_the_poll_when_name_is_absolute() -> None:
    # A defensive guard mirrored from _resolve_content: an absolute ``name`` must
    # never be joined onto save_path (it would escape it), so with no content_path
    # there is nothing distinct to poll -- an honest skip.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name="/etc/passwd",
                raw_state="stalledUP",
                save_path="/srv/downloads",
                content_path=None,
            )
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True
    assert qbt.removed == [("a" * 40, True)]


def test_snapshot_content_path_rejects_a_relative_name_that_escapes_save_path() -> None:
    # PR #309 codex finding: a RELATIVE name can still escape save_path via ``..``
    # components (save_path=/srv/downloads + name=../escape.mkv resolves to
    # /srv/escape.mkv). Mirrors _resolve_content's realpath containment guard:
    # nothing safe to poll, so the snapshot is None -- the post-delete poll must
    # never watch an unrelated path and release/delay the same-hash guard on the
    # wrong file.
    status = DownloadStatus(
        info_hash="a" * 40,
        name="../escape.mkv",
        raw_state="stalledUP",
        save_path="/srv/downloads",
        content_path=None,
    )
    assert purge_service._snapshot_content_path(status) is None  # pyright: ignore[reportPrivateUsage]


def test_snapshot_content_path_rejects_a_nested_dotdot_escape_and_a_dot_name() -> None:
    # ``sub/../../escape`` sneaks the same escape past a naive startswith check on
    # the unresolved join; a ``.`` name resolves back to save_path ITSELF, which is
    # shared by sibling torrents and must never be polled.
    for name in ("sub/../../escape.mkv", "."):
        status = DownloadStatus(
            info_hash="a" * 40,
            name=name,
            raw_state="stalledUP",
            save_path="/srv/downloads",
            content_path=None,
        )
        assert (
            purge_service._snapshot_content_path(status)  # pyright: ignore[reportPrivateUsage]
            is None
        )


def test_snapshot_content_path_joins_a_normal_relative_name() -> None:
    # The guard must not disturb the normal fallback: a plain (even nested)
    # relative name still joins onto save_path unchanged.
    status = DownloadStatus(
        info_hash="a" * 40,
        name="Some.Movie.2020.1080p-GRP/Some.Movie.2020.1080p-GRP.mkv",
        raw_state="stalledUP",
        save_path="/srv/downloads",
        content_path=None,
    )
    assert (
        purge_service._snapshot_content_path(status)  # pyright: ignore[reportPrivateUsage]
        == "/srv/downloads/Some.Movie.2020.1080p-GRP/Some.Movie.2020.1080p-GRP.mkv"
    )


async def test_remove_torrent_skips_the_poll_when_name_escapes_save_path() -> None:
    # End-to-end shape of the guard above: with content_path absent and a
    # ``..``-bearing name, there is nothing distinct AND contained to poll -- the
    # removal still succeeds, the poll is honestly skipped.
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name="../escape.mkv",
                raw_state="stalledUP",
                save_path="/srv/downloads",
                content_path=None,
            )
        ]
    )
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    assert ok is True
    assert qbt.removed == [("a" * 40, True)]


async def test_remove_torrent_prefers_the_mounted_file_over_an_outside_mount_phantom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #290, finding #2: the HOST content_path coincidentally exists in this
    # container as a stale PHANTOM tree OUTSIDE the live /downloads mount, while
    # the REAL file sits under the mount. Watching the phantom would release the
    # same-hash guard immediately while qBittorrent still deletes the real file.
    # The poll must run against the REMAPPED, mounted path -- so it genuinely
    # waits for the real file to leave disk.
    mount = tmp_path / "downloads"
    mount.mkdir()
    real = mount / "Some.Movie.2020.1080p-GRP.mkv"
    real.write_bytes(b"x" * 1024)
    # The phantom: a host-shaped tree that ALSO exists in-container, outside the mount.
    host_save_path = str(tmp_path / "srv" / "downloads")
    phantom = tmp_path / "srv" / "downloads" / real.name
    phantom.parent.mkdir(parents=True)
    phantom.write_bytes(b"x" * 1024)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_TIMEOUT_SECONDS", 2.0)
    monkeypatch.setattr(purge_service, "_CONTENT_PATH_GONE_POLL_INTERVAL_SECONDS", 0.05)
    qbt = FakeQbittorrent(
        statuses=[
            DownloadStatus(
                info_hash="a" * 40,
                name=real.name,
                raw_state="stalledUP",
                save_path=host_save_path,
                content_path=f"{host_save_path}/{real.name}",
            )
        ],
        files={("a" * 40): [DownloadedFile(name=real.name, size_bytes=1024)]},
    )

    async def _delete_the_real_file() -> None:
        await asyncio.sleep(0.15)
        real.unlink()  # only the mounted file is deleted; the phantom lingers

    deleter = asyncio.create_task(_delete_the_real_file())
    ok = await purge_service.remove_torrent(qbt, "a" * 40, context="a test")
    await asyncio.gather(deleter)

    assert ok is True
    assert not real.exists()  # the poll waited on the MOUNTED path, not the phantom
    assert phantom.exists()  # never touched -- proves the phantom was not what was watched


async def test_visible_content_path_prefers_mounted_remap_over_a_phantom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direct unit of finding #2: _visible_content_path must not short-circuit to a
    # verbatim content_path that exists only as a phantom OUTSIDE the live mount.
    mount = tmp_path / "downloads"
    mount.mkdir()
    real = mount / "clip.mkv"
    real.write_bytes(b"x" * 512)
    host_save_path = str(tmp_path / "srv" / "downloads")
    phantom = tmp_path / "srv" / "downloads" / "clip.mkv"
    phantom.parent.mkdir(parents=True)
    phantom.write_bytes(b"x" * 512)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    qbt = FakeQbittorrent(files={("a" * 40): [DownloadedFile(name="clip.mkv", size_bytes=512)]})
    result = await purge_service._visible_content_path(  # pyright: ignore[reportPrivateUsage]
        qbt, "a" * 40, f"{host_save_path}/clip.mkv", host_save_path
    )
    assert result == str(real)


async def test_visible_content_path_returns_none_when_save_path_is_empty() -> None:
    # No live save-path anchor (a torrent status with no save path reported) --
    # only the verbatim content path counts; a free suffix search is never
    # attempted (mirrors import_service._resolve_visible_content).
    qbt = FakeQbittorrent()
    result = await purge_service._visible_content_path(  # pyright: ignore[reportPrivateUsage]
        qbt, "a" * 40, "/srv/downloads/gone.mkv", ""
    )
    assert result is None


class _RaisingListFilesQbt(FakeQbittorrent):
    async def list_files(self, info_hash: str) -> list[DownloadedFile]:
        raise RuntimeError("qbt is down")


async def test_visible_content_path_tolerates_a_failed_file_list_fetch() -> None:
    # A best-effort remap: a client hiccup fetching the file list must not raise
    # -- it just means there's nothing safely provable to poll.
    qbt: DownloadClientPort = _RaisingListFilesQbt()
    result = await purge_service._visible_content_path(  # pyright: ignore[reportPrivateUsage]
        qbt, "a" * 40, "/srv/downloads/gone.mkv", "/srv/downloads"
    )
    assert result is None


class _MissingRemoveQbt(DownloadClientPort):
    """A ``DownloadClientPort`` implementation that overrides every method
    EXCEPT ``remove`` -- proving issue #204's fix: the Protocol's own default
    body for ``remove`` now raises ``NotImplementedError`` (never a silent
    implicit ``return None``) when a subclass forgets to override it."""

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> AddResult:
        return AddResult(torrent_hash="hash", created=True)

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        return None

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        return []

    async def get_statuses_for_hashes(self, hashes: Sequence[str]) -> list[DownloadStatus]:
        return []

    async def pause(self, info_hash: str) -> None:
        return None

    async def resume(self, info_hash: str) -> None:
        return None

    # ``remove`` deliberately NOT overridden -- the Protocol's own default runs.

    async def set_category(self, info_hash: str, category: str) -> None:
        return None

    async def get_save_path(self, info_hash: str) -> str | None:
        return None

    async def list_files(self, info_hash: str) -> list[DownloadedFile]:
        return []

    async def get_default_save_path(self) -> str | None:
        return None

    async def set_location(self, info_hash: str, save_path: str) -> None:
        return None

    async def get_failure_detail(self, info_hash: str) -> FailureDetail | None:
        return None


async def test_missing_remove_implementation_can_never_report_purge_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #204's load-bearing regression: before the fix, a
    ``DownloadClientPort`` implementation that forgot to override ``remove``
    fell through to the Protocol's docstring-only default -- Python's implicit
    ``return None`` -- so ``qbt.remove(...)`` would appear to SUCCEED for a
    torrent that was never actually removed, and ``purge_service.remove_torrent``
    would report ``True``: a blocklisted/cancelled torrent silently kept
    seeding forever with no visible failure anywhere.

    Now the default raises ``NotImplementedError``, which ``remove_torrent``'s
    best-effort ``except Exception`` catches, logs, and reports as ``False`` --
    the caller (``queue_service``) then correctly keeps the durable "removal
    still owed" marker instead of persisting a false "removed" outcome.
    """
    qbt: DownloadClientPort = _MissingRemoveQbt()  # pyright: ignore[reportAbstractUsage]
    with caplog.at_level(logging.WARNING, logger="plex_manager.services.purge_service"):
        result = await purge_service.remove_torrent(qbt, "c" * 40, context="a test")
    assert result is False
    assert "failed to remove torrent" in caplog.text


async def test_trigger_library_scan_records_the_scan() -> None:
    library = FakeLibrary()
    await purge_service.trigger_library_scan(
        library, library_path="/lib/movies/x", media_type="movie", context="report-issue"
    )
    assert library.scan_calls == [("/lib/movies/x", "movie")]
