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

    def delete(self, path: str) -> None:
        self.started.set()
        self.release.wait(timeout=5)
        super().delete(path)


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


async def test_a_hung_shielded_delete_times_out_and_names_the_stuck_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The scenario issue #401 exists for: an eviction delete stuck on a dead
    mount at shutdown time must not hang the wait forever. The bound trips,
    the still-active purge path is named in the warning (honesty over
    silence), and the coroutine returns so shutdown can proceed -- the stuck
    worker thread is abandoned, not killed (only the OS reclaims it on
    process exit; #395's shielding semantics for the task itself are
    unchanged -- it is still running, uncancelled, when this returns)."""
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
        # The bound is on THIS wait only -- the shielded task itself keeps
        # running, exactly as #395 requires (never killed out from under a
        # still-mutating delete).
        assert not task.done()
        assert purge_service.active_purge_paths() == (
            os.path.abspath(os.path.normpath(str(target))),
        )
    finally:
        # Let the stuck delete genuinely finish so the worker thread and the
        # module-level purge registry don't leak into a later test. Once it
        # settles, ``_delete_to_settlement`` honours the earlier cancellation
        # (PR #395's "cancellation wins once settled" contract) and raises
        # ``CancelledError`` out of the task -- expected, not a failure; see
        # ``tests.services.test_purge_service``'s identical cleanup pattern.
        fs.release.set()
        # Once settled, ``_delete_to_settlement`` honours the earlier
        # cancellation, so the task's outcome IS a CancelledError -- assert
        # that contract (which also gives this await an effect CodeQL can
        # see) rather than merely suppressing it.
        (outcome,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(outcome, asyncio.CancelledError)
    assert purge_service.active_purge_paths() == ()
