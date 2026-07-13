"""Watchlist worker lifecycle and honest skip-state coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.models import User, WatchlistItem
from plex_manager.web import app as app_module
from plex_manager.web.deps import PLEX_MACHINE_ID_SETTING, SettingsStore

SeedFn = Callable[..., Awaitable[None]]

_MACHINE_ID = "configured-server-machine-id"


def _plex_tv_resources_transport(resources: list[dict[str, object]]) -> httpx.MockTransport:
    """A transport answering plex.tv ``/api/v2/resources`` (used by watchlist
    revalidation). Any other path answers a trivial 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=resources)
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handler)


def _server_resource(machine_id: str) -> dict[str, object]:
    return {
        "name": "Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [],
    }


async def test_watchlist_tick_reports_disabled_without_claiming_success(
    app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        await SettingsStore(session).set("watchlist_sync_enabled", "false")
        await session.commit()

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.state == "disabled"
    assert status.last_run_at is not None
    assert status.last_ok_at is None


async def test_disabling_sync_clears_stale_snapshot_rows(app: FastAPI, seed: SeedFn) -> None:
    """Turning off watchlist sync must END eviction protection, not merely stop
    future ticks: the disabled tick clears the stored snapshot (#296)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        await SettingsStore(session).set("watchlist_sync_enabled", "false")
        user = User(username="watcher", encrypted_plex_token="t")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    assert app.state.watchlist_status.state == "disabled"

    async with app.state.sessionmaker() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert remaining == []


async def test_stale_user_is_skipped_and_snapshot_cleared(app: FastAPI, seed: SeedFn) -> None:
    """A stored token that no longer reaches the configured server (e.g. after a
    repoint) is skipped AND its pre-existing snapshot rows are deleted, so a stale
    old-server account can neither create nor keep PROTECTING titles from eviction
    on the new server (#296 finding 1 -- both halves)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("tmdb_api_key", "tmdb-key")
        await store.set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        user = User(username="stale-watcher", encrypted_plex_token="old-token")  # noqa: S106
        session.add(user)
        await session.flush()
        # A snapshot row left behind from when this account WAS authorized: without
        # the clear-on-stale fix it would keep protecting tmdb 603 forever.
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    # plex.tv only advertises a DIFFERENT server for this token: not authorized.
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(
        transport=_plex_tv_resources_transport([_server_resource("some-other-server")])
    )

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.skipped_users == 1
    assert status.created == 0
    async with app.state.sessionmaker() as session:
        assert list((await session.execute(WatchlistItem.__table__.select())).all()) == []


async def test_unknown_authorization_retains_snapshot(app: FastAPI, seed: SeedFn) -> None:
    """A transient plex.tv outage (authorization UNKNOWN) must skip the tick but
    RETAIN the snapshot -- it must never be mistaken for a revoked account and
    have its eviction-protection rows deleted (#296)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("tmdb_api_key", "tmdb-key")
        await store.set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        user = User(username="watcher", encrypted_plex_token="live-token")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    def _unreachable(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            raise httpx.ConnectError("plex.tv unreachable", request=request)
        return httpx.Response(200, text="ok")

    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_unreachable))

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.skipped_users == 1
    async with app.state.sessionmaker() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert len(remaining) == 1


async def test_watchlist_tick_reports_not_configured_without_tmdb(
    app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True)

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.state == "not_configured"
    assert status.last_run_at is not None
    assert status.last_ok_at is None


async def test_watchlist_loop_wakes_immediately_when_settings_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_tick = asyncio.Event()
    second_tick = asyncio.Event()
    calls = 0

    async def fake_tick(_app: FastAPI) -> int:
        nonlocal calls
        calls += 1
        (first_tick if calls == 1 else second_tick).set()
        return 0

    async def long_interval(_session: object) -> float:
        return 10_080

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(app_module, "_watchlist_sync_once", fake_tick)
    monkeypatch.setattr(app_module, "get_watchlist_sync_interval_minutes", long_interval)
    app = FastAPI()
    app.state.sessionmaker = lambda: SessionContext()
    app.state.watchlist_wake_event = asyncio.Event()
    task = asyncio.create_task(
        app_module._watchlist_sync_loop(app)  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(first_tick.wait(), timeout=1)
        app.state.watchlist_wake_event.set()
        await asyncio.wait_for(second_tick.wait(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert calls >= 2
