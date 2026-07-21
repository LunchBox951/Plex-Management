"""``web.app._await_background_tasks_shutdown`` — the bounded shutdown wait
(issue #401) around the six background tasks ``lifespan`` cancels on exit.

A purge/eviction delete's off-thread ``fs.delete`` is cancellation-shielded
until it genuinely settles (PR #395 / issue #128's
``purge_service._delete_to_settlement``): a Python thread performing a
blocking syscall cannot be interrupted from outside, so a hung mount can keep
a cancelled background task from ever finishing its unwind. These tests cover
both sides of the bound this issue adds: the normal (fast) case behaves
exactly like the old unbounded wait, and a genuinely stuck delete times out,
logs a clear warning naming the still-active path, and lets the coroutine
return so shutdown can proceed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

import pytest

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.services import purge_service
from plex_manager.web import app as app_module


class _BlockedProbeFileSystem(LocalFileSystem):
    """Blocks before delete, in the root-guard probe phase."""

    def __init__(self, root: Path) -> None:
        super().__init__([str(root)])
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def delete_guard_refuses(self, path: str) -> bool:
        self.started.set()
        self.release.wait(timeout=5)
        try:
            return super().delete_guard_refuses(path)
        finally:
            self.finished.set()


class _BlockedReclaimProbeFileSystem(LocalFileSystem):
    """Allows the guard, then blocks in reclaimable-byte accounting."""

    def __init__(self, root: Path) -> None:
        super().__init__([str(root)])
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def reclaimable_bytes(self, path: str) -> int:
        self.started.set()
        self.release.wait(timeout=5)
        try:
            return super().reclaimable_bytes(path)
        finally:
            self.finished.set()


class _FailingProbeFileSystem(LocalFileSystem):
    def delete_guard_refuses(self, path: str) -> bool:
        del path
        raise OSError("probe failed")


class _BlockedDeleteFileSystem(LocalFileSystem):
    """A real, root-guarded ``LocalFileSystem`` whose ``delete`` blocks (in its
    worker thread) until the test releases it -- lets a test cancel the
    awaiting coroutine while the underlying delete thread is still running,
    the exact window #395's shielding (and this bound around it) both target.
    Mirrors ``tests.services.test_purge_service._BlockedDeleteFileSystem``,
    kept local so this suite has no cross-test-module dependency."""

    def __init__(self, root: Path) -> None:
        super().__init__([str(root)])
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def delete(self, path: str) -> None:
        self.started.set()
        self.release.wait(timeout=5)
        try:
            super().delete(path)
        finally:
            self.finished.set()


async def _purge_worker(fs: LocalFileSystem, path: str) -> None:
    """A background task shaped like a real ``lifespan`` member (returns
    ``None``): its body calls the shielded purge primitive."""
    await purge_service.purge_library_path(fs, path)


async def test_fast_settling_tasks_are_unaffected_by_the_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task that finishes its cancelled unwind quickly behaves exactly like
    the old unbounded ``gather``: it settles well within the timeout and the
    bound never trips (no warning logged)."""

    async def _quick() -> None:
        await asyncio.sleep(1000)

    task = asyncio.create_task(_quick())
    await asyncio.sleep(0)  # let it start before cancelling
    task.cancel()

    with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
        await app_module._await_background_tasks_shutdown(  # pyright: ignore[reportPrivateUsage]
            (task,), timeout_seconds=5.0
        )

    assert task.done()
    assert caplog.text == ""


async def test_delete_runs_on_a_daemon_thread_the_interpreter_cannot_rejoin(
    tmp_path: Path,
) -> None:
    """Codex #406 P1: the bounded wait only truly bounds shutdown if the hung
    delete thread can never be re-joined later. ``asyncio.to_thread``'s
    default-executor workers are non-daemon and ARE joined at interpreter
    shutdown (``asyncio.run``'s ``shutdown_default_executor`` plus
    ``concurrent.futures``' atexit hook) -- on that substrate the process
    would still block on a wedged mount after the bound "proceeded". Pin the
    substrate: the in-flight delete runs on a dedicated named DAEMON thread,
    abandonable by construction."""
    target = tmp_path / "movies" / "Stuck Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)

    task = asyncio.create_task(_purge_worker(fs, str(target)))
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        delete_threads = [t for t in threading.enumerate() if t.name == "purge-delete"]
        assert delete_threads, "the purge delete must run on its own dedicated thread"
        assert all(thread.daemon for thread in delete_threads)
    finally:
        fs.release.set()
        assert await task is None  # no cancellation here: the purge completes
    assert purge_service.active_purge_paths() == ()


