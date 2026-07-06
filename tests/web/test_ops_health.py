"""``GET /api/v1/ops/health`` (ADR-0012, Component 1) — per-subsystem
reachability (honest ``not_configured`` vs ``ok``/``down``), disk gauges, DB
ping, and the reconcile loop's own status.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.services.health_service import ReconcileStatus
from plex_manager.web.deps import SettingsStore

SeedFn = Callable[..., Awaitable[None]]
Handler = Callable[[httpx.Request], httpx.Response]

_API_KEY = "ops-health-key"
_HEADERS = {"X-Api-Key": _API_KEY}


async def _use_transport(app: FastAPI, handler: Handler) -> None:
    """Point the app's shared HTTP client at a mock transport for one test."""
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_health_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/ops/health")
    assert response.status_code == 401


async def test_every_subsystem_is_honestly_not_configured_by_default(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/ops/health", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()

    by_name = {s["name"]: s for s in body["subsystems"]}
    assert {by_name[n]["status"] for n in ("plex", "prowlarr", "qbittorrent", "tmdb")} == {
        "not_configured"
    }
    # The database subsystem is always real (a live in-memory sqlite engine).
    assert by_name["database"]["status"] == "ok"
    # No library root configured -- no disk gauge to report, honestly empty.
    assert body["disks"] == []
    # A fresh process: the reconcile loop has never run.
    assert body["reconcile"] == {
        "last_run_at": None,
        "last_ok_at": None,
        "last_error_type": None,
        "last_error_at": None,
        "consecutive_failures": 0,
    }
    # The auto-grab loop (ADR-0013) surfaces the same fresh-process shape, plus its
    # grab-pipeline cooldown gauge (round-3 #2): a fresh process has nothing cooling.
    assert body["autograb"] == {
        "last_run_at": None,
        "last_ok_at": None,
        "last_error_type": None,
        "last_error_at": None,
        "consecutive_failures": 0,
        "cooled_down_scopes": 0,
    }


async def test_prowlarr_reports_ok_then_down_once_configured(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("prowlarr_url", "http://prowlarr.local")
        await store.set("prowlarr_api_key", "pk")
        await session.commit()

    await _use_transport(app, lambda _r: httpx.Response(200, json={"version": "1.0"}))
    ok_response = await client.get("/api/v1/ops/health", headers=_HEADERS)
    ok_body = {s["name"]: s for s in ok_response.json()["subsystems"]}
    assert ok_body["prowlarr"]["status"] == "ok"
    assert ok_body["prowlarr"]["detail"] is None

    # Force a fresh probe: within the TTL window a second call is EXPECTED to
    # reuse the cached "ok" result (that IS the point of the cache -- see
    # ``test_subsystem_probe_is_ttl_cached_across_requests`` below), so this
    # test clears it explicitly to prove the endpoint reflects a NEW outcome
    # once the upstream actually starts rejecting the key.
    app.state.health_cache.clear()
    await _use_transport(app, lambda _r: httpx.Response(401))
    down_response = await client.get("/api/v1/ops/health", headers=_HEADERS)
    down_body = {s["name"]: s for s in down_response.json()["subsystems"]}
    assert down_body["prowlarr"]["status"] == "down"
    assert down_body["prowlarr"]["detail"] is not None


async def test_subsystem_probe_is_ttl_cached_across_requests(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("prowlarr_url", "http://prowlarr.local")
        await store.set("prowlarr_api_key", "pk")
        await session.commit()

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"version": "1.0"})

    await _use_transport(app, handler)
    first = await client.get("/api/v1/ops/health", headers=_HEADERS)
    second = await client.get("/api/v1/ops/health", headers=_HEADERS)
    assert first.status_code == second.status_code == 200
    # The second call within the TTL window reuses the cached probe result --
    # the whole point of the cache (never hammer an upstream on every poll).
    assert calls["n"] == 1


async def test_health_generation_snapshot_covers_the_credential_read(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex round 3: the endpoint's generation snapshot must be taken BEFORE
    its ``SettingsStore`` credential reads, or an invalidation landing in the
    gap between the credential read and the probes (a ``PUT /settings`` racing
    this request) post-dates a probe-time snapshot and the stale, old-credential
    probe result gets cached for another full TTL. Hooks the endpoint's LAST
    credential read to fire the invalidation in exactly that gap."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("prowlarr_url", "http://prowlarr.local")
        await store.set("prowlarr_api_key", "old-key")
        await session.commit()

    await _use_transport(app, lambda _r: httpx.Response(200, json={"version": "1.0"}))

    real_get = SettingsStore.get

    async def get_then_invalidate(self: SettingsStore, key: str) -> str | None:
        value = await real_get(self, key)
        if key == "tmdb_api_key":
            # The endpoint's LAST credential read: the (old) prowlarr key has
            # already left the store, no probe has started yet -- a settings
            # save committing right here invalidates "prowlarr" in the exact
            # window a probe-time snapshot cannot see.
            app.state.health_cache.invalidate("prowlarr")
        return value

    monkeypatch.setattr(SettingsStore, "get", get_then_invalidate)

    response = await client.get("/api/v1/ops/health", headers=_HEADERS)
    assert response.status_code == 200
    by_name = {s["name"]: s for s in response.json()["subsystems"]}
    # The response honestly reports what the old-credential probe saw...
    assert by_name["prowlarr"]["status"] == "ok"
    # ...but that stale result must NOT have been cached: the mid-request
    # invalidation wins, and the next poll re-probes with fresh credentials.
    assert app.state.health_cache.get("prowlarr") is None
    # The fence is per-key: subsystems the mid-request save didn't touch
    # cached their (not_configured) results normally.
    assert app.state.health_cache.get("plex") is not None


async def test_health_reports_disk_gauge_for_a_configured_root(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn, tmp_path: Path
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    async with app.state.sessionmaker() as session:
        await SettingsStore(session).set("movies_root", str(tmp_path))
        await session.commit()

    response = await client.get("/api/v1/ops/health", headers=_HEADERS)
    disks = response.json()["disks"]
    assert len(disks) == 1
    assert disks[0]["root"] == "movies_root"
    assert disks[0]["path"] == str(tmp_path)
    assert disks[0]["total_bytes"] > 0
    assert disks[0]["error"] is None


async def test_health_reflects_the_live_reconcile_status(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    status = ReconcileStatus()
    status.mark_run_started()
    status.mark_ok()
    app.state.reconcile_status = status

    response = await client.get("/api/v1/ops/health", headers=_HEADERS)
    reconcile = response.json()["reconcile"]
    assert reconcile["last_run_at"] is not None
    assert reconcile["last_ok_at"] is not None
    assert reconcile["last_error_type"] is None
    assert reconcile["consecutive_failures"] == 0
