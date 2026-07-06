"""``_eviction_tick`` — the operability beta's periodic sweep wiring
(ADR-0012): settings resolution, the master ``eviction_enabled`` kill switch,
and the root-scoped filesystem it hands to ``eviction_service``.

Mirrors ``test_reconcile_loop.py``'s pattern: the private tick function is
called directly against a bare ``FastAPI()`` (not through the full app/lifespan,
which never runs in the test suite), with ``get_library_optional`` monkeypatched
on the app module (the only dependency this sweep cannot exercise for real).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import MediaRequest, MediaType, RequestStatus
from plex_manager.ports.library import LibraryPort, WatchState
from plex_manager.services import eviction_service, log_capture_service, retention_telemetry_service
from plex_manager.web import app as app_module
from plex_manager.web.deps import EVICTION_INTERVAL_MINUTES_DEFAULT, SettingsStore
from tests.web.fakes import FakeLibrary

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB_ID = 909
_STALE = datetime.now(UTC) - timedelta(days=45)


async def _seed(
    sessionmaker_: SessionMaker,
    *,
    movies_root: str,
    library_path: str,
    eviction_enabled: str = "true",
) -> int:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB_ID,
            media_type=MediaType.movie,
            title="Stale Movie",
            status=RequestStatus.available,
            library_path=library_path,
        )
        session.add(request)
        await session.flush()
        request_id = request.id

        store = SettingsStore(session)
        await store.set("movies_root", movies_root)
        await store.set("eviction_enabled", eviction_enabled)
        # threshold=0 always trips (real disk usage is never negative); target=0
        # asks the sweep to evict every eligible candidate.
        await store.set("disk_pressure_threshold_percent", "0")
        await store.set("disk_pressure_target_percent", "0")
        await store.set("eviction_grace_days", "30")
        await store.set("eviction_interval_minutes", "5")
        await session.commit()
    return request_id


def _app(sessionmaker_: SessionMaker) -> FastAPI:
    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, text="ok"))
    )
    # Mirror production: lifespan always sets ``log_handler`` before the eviction
    # loop starts, and ``_eviction_tick`` reads its ``free_slots`` to pace the
    # telemetry sweep's emission budget against the live log-queue headroom.
    # Constructed here inside the test's running loop (the handler captures it).
    app.state.log_handler = log_capture_service.LogCaptureHandler()
    return app


async def test_eviction_tick_evicts_a_stale_watched_movie(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    request_id = await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_library_optional", _library)

    app = _app(sessionmaker_)
    try:
        sleep_seconds = await app_module._eviction_tick(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    assert sleep_seconds == 5 * 60.0
    assert not movie_file.exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted


async def test_eviction_disabled_setting_is_a_master_kill_switch(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    request_id = await _seed(
        sessionmaker_,
        movies_root=str(tmp_path),
        library_path=str(movie_file),
        eviction_enabled="false",
    )

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_library_optional", _library)

    app = _app(sessionmaker_)
    try:
        await app_module._eviction_tick(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    # Everything else was primed to trigger an eviction -- only the disabled
    # setting stopped it (never a terminal, always a settings toggle).
    assert movie_file.exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available


# --------------------------------------------------------------------------- #
# Retention telemetry (ADR-0012 follow-up): a DELETE-NOTHING sweep that only
# runs when the SAME tick's pressure gate does NOT fire -- proving (a) it never
# changes eviction's own outcome when pressure DOES fire (byte-identical to the
# tests above) and (b) it DOES run, exactly once, when pressure does not.
# --------------------------------------------------------------------------- #


async def test_telemetry_sweep_runs_when_the_pressure_gate_does_not_fire(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    request_id = await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        # Unreachable -- real disk usage can never hit this, so the pressure
        # gate never fires and the real sweep evicts nothing either way.
        await store.set("disk_pressure_threshold_percent", "101")
        await session.commit()

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_library_optional", _library)

    calls: list[str] = []

    async def _fake_sweep(**kwargs: object) -> None:
        calls.append(kwargs["root_path"])  # type: ignore[index]

    monkeypatch.setattr(retention_telemetry_service, "run_retention_telemetry_sweep", _fake_sweep)

    app = _app(sessionmaker_)
    try:
        await app_module._eviction_tick(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    assert calls == [str(tmp_path)]
    # Delete-nothing: the pressure gate never fired, so nothing was evicted --
    # the telemetry sweep never touches the real eviction outcome either way.
    assert movie_file.exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available


async def test_telemetry_sweep_stands_down_when_proactive_eviction_is_enabled(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With proactive eviction ON, the 'about to evict nothing' premise is
    false (the proactive pass acts on the same candidates this tick), so the
    delete-nothing observer stands down instead of doubling the Plex/FS walk
    right before a real cleanup."""
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        # Pressure unreachable, but the proactive pass below is live.
        await store.set("disk_pressure_threshold_percent", "101")
        await store.set("eviction_proactive_enabled", "true")
        await store.set("eviction_grace_days", "0")
        await session.commit()

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_library_optional", _library)

    calls: list[str] = []

    async def _fake_sweep(**kwargs: object) -> None:
        calls.append(kwargs["root_path"])  # type: ignore[index]

    monkeypatch.setattr(retention_telemetry_service, "run_retention_telemetry_sweep", _fake_sweep)

    app = _app(sessionmaker_)
    try:
        await app_module._eviction_tick(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    assert calls == []  # observer stood down: a real retention behaviour is on
    assert not movie_file.exists()  # the proactive pass actually cleaned up


async def test_telemetry_sweep_does_not_run_when_the_pressure_gate_fires(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte-identical eviction behaviour when pressure fires: telemetry is
    strictly additive to the below-threshold case, never invoked (and never
    interfering) once the real sweep is about to act."""
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    # _seed's default threshold/target are both "0" -- always trips.
    await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_library_optional", _library)

    calls: list[str] = []

    async def _fake_sweep(**kwargs: object) -> None:
        calls.append(kwargs["root_path"])  # type: ignore[index]

    monkeypatch.setattr(retention_telemetry_service, "run_retention_telemetry_sweep", _fake_sweep)

    app = _app(sessionmaker_)
    try:
        await app_module._eviction_tick(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    assert calls == []  # never invoked -- pressure fired, the real sweep handled it
    assert not movie_file.exists()  # the real eviction still ran, unaffected


async def test_telemetry_sweep_failure_never_prevents_the_real_eviction(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug in the delete-nothing telemetry sweep must never take down (or
    skip) the real eviction sweep for the SAME root -- they are wired as two
    independent steps, the telemetry one wrapped in its own try/except."""
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    request_id = await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("disk_pressure_threshold_percent", "101")  # gate never fires
        await session.commit()

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_library_optional", _library)

    async def _boom_sweep(**_kwargs: object) -> None:
        raise RuntimeError("telemetry sweep exploded")

    monkeypatch.setattr(retention_telemetry_service, "run_retention_telemetry_sweep", _boom_sweep)

    app = _app(sessionmaker_)
    try:
        # Must not raise -- the tick itself survives the telemetry failure.
        await app_module._eviction_tick(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    # The gate never fired (threshold=101), so the request is still available
    # regardless -- this test's real point is simply that the RuntimeError
    # above never escaped _eviction_tick.
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.available


async def test_telemetry_failure_rolls_back_so_the_real_eviction_still_writes(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A telemetry sweep that raises a SQLAlchemy error leaves the SHARED tick
    session in a poisoned (aborted) transaction. Without a rollback in the tick's
    telemetry except path, the real eviction sweep's own reads/writes on that same
    session would then raise too -- a telemetry bug silently BLOCKING eviction,
    the exact "telemetry can never block eviction" guarantee this subsystem
    promises. The tick must roll the session back so the real eviction still
    commits in the same tick.

    Threshold is set unreachable (101) so the pressure gate never fires: the
    telemetry sweep runs (and poisons the session) while the pressure sweep evicts
    nothing. Proactive eviction -- which ignores pressure and evicts past-grace
    watched content -- is what actually WRITES here, proving the real eviction
    succeeds after the rollback.
    """
    movie_file = tmp_path / "Stale Movie.mkv"
    movie_file.write_bytes(b"0" * 1024)
    request_id = await _seed(sessionmaker_, movies_root=str(tmp_path), library_path=str(movie_file))
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("disk_pressure_threshold_percent", "101")  # gate never fires
        await store.set("eviction_proactive_enabled", "true")
        await session.commit()

    library = FakeLibrary(
        watch_states={(_TMDB_ID, "movie", None): WatchState(watched=True, last_viewed_at=_STALE)}
    )

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_library_optional", _library)

    async def _poisoning_sweep(**kwargs: object) -> None:
        session = kwargs["session"]
        # A statement that fails, aborting the shared transaction the real
        # eviction sweep is about to reuse (mirrors a mid-telemetry DB error).
        await session.execute(text("SELECT * FROM a_table_that_does_not_exist"))  # type: ignore[attr-defined]

    monkeypatch.setattr(
        retention_telemetry_service, "run_retention_telemetry_sweep", _poisoning_sweep
    )

    app = _app(sessionmaker_)
    try:
        # Must not raise: the tick survives the poisoned telemetry AND still evicts.
        await app_module._eviction_tick(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    # The proactive eviction ran and committed on the rolled-back session.
    assert not movie_file.exists()
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
        assert row is not None
        assert row.status is RequestStatus.evicted


class _StopLoop(Exception):
    """Sentinel raised from the patched ``asyncio.sleep`` to end the (real)
    ``while True`` in ``_eviction_loop`` after a bounded number of iterations,
    without ever needing the loop to actually sleep in real time."""


async def test_eviction_loop_survives_a_tick_and_fallback_read_that_both_raise(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3-2 regression: ``_eviction_tick`` failing is already handled by a
    fallback that re-reads ``eviction_interval_minutes`` in a fresh session --
    but if THAT read also raises (the same transient DB hiccup that failed the
    tick), the old code let the exception escape ``_eviction_loop`` entirely,
    silently killing automatic disk-pressure eviction until a process restart.

    Both the tick AND the fallback's settings read are made to raise on every
    iteration; the loop must still log each failure, fall back to the
    hardcoded ``EVICTION_INTERVAL_MINUTES_DEFAULT``, and keep ticking.
    """

    async def _boom_tick(_app: FastAPI) -> float:
        raise RuntimeError("tick failed")

    async def _boom_get_interval(_session: AsyncSession) -> float:
        raise RuntimeError("settings read failed too")

    monkeypatch.setattr(app_module, "_eviction_tick", _boom_tick)
    monkeypatch.setattr(app_module, "get_eviction_interval_minutes", _boom_get_interval)

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Stop the (otherwise infinite) loop once it has proven it survives
        # more than one bad iteration -- never by letting the injected
        # RuntimeErrors above propagate.
        if len(sleep_calls) >= 3:
            raise _StopLoop

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    app = _app(sessionmaker_)
    try:
        with pytest.raises(_StopLoop):
            await app_module._eviction_loop(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    # The loop kept ticking for 3 iterations -- never died on the first (or
    # any) transient failure -- and every one fell all the way back to the
    # safe hardcoded default interval, since even the fallback's own settings
    # read was made to fail.
    assert sleep_calls == [EVICTION_INTERVAL_MINUTES_DEFAULT * 60.0] * 3


# --------------------------------------------------------------------------- #
# #95 -- per-root failure isolation on the SHARED tick session: a DB/SQLAlchemy
# failure sweeping one root must be rolled back so the remaining roots still run
# in the SAME tick, rather than the poisoned transaction cascading into them.
# --------------------------------------------------------------------------- #


async def test_eviction_tick_one_roots_db_failure_still_sweeps_the_next_root(
    sessionmaker_: SessionMaker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#95: every configured root shares THIS tick's single session. A
    DB/SQLAlchemy failure sweeping one root leaves that transaction poisoned;
    without the guarded ``session.rollback()`` the NEXT root's sweep would then
    raise too (``PendingRollbackError``), silently skipping every remaining root.
    Proves root A's failure is caught + rolled back so root B still runs on the
    same, now-clean session in the same tick."""
    movies_root = tmp_path / "movies"
    movies_root.mkdir()
    tv_root = tmp_path / "tv"
    tv_root.mkdir()
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("movies_root", str(movies_root))
        await store.set("tv_root", str(tv_root))
        await store.set("eviction_enabled", "true")
        # threshold=0 always trips, so the pressure sweep runs for every root and
        # the delete-nothing telemetry sweep is skipped (keeping this focused on
        # the eviction sweep's own per-root session hygiene).
        await store.set("disk_pressure_threshold_percent", "0")
        await store.set("disk_pressure_target_percent", "0")
        await store.set("eviction_grace_days", "30")
        await store.set("eviction_interval_minutes", "5")
        await session.commit()

    library = FakeLibrary()

    async def _library(_session: AsyncSession, _client: httpx.AsyncClient) -> LibraryPort | None:
        return library

    monkeypatch.setattr(app_module, "get_library_optional", _library)

    swept: list[str] = []

    async def _fake_sweep(**kwargs: object) -> list[object]:
        session = kwargs["session"]
        assert isinstance(session, AsyncSession)
        root = kwargs["root_path"]
        if kwargs["media_type"] == "movie" and root == str(movies_root):
            # A DB error mid-sweep poisons the shared transaction, then propagates
            # exactly as a real SQLAlchemy failure would.
            await session.execute(text("SELECT * FROM __eviction_no_such_table__"))
            return []  # pragma: no cover - the statement above always raises
        # Any later root: the shared session must be USABLE again (rolled back
        # clean). If it were still poisoned this SELECT would raise
        # PendingRollbackError and this root would never be recorded.
        await session.execute(text("SELECT 1"))
        assert isinstance(root, str)
        swept.append(root)
        return []

    monkeypatch.setattr(eviction_service, "run_eviction_sweep", _fake_sweep)

    app = _app(sessionmaker_)
    try:
        sleep_seconds = await app_module._eviction_tick(app)  # pyright: ignore[reportPrivateUsage]
    finally:
        await app.state.http_client.aclose()

    # The tick completed (never re-raised root A's DB failure) and root B (tv)
    # still ran on the rolled-back, clean session.
    assert sleep_seconds == 5 * 60.0
    assert swept == [str(tv_root)]
