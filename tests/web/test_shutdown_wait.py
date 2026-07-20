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
    purge_service._run_delete_on_abandonable_thread(  # pyright: ignore[reportPrivateUsage]
        fs, str(target)
    )
    assert fs.started.wait(timeout=2.0)
    fs.release.set()
    assert fs.finished.wait(timeout=2.0)
    assert delivered.wait(timeout=2.0)
