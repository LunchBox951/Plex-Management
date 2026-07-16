"""Plex-native artwork proxy (issue #66) — the backend image proxy that keeps the
Plex token server-side.

These tests pin the route contract: an authenticated request for an in-library
title streams the Plex image bytes with the upstream ``Content-Type``; every
not-available path (Plex unconfigured, not in library, no art, Plex down) degrades
to 404 so the browser falls back to TMDB, never a 500. Auth is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import (
    PlexLibrary,
    PlexLibraryError,
    _ArtworkKeys,  # pyright: ignore[reportPrivateUsage]
    reset_caches,
)
from plex_manager.ports.library import ArtworkImage, ArtworkKind, LibraryPort
from plex_manager.web import deps
from plex_manager.web.deps import SettingsStore
from tests.support import assert_task_raises
from tests.web.fakes import FakeLibrary, override_adapters

SessionMaker = async_sessionmaker[AsyncSession]
SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "artwork-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_POSTER = ArtworkImage(content=b"\x89PNG\r\n\x1a\nposter-bytes", content_type="image/png")


class _TrackingAsyncSession(AsyncSession):
    closed = False

    async def close(self) -> None:
        type(self).closed = True
        await super().close()


async def test_optional_library_short_session_closes_before_fetch_and_reuses_client(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
) -> None:
    _TrackingAsyncSession.closed = False
    maker = async_sessionmaker(engine, class_=_TrackingAsyncSession, expire_on_commit=False)
    app = FastAPI()
    app.state.sessionmaker = maker
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/"})
    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    captured_client: httpx.AsyncClient | None = None

    class ProbeLibrary(FakeLibrary):
        async def fetch_artwork(
            self,
            tmdb_id: int,
            media_type: Literal["movie", "tv"],
            kind: ArtworkKind,
        ) -> ArtworkImage | None:
            assert _TrackingAsyncSession.closed is True
            return None

    async def probe_get_library(session: AsyncSession, client: httpx.AsyncClient) -> LibraryPort:
        nonlocal captured_client
        assert isinstance(session, _TrackingAsyncSession)
        assert _TrackingAsyncSession.closed is False
        captured_client = client
        return ProbeLibrary()

    monkeypatch.setattr(deps, "get_library", probe_get_library)
    try:
        library = await deps.get_library_optional_short_session(request, shared_client)
        assert library is not None
        assert _TrackingAsyncSession.closed is True
        assert captured_client is shared_client
        assert await library.fetch_artwork(1, "movie", "poster") is None
    finally:
        await shared_client.aclose()


async def test_optional_library_short_session_returns_none_only_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
    engine: AsyncEngine,
) -> None:
    _TrackingAsyncSession.closed = False
    app = FastAPI()
    app.state.sessionmaker = async_sessionmaker(
        engine, class_=_TrackingAsyncSession, expire_on_commit=False
    )
    request = Request({"type": "http", "app": app, "method": "GET", "path": "/"})
    shared_client = httpx.AsyncClient()

    async def unconfigured(_session: AsyncSession, client: httpx.AsyncClient) -> LibraryPort:
        assert client is shared_client
        raise deps.ServiceNotConfiguredError("plex")

    monkeypatch.setattr(deps, "get_library", unconfigured)
    try:
        assert await deps.get_library_optional_short_session(request, shared_client) is None
        assert _TrackingAsyncSession.closed is True
    finally:
        await shared_client.aclose()


async def _configure_plex(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("plex_url", "http://plex.test:32400")
        await store.set("plex_token", "route-test-token")
        await session.commit()


async def test_route_actual_dependencies_close_auth_and_config_sessions_before_fetch(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure_plex(sessionmaker_)

    class RouteTrackingSession(AsyncSession):
        active = 0
        closed = 0

        async def __aenter__(self) -> AsyncSession:
            type(self).active += 1
            return await super().__aenter__()

        async def close(self) -> None:
            type(self).active -= 1
            type(self).closed += 1
            await super().close()

    app.state.sessionmaker = async_sessionmaker(
        engine, class_=RouteTrackingSession, expire_on_commit=False
    )
    original_fetch = PlexLibrary.fetch_artwork

    async def observed_fetch(
        self: PlexLibrary,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        kind: ArtworkKind,
    ) -> ArtworkImage | None:
        assert RouteTrackingSession.active == 0
        assert RouteTrackingSession.closed >= 2
        return await original_fetch(self, tmdb_id, media_type, kind)

    monkeypatch.setattr(PlexLibrary, "fetch_artwork", observed_fetch)
    response = await client.get("/api/v1/artwork/plex/movie/603/poster", headers=_HEADERS)

    assert response.status_code == 404
    assert RouteTrackingSession.active == 0
    assert RouteTrackingSession.closed >= 2


async def test_route_real_plex_library_limiter_caps_burst_at_four(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_caches()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure_plex(sessionmaker_)
    entered: asyncio.Queue[int] = asyncio.Queue()
    release = asyncio.Event()

    async def blocked_keys(
        _self: PlexLibrary, tmdb_id: int, _media_type: Literal["movie", "tv"]
    ) -> _ArtworkKeys | None:
        await entered.put(tmdb_id)
        await release.wait()
        return None

    monkeypatch.setattr(PlexLibrary, "_artwork_keys", blocked_keys)
    tasks = [
        asyncio.create_task(
            client.get(f"/api/v1/artwork/plex/movie/{1000 + index}/poster", headers=_HEADERS)
        )
        for index in range(8)
    ]
    try:
        first_four = [await asyncio.wait_for(entered.get(), timeout=1.0) for _ in range(4)]
        assert len(set(first_four)) == 4
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(entered.get(), timeout=0.05)
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
        assert [response.status_code for response in responses] == [404] * 8
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        reset_caches()


async def test_route_real_limiter_releases_after_typed_upstream_error(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_caches()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure_plex(sessionmaker_)
    entered: asyncio.Queue[int] = asyncio.Queue()
    release = asyncio.Event()

    async def keys(
        _self: PlexLibrary, tmdb_id: int, _media_type: Literal["movie", "tv"]
    ) -> _ArtworkKeys:
        await entered.put(tmdb_id)
        if tmdb_id == 1100:
            raise PlexLibraryError("synthetic typed upstream failure")
        await release.wait()
        return _ArtworkKeys(None, None)

    monkeypatch.setattr(PlexLibrary, "_artwork_keys", keys)
    tasks = [
        asyncio.create_task(
            client.get(f"/api/v1/artwork/plex/movie/{1100 + index}/poster", headers=_HEADERS)
        )
        for index in range(5)
    ]
    try:
        first_four = [await asyncio.wait_for(entered.get(), timeout=1.0) for _ in range(4)]
        assert 1100 in first_four
        fifth = await asyncio.wait_for(entered.get(), timeout=1.0)
        assert fifth not in first_four
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
        assert [response.status_code for response in responses] == [404] * 5
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        reset_caches()


async def test_route_real_limiter_releases_after_request_cancellation(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_caches()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _configure_plex(sessionmaker_)
    entered: asyncio.Queue[int] = asyncio.Queue()
    release = asyncio.Event()

    async def blocked_keys(
        _self: PlexLibrary, tmdb_id: int, _media_type: Literal["movie", "tv"]
    ) -> _ArtworkKeys | None:
        await entered.put(tmdb_id)
        await release.wait()
        return None

    monkeypatch.setattr(PlexLibrary, "_artwork_keys", blocked_keys)
    admitted = [
        asyncio.create_task(
            client.get(f"/api/v1/artwork/plex/movie/{1200 + index}/poster", headers=_HEADERS)
        )
        for index in range(4)
    ]
    queued: asyncio.Task[httpx.Response] | None = None
    try:
        first_four = [await asyncio.wait_for(entered.get(), timeout=1.0) for _ in range(4)]
        queued = asyncio.create_task(
            client.get("/api/v1/artwork/plex/movie/1300/poster", headers=_HEADERS)
        )
        cancelled_id = first_four[0]
        cancelled_task = admitted[cancelled_id - 1200]
        cancelled_task.cancel()
        await assert_task_raises(cancelled_task, asyncio.CancelledError)
        assert await asyncio.wait_for(entered.get(), timeout=1.0) == 1300
        release.set()
        remaining = [task for task in admitted if task is not cancelled_task]
        responses = await asyncio.wait_for(asyncio.gather(*remaining, queued), timeout=2.0)
        assert [response.status_code for response in responses] == [404] * 4
    finally:
        release.set()
        cleanup = [*admitted]
        if queued is not None:
            cleanup.append(queued)
        await asyncio.gather(*cleanup, return_exceptions=True)
        reset_caches()


async def test_proxy_streams_plex_image_for_in_library_title(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    library = FakeLibrary(artwork={(603, "movie", "poster"): _POSTER})
    override_adapters(app, library=library)

    response = await client.get("/api/v1/artwork/plex/movie/603/poster", headers=_HEADERS)

    assert response.status_code == 200
    assert response.content == _POSTER.content
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=86400"
    # The proxy asked the port with exactly the client-named key — nothing else.
    assert library.fetch_artwork_calls == [(603, "movie", "poster")]


async def test_proxy_requires_authentication(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=FakeLibrary(artwork={(603, "movie", "poster"): _POSTER}))

    response = await client.get("/api/v1/artwork/plex/movie/603/poster")

    assert response.status_code == 401


async def test_proxy_404s_when_plex_unconfigured(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # No Plex creds and no library override -> get_library_optional is None. The
    # browser falls back to TMDB, so a 404 (not a 409/500) is the honest answer.
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/artwork/plex/movie/603/poster", headers=_HEADERS)

    assert response.status_code == 404


async def test_proxy_404s_when_title_has_no_plex_artwork(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=FakeLibrary(artwork={}))

    response = await client.get("/api/v1/artwork/plex/movie/999/poster", headers=_HEADERS)

    assert response.status_code == 404


async def test_proxy_404s_when_plex_is_down(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # A Plex outage must degrade to TMDB (404), never surface a 500.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app, library=FakeLibrary(artwork_raises=PlexLibraryError("plex request to /x failed"))
    )

    response = await client.get("/api/v1/artwork/plex/movie/603/poster", headers=_HEADERS)

    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["thumbnail", "art", "", "poster/../.."])
async def test_proxy_rejects_unknown_kind(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, bad: str
) -> None:
    # ``kind`` is a Literal path param: anything but poster/background is a 4xx
    # before the adapter is ever consulted (no arbitrary passthrough).
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=FakeLibrary(artwork={}))

    response = await client.get(f"/api/v1/artwork/plex/movie/603/{bad}", headers=_HEADERS)

    assert response.status_code in (404, 422)