async def test_a_hung_shielded_delete_timeout_finishes_the_settlement_task(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The scenario issue #401 exists for: an eviction delete stuck on a dead
    mount at shutdown time must not hang the wait forever. The bound trips,
    names the still-active purge path, signals the settlement loop to abandon
    its daemon worker, and does not return until the cancelled task has really
    finished. The filesystem thread may still be blocked, but no pending task
    remains for ``asyncio.run`` teardown to gather forever."""
    target = tmp_path / "movies" / "Stuck Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)

    task = asyncio.create_task(_purge_worker(fs, str(target)))
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        task.cancel()
        # Give the cancellation a chance to be delivered; the shielded wait
        # must NOT have let the task finish yet -- the worker thread is still
        # blocked on ``fs.release``.
        await asyncio.sleep(0)
        assert not task.done()

        with caplog.at_level(logging.WARNING, logger="plex_manager.web.app"):
            await app_module._await_background_tasks_shutdown(  # pyright: ignore[reportPrivateUsage]
                (task,), timeout_seconds=0.05
            )

        assert "shutdown timed out" in caplog.text
        assert os.path.abspath(os.path.normpath(str(target))) in caplog.text
        assert "purge settlement abandoned" in caplog.text
        assert task.done()
        assert task.cancelled()
        assert not fs.finished.is_set()
        assert purge_service.active_purge_paths() == ()
    finally:
        # The settlement coroutine and registry are already finished; release
        # only the test's daemon worker so it does not linger across tests.
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)
    assert purge_service.active_purge_paths() == ()


async def test_timeout_unblocks_request_scoped_settlement(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """P1-b: a protocol/request task is not one of lifespan's six tasks, but
    it uses the same settlement loop and therefore must observe the process-
    wide abandon signal raised when the background-task bound expires."""
    target = tmp_path / "movies" / "Reported Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)

    request_task = asyncio.create_task(_purge_worker(fs, str(target)))
    timeout_trigger = asyncio.create_task(asyncio.sleep(1000))
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        request_task.cancel()
        await asyncio.sleep(0)
        assert not request_task.done()

        with caplog.at_level(logging.WARNING):
            await app_module._await_background_tasks_shutdown(  # pyright: ignore[reportPrivateUsage]
                (timeout_trigger,), timeout_seconds=0.05
            )

        (outcome,) = await asyncio.wait_for(
            asyncio.gather(request_task, return_exceptions=True), timeout=0.5
        )
        assert isinstance(outcome, asyncio.CancelledError)
        assert not fs.finished.is_set()
        assert purge_service.active_purge_paths() == ()
    finally:
        timeout_trigger.cancel()
        await asyncio.gather(timeout_trigger, return_exceptions=True)
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)


async def test_request_settlement_is_abandoned_when_background_tasks_finish_fast(
    tmp_path: Path,
) -> None:
    """P1-c: request purge abandonment is independent of a background timeout."""
    target = tmp_path / "movies" / "Reported Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)
    request_task = asyncio.create_task(_purge_worker(fs, str(target)))

    async def _quick() -> None:
        await asyncio.sleep(1000)

    background_task = asyncio.create_task(_quick())
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        request_task.cancel()
        background_task.cancel()
        await asyncio.sleep(0)
        assert not request_task.done()

        await app_module._await_background_tasks_shutdown(  # pyright: ignore[reportPrivateUsage]
            (background_task,), timeout_seconds=5.0
        )

        assert request_task.done()
        assert request_task.cancelled()
        assert not fs.finished.is_set()
    finally:
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)


async def test_hung_pre_delete_probe_is_abandoned_at_shutdown(tmp_path: Path) -> None:
    """P1-d: guard/size probes use abandonable workers and settlement tracking."""
    target = tmp_path / "movies" / "Probe Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedProbeFileSystem(target.parent)
    purge_task = asyncio.create_task(_purge_worker(fs, str(target)))

    async def _quick() -> None:
        await asyncio.sleep(1000)

    background_task = asyncio.create_task(_quick())
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        purge_task.cancel()
        background_task.cancel()
        await asyncio.sleep(0)
        assert not purge_task.done(), "probe cancellation must wait for shutdown abandonment"

        await app_module._await_background_tasks_shutdown(  # pyright: ignore[reportPrivateUsage]
            (background_task,), timeout_seconds=5.0
        )

        assert purge_task.done()
        assert purge_task.cancelled()
        assert not fs.finished.is_set()
        assert purge_service.active_purge_paths() == ()
    finally:
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)


async def test_hung_reclaimable_bytes_probe_is_abandoned_at_shutdown(tmp_path: Path) -> None:
    """The second pre-delete probe uses the same abandonable settlement path."""
    target = tmp_path / "movies" / "Size Probe Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedReclaimProbeFileSystem(target.parent)
    purge_task = asyncio.create_task(_purge_worker(fs, str(target)))

    async def _quick() -> None:
        await asyncio.sleep(1000)

    background_task = asyncio.create_task(_quick())
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        purge_task.cancel()
        background_task.cancel()
        await asyncio.sleep(0)
        assert not purge_task.done()

        await app_module._await_background_tasks_shutdown(  # pyright: ignore[reportPrivateUsage]
            (background_task,), timeout_seconds=5.0
        )

        assert purge_task.done()
        assert purge_task.cancelled()
        assert not fs.finished.is_set()
        assert purge_service.active_purge_paths() == ()
    finally:
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)


async def test_live_pre_delete_probe_error_keeps_existing_classification(tmp_path: Path) -> None:
    """P1-d: moving probes to daemon workers must preserve live-loop errors."""
    target = tmp_path / "movies" / "Probe Error Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _FailingProbeFileSystem([str(target.parent)])

    with pytest.raises(OSError, match="probe failed"):
        await purge_service.purge_library_path(fs, str(target))
    assert purge_service.active_purge_paths() == ()


async def test_begin_placement_can_claim_a_path_whose_abandoned_delete_still_runs(
    tmp_path: Path,
) -> None:
    """Issue #421: characterize the abandonment-to-exit window's confidence gap.

    Shutdown abandonment (PR #406's ``abandon_active_settlements``) resolves the
    purge's shielded settlement -- and this task's ``finally`` releases
    ``_ACTIVE_PURGE_PATHS`` -- WITHOUT waiting for the daemon ``shutil.rmtree``
    thread to actually finish (``fs.finished`` provably still unset below). This
    drives exactly that abandonment for a real, still-running delete and then
    calls ``begin_placement`` on the identical path in the narrow window before
    process exit, the live-caller race issue #421 asks a targeted test to settle.

    Result: ``begin_placement`` returns ``True``. ``_conflicts_with`` only ever
    consults the current (already-emptied) ``_ACTIVE_PURGE_PATHS`` registry, so
    a placement can be registered for a directory an abandoned ``rmtree`` is
    concurrently, physically tearing down -- the exact interleaving the
    ``_ACTIVE_PURGE_PATHS``/``_ACTIVE_PLACEMENT_PATHS`` ordering rule (PR #117
    round 9) exists to prevent. This is NOT restructured here (out of scope for
    this change): it is recorded as a known gap, real only in the shutdown-to-
    exit window, for the coordinator to weigh against issue #128's crash-
    recovery backstop reconciling any resulting partial disk state on next
    startup.
    """
    target = tmp_path / "movies" / "Stuck Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)

    task = asyncio.create_task(_purge_worker(fs, str(target)))
    try:
        assert await asyncio.to_thread(fs.started.wait, 2.0)
        task.cancel()
        # As in test_a_hung_shielded_delete_timeout_finishes_the_settlement_task:
        # confirm the shielded wait has not let the task finish on its own before
        # the shutdown bound forces abandonment below.
        await asyncio.sleep(0)
        assert not task.done()

        await app_module._await_background_tasks_shutdown(  # pyright: ignore[reportPrivateUsage]
            (task,), timeout_seconds=0.05
        )

        # The registration is already released and the daemon thread is
        # PROVABLY still mid-rmtree -- the exact abandonment-to-exit window
        # issue #421 is concerned with.
        assert task.done()
        assert task.cancelled()
        assert purge_service.active_purge_paths() == ()
        assert not fs.finished.is_set()

        # CHARACTERIZATION: a live caller reaching begin_placement() in this
        # window observes no conflict and is allowed to claim the path, even
        # though the abandoned delete is still physically running against it.
        claimed = purge_service.begin_placement(str(target))
        assert claimed is True, (
            "known gap (issue #421): begin_placement currently succeeds for a "
            "path an abandoned purge delete is still physically tearing down"
        )
    finally:
        purge_service.end_placement(str(target))
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)


async def test_late_delete_completion_after_abandon_does_not_touch_closed_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon worker may finish after settlement abandonment and loop close.
    Its narrow late-delivery guard must absorb only that expected closed-loop
    RuntimeError rather than surfacing an unhandled thread exception."""
    target = tmp_path / "movies" / "Late Movie.mkv"
    target.parent.mkdir()
    target.write_bytes(b"x")
    fs = _BlockedDeleteFileSystem(target.parent)
    loop = asyncio.get_running_loop()
    delivered = threading.Event()

    def _closed_loop_delivery(*_args: object) -> None:
        delivered.set()
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(loop, "call_soon_threadsafe", _closed_loop_delivery)
    monkeypatch.setattr(loop, "is_closed", lambda: True)
    await purge_service._run_delete_on_abandonable_thread(  # pyright: ignore[reportPrivateUsage]
        fs, str(target)
    )
    assert fs.started.wait(timeout=2.0)
    fs.release.set()
    assert fs.finished.wait(timeout=2.0)
    assert delivered.wait(timeout=2.0)
