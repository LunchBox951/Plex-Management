"""Watchlist worker lifecycle and honest skip-state coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from fastapi import FastAPI

from plex_manager.web import app as app_module
from plex_manager.web.deps import SettingsStore

SeedFn = Callable[..., Awaitable[None]]


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
