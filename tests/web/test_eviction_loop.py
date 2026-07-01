"""``_eviction_tick`` — the operability beta's periodic sweep wiring
(ADR-0012): settings resolution, the master ``eviction_enabled`` kill switch,
and the root-scoped filesystem it hands to ``eviction_service``.

Mirrors ``test_reconcile_loop.py``'s pattern: the private tick function is
called directly against a bare ``FastAPI()`` (not through the full app/lifespan,
which never runs in the test suite), with ``get_library_optional`` monkeypatched
on the app module (the only dependency this sweep cannot exercise for real).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import MediaRequest, MediaType, RequestStatus
from plex_manager.ports.library import LibraryPort, WatchState
from plex_manager.web import app as app_module
from plex_manager.web.deps import SettingsStore
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
