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
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.domain.disk_usage import DiskUsage
from plex_manager.ports.library import LibraryPort
from plex_manager.services import eviction_service, purge_service, retention_telemetry_service
from plex_manager.services.health_service import TtlCache
from plex_manager.web import app as app_module
from plex_manager.web.deps import SettingsStore
from plex_manager.web.routers import ops as ops_router
from plex_manager.web.schemas import DiskRootItem
from tests.web.fakes import FakeLibrary

SessionMaker = async_sessionmaker[AsyncSession]


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


class _BlockedDiskUsageProbe:
    """A synchronous ``read_disk_usage`` replacement that models a dead mount."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.thread_name: str | None = None
        self.thread_daemon: bool | None = None

    def __call__(self, path: str) -> DiskUsage:
        worker = threading.current_thread()
        self.thread_name = worker.name
        self.thread_daemon = worker.daemon
        self.started.set()
        self.release.wait(timeout=5)
        try:
            return DiskUsage(root=path, total_bytes=1000, available_bytes=900)
        finally:
            self.finished.set()


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


async def _wait_for_registry_release(path: str, *, timeout: float = 2.0) -> None:
    """Yield to the loop until ``path``'s deferred purge registration is released.

    After shutdown abandonment (#431) the ``_ACTIVE_PURGE_PATHS`` release is
    deferred to the raw delete worker's done-callback, which the loop runs only
    once the daemon thread physically settles -- so a test must poll rather than
    read the registry synchronously right after releasing the worker."""
    normalized = os.path.abspath(os.path.normpath(path))
    deadline = time.monotonic() + timeout
    while normalized in purge_service.active_purge_paths():
        if time.monotonic() >= deadline:
            raise AssertionError(f"purge registration for {normalized!r} was never released")
        await asyncio.sleep(0.01)


async def _configured_eviction_app(sessionmaker_: SessionMaker, root: Path) -> FastAPI:
    """Build the smallest production-shaped app state for one leased eviction tick."""
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("movies_root", str(root))
        await store.set("eviction_enabled", "true")
        await store.set("disk_pressure_threshold_percent", "95")
        await store.set("disk_pressure_target_percent", "90")
        await store.set("eviction_grace_days", "30")
        await store.set("eviction_interval_minutes", "5")
        await session.commit()

    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200))
    )
    return app


async def _abandon_cancelled_task_at_shutdown(task: asyncio.Task[object]) -> None:
    """Drive the production shutdown escape hatch for one cancelled settlement."""

    async def _quick_background_task() -> None:
        await asyncio.sleep(1000)

    background_task = asyncio.create_task(_quick_background_task())
    task.cancel()
    background_task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), "probe cancellation must wait for shutdown abandonment"
    await app_module._await_background_tasks_shutdown(  # pyright: ignore[reportPrivateUsage]
        (background_task,), timeout_seconds=5.0
    )


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
        assert "filesystem settlement abandoned" in caplog.text
        assert task.done()
        assert task.cancelled()
        assert not fs.finished.is_set()
        # #431: the abandoned delete is still physically running, so its
        # ``_ACTIVE_PURGE_PATHS`` registration is HELD -- deferred to the raw
        # worker's physical completion -- not cleared the instant the settlement
        # resolved.
        normalized = os.path.abspath(os.path.normpath(str(target)))
        assert purge_service.active_purge_paths() == (normalized,)
    finally:
        # Release the daemon worker; its physical completion delivers the
        # deferred registration release on the loop.
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)
    await _wait_for_registry_release(str(target))
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
        # #431: the abandoned request-scoped delete is still physically running,
        # so its registration is HELD until the daemon thread settles.
        normalized = os.path.abspath(os.path.normpath(str(target)))
        assert purge_service.active_purge_paths() == (normalized,)
    finally:
        timeout_trigger.cancel()
        await asyncio.gather(timeout_trigger, return_exceptions=True)
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)
    await _wait_for_registry_release(str(target))
    assert purge_service.active_purge_paths() == ()


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


async def test_hung_disk_pressure_probe_is_abandoned_at_shutdown(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #418: the tick's telemetry pressure probe cannot delay process exit."""
    app = await _configured_eviction_app(sessionmaker_, tmp_path)
    blocker = _BlockedDiskUsageProbe()

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return FakeLibrary()

    monkeypatch.setattr(app_module, "get_library_optional", _library)
    monkeypatch.setattr(app_module, "read_disk_usage", blocker)
    task = asyncio.create_task(app_module._eviction_tick_leased(app))  # pyright: ignore[reportPrivateUsage]
    try:
        assert await asyncio.to_thread(blocker.started.wait, 2.0)
        await _abandon_cancelled_task_at_shutdown(task)
        assert task.done()
        assert task.cancelled()
        assert not blocker.finished.is_set()
    finally:
        blocker.release.set()
        assert await asyncio.to_thread(blocker.finished.wait, 2.0)
        await app.state.http_client.aclose()


@pytest.mark.parametrize("probe", ["preview", "sweep"])
async def test_hung_eviction_service_disk_probe_is_abandoned_at_shutdown(
    probe: str,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #418: both eviction-service disk reads share settlement tracking."""
    blocker = _BlockedDiskUsageProbe()
    monkeypatch.setattr(eviction_service, "read_disk_usage", blocker)
    library = FakeLibrary()
    fs = LocalFileSystem([str(tmp_path)])

    async with sessionmaker_() as session:
        if probe == "preview":
            task = asyncio.create_task(
                eviction_service.preview_candidates(
                    session=session,
                    library=library,
                    media_type="movie",
                    root_path=str(tmp_path),
                    grace_days=30,
                )
            )
        else:
            task = asyncio.create_task(
                eviction_service.run_eviction_sweep(
                    session=session,
                    library=library,
                    fs=fs,
                    media_type="movie",
                    root_path=str(tmp_path),
                    threshold_pct=95.0,
                    target_pct=90.0,
                    grace_days=30,
                )
            )
        try:
            assert await asyncio.to_thread(blocker.started.wait, 2.0)
            await _abandon_cancelled_task_at_shutdown(task)
            assert task.done()
            assert task.cancelled()
            assert not blocker.finished.is_set()
        finally:
            blocker.release.set()
            assert await asyncio.to_thread(blocker.finished.wait, 2.0)


async def test_disk_pressure_probe_oserror_still_suppresses_telemetry(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick still treats an unreadable root as pressure firing."""
    app = await _configured_eviction_app(sessionmaker_, tmp_path)
    telemetry_calls = 0
    sweep_calls = 0

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return FakeLibrary()

    def _unreadable(_path: str) -> DiskUsage:
        raise OSError("dead mount")

    async def _telemetry(**_kwargs: object) -> None:
        nonlocal telemetry_calls
        telemetry_calls += 1

    async def _sweep(**_kwargs: object) -> list[object]:
        nonlocal sweep_calls
        sweep_calls += 1
        return []

    monkeypatch.setattr(app_module, "get_library_optional", _library)
    monkeypatch.setattr(app_module, "read_disk_usage", _unreadable)
    monkeypatch.setattr(
        app_module.retention_telemetry_service, "run_retention_telemetry_sweep", _telemetry
    )
    monkeypatch.setattr(eviction_service, "run_eviction_sweep", _sweep)
    try:
        await app_module._eviction_tick_leased(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    assert telemetry_calls == 0
    assert sweep_calls == 1


@pytest.mark.parametrize("probe", ["preview", "sweep"])
async def test_eviction_service_disk_probe_oserror_still_skips_root(
    probe: str,
    sessionmaker_: SessionMaker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving either service probe preserves its existing ``OSError`` fallback."""

    def _unreadable(_path: str) -> DiskUsage:
        raise OSError("dead mount")

    monkeypatch.setattr(eviction_service, "read_disk_usage", _unreadable)
    async with sessionmaker_() as session:
        if probe == "preview":
            result = await eviction_service.preview_candidates(
                session=session,
                library=FakeLibrary(),
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=30,
            )
        else:
            result = await eviction_service.run_eviction_sweep(
                session=session,
                library=FakeLibrary(),
                fs=LocalFileSystem([str(tmp_path)]),
                media_type="movie",
                root_path=str(tmp_path),
                threshold_pct=95.0,
                target_pct=90.0,
                grace_days=30,
            )

    assert result == []


async def test_hung_retention_telemetry_disk_probe_is_abandoned_at_shutdown(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2: the pressure-pass branch of the eviction tick falls straight
    into ``run_retention_telemetry_sweep``'s own disk-usage probe -- it must
    share the same shutdown-abandonable settlement tracking as the other three
    call sites, not a bare ``asyncio.to_thread``."""
    blocker = _BlockedDiskUsageProbe()
    monkeypatch.setattr(retention_telemetry_service, "read_disk_usage", blocker)
    library = FakeLibrary()
    fs = LocalFileSystem([str(tmp_path)])

    async with sessionmaker_() as session:
        task = asyncio.create_task(
            retention_telemetry_service.run_retention_telemetry_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type="movie",
                root_path=str(tmp_path),
                grace_days=30,
                threshold_pct=95.0,
                target_pct=90.0,
            )
        )
        try:
            assert await asyncio.to_thread(blocker.started.wait, 2.0)
            assert blocker.thread_name == "filesystem-probe"
            assert blocker.thread_daemon is True
            await _abandon_cancelled_task_at_shutdown(task)
            assert task.done()
            assert task.cancelled()
            assert not blocker.finished.is_set()
        finally:
            blocker.release.set()
            assert await asyncio.to_thread(blocker.finished.wait, 2.0)


async def test_retention_telemetry_disk_probe_oserror_still_skips_root(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the telemetry sweep's probe preserves its existing ``OSError``
    fallback: an unreadable root skips the whole sweep for that root (logged),
    never raises."""

    def _unreadable(_path: str) -> DiskUsage:
        raise OSError("dead mount")

    monkeypatch.setattr(retention_telemetry_service, "read_disk_usage", _unreadable)
    async with sessionmaker_() as session:
        # None of these should raise -- an unreadable root is a silent (but
        # logged) skip, exactly as before the probe moved substrates.
        await retention_telemetry_service.run_retention_telemetry_sweep(
            session=session,
            library=FakeLibrary(),
            fs=LocalFileSystem([str(tmp_path)]),
            media_type="movie",
            root_path=str(tmp_path),
            grace_days=30,
            threshold_pct=95.0,
            target_pct=90.0,
        )


async def test_hung_ops_disk_probe_is_abandoned_at_shutdown(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2: ``GET /api/v1/ops/disk`` reads usage BEFORE it ever reaches
    the now-protected ``preview_candidates`` -- that first read must share the
    same shutdown-abandonable settlement tracking, not a bare
    ``asyncio.to_thread``."""
    blocker = _BlockedDiskUsageProbe()
    monkeypatch.setattr(ops_router, "read_disk_usage", blocker)
    cache: TtlCache[DiskRootItem] = TtlCache()

    async with sessionmaker_() as session:
        task = asyncio.create_task(
            ops_router._disk_root_item(  # pyright: ignore[reportPrivateUsage]
                session=session,
                library=FakeLibrary(),
                label="movies",
                media_type="movie",
                root_path=str(tmp_path),
                all_roots=(str(tmp_path),),
                grace_days=30,
                cache=cache,
            )
        )
        try:
            assert await asyncio.to_thread(blocker.started.wait, 2.0)
            assert blocker.thread_name == "filesystem-probe"
            assert blocker.thread_daemon is True
            await _abandon_cancelled_task_at_shutdown(task)
            assert task.done()
            assert task.cancelled()
            assert not blocker.finished.is_set()
        finally:
            blocker.release.set()
            assert await asyncio.to_thread(blocker.finished.wait, 2.0)


async def test_ops_disk_probe_oserror_still_reports_root_error(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving the ops-dashboard probe preserves its existing ``OSError``
    fallback: an unreadable root reports a zeroed, ``error``-set item instead
    of raising or 500ing the whole ``/ops/disk`` response."""

    def _unreadable(_path: str) -> DiskUsage:
        raise OSError("dead mount")

    monkeypatch.setattr(ops_router, "read_disk_usage", _unreadable)
    cache: TtlCache[DiskRootItem] = TtlCache()

    async with sessionmaker_() as session:
        result = await ops_router._disk_root_item(  # pyright: ignore[reportPrivateUsage]
            session=session,
            library=FakeLibrary(),
            label="movies",
            media_type="movie",
            root_path=str(tmp_path),
            all_roots=(str(tmp_path),),
            grace_days=30,
            cache=cache,
        )

    assert result.error is not None
    assert result.candidates == []


async def test_begin_placement_refuses_while_an_abandoned_delete_still_runs(
    tmp_path: Path,
) -> None:
    """Issue #431: the abandonment-to-exit ``begin_placement`` race is CLOSED.

    Shutdown abandonment (PR #406's ``abandon_active_settlements``) resolves the
    purge's shielded settlement and lets the cancelled task finish, but the
    ``_ACTIVE_PURGE_PATHS`` release is now tied to the delete worker's PHYSICAL
    completion, not the settlement -- ``purge_library_path`` hands the
    registration to ``_delete_to_settlement``, which defers the unregister to the
    raw worker's done-callback when abandonment resolves the wait early. This
    drives exactly that abandonment for a real, still-running delete and asserts
    a live caller reaching ``begin_placement`` in the abandonment-to-exit window
    is REFUSED the path while the abandoned ``rmtree`` is still tearing it down --
    the interleaving the ``_ACTIVE_PURGE_PATHS``/``_ACTIVE_PLACEMENT_PATHS``
    ordering rule (PR #117 round 9) exists to prevent -- and only succeeds once
    the daemon thread genuinely finishes.
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

        # The task has finished (cancelled) but the daemon thread is PROVABLY
        # still mid-rmtree -- the exact abandonment-to-exit window.
        assert task.done()
        assert task.cancelled()
        assert not fs.finished.is_set()

        # #431: the registration is HELD until physical completion, so a live
        # caller is REFUSED the path an abandoned delete is still tearing down.
        normalized = os.path.abspath(os.path.normpath(str(target)))
        assert purge_service.active_purge_paths() == (normalized,)
        assert purge_service.begin_placement(str(target)) is False
    finally:
        fs.release.set()
        assert await asyncio.to_thread(fs.finished.wait, 2.0)

    # Once the daemon thread physically finished, the deferred loop-delivered
    # callback releases the registration and placement is allowed again.
    await _wait_for_registry_release(str(target))
    assert purge_service.begin_placement(str(target)) is True
    purge_service.end_placement(str(target))


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
    await purge_service._run_on_abandonable_thread(  # pyright: ignore[reportPrivateUsage]
        lambda: fs.delete(str(target)), thread_name="purge-delete"
    )
    assert fs.started.wait(timeout=2.0)
    fs.release.set()
    assert fs.finished.wait(timeout=2.0)
    assert delivered.wait(timeout=2.0)
