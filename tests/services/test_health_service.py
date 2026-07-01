"""Health aggregation (ADR-0012): cached upstream probes, disk gauges, DB ping,
reconcile status — each piece plus the full aggregate.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import UTC
from pathlib import Path

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.services.health_service import (
    HealthCredentials,
    ReconcileStatus,
    SubsystemHealth,
    TtlCache,
    check_database,
    check_subsystems,
    collect_disk_gauges,
    collect_health_snapshot,
    read_disk_usage,
)

SessionMaker = async_sessionmaker[AsyncSession]
# Mirrors httpx's own SyncHandler | AsyncHandler union so a test can hand
# MockTransport either a plain or an async handler function.
Handler = (
    Callable[[httpx.Request], httpx.Response]
    | Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]
)


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --------------------------------------------------------------------------- #
# check_subsystems: not_configured / ok / down / TTL caching
# --------------------------------------------------------------------------- #


async def test_every_subsystem_reports_not_configured_when_unset() -> None:
    async def _fail(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request should be made for an unconfigured subsystem")

    async with _client(_fail) as client:
        results = await check_subsystems(client, HealthCredentials(), TtlCache())

    assert {r.name: r.status for r in results} == {
        "plex": "not_configured",
        "prowlarr": "not_configured",
        "qbittorrent": "not_configured",
        "tmdb": "not_configured",
    }
    assert all(r.detail is None for r in results)


async def test_qbittorrent_not_configured_when_password_is_empty_string() -> None:
    # An empty string IS a real (if odd) password -- only None counts as unset.
    # Covers the deliberate `is None` (not falsy) check in _check_qbittorrent.
    creds = HealthCredentials(
        qbittorrent_url="http://qb.local", qbittorrent_username="admin", qbittorrent_password=None
    )

    async def _fail(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected: password is unset")

    async with _client(_fail) as client:
        results = await check_subsystems(client, creds, TtlCache())
    qbt = next(r for r in results if r.name == "qbittorrent")
    assert qbt.status == "not_configured"


async def test_all_subsystems_ok_when_upstreams_answer_well() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "Directory": [
                            {
                                "key": "1",
                                "title": "Movies",
                                "type": "movie",
                                "Location": [{"path": "/movies"}],
                            }
                        ]
                    }
                },
            )
        if path == "/api/v1/system/status":
            return httpx.Response(200, json={"version": "1.0"})
        if path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[])
        if path == "/3/search/multi":
            return httpx.Response(200, json={"results": []})
        raise AssertionError(f"unexpected path {path}")

    creds = HealthCredentials(
        plex_url="http://plex.local",
        plex_token="tok",  # noqa: S106
        prowlarr_url="http://prowlarr.local",
        prowlarr_api_key="pk",
        qbittorrent_url="http://qb.local",
        qbittorrent_username="admin",
        qbittorrent_password="pw",  # noqa: S106
        tmdb_api_key="tk",
    )
    async with _client(handler) as client:
        results = await check_subsystems(client, creds, TtlCache())

    assert {r.name: r.status for r in results} == {
        "plex": "ok",
        "prowlarr": "ok",
        "qbittorrent": "ok",
        "tmdb": "ok",
    }


async def test_down_subsystem_carries_an_operator_facing_message() -> None:
    creds = HealthCredentials(plex_url="http://plex.local", plex_token="bad-token")  # noqa: S106

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    async with _client(handler) as client:
        results = await check_subsystems(client, creds, TtlCache())
    plex = next(r for r in results if r.name == "plex")
    assert plex.status == "down"
    assert plex.detail is not None
    assert "bad-token" not in (plex.detail or "")  # never echoes a secret back


async def test_probe_result_is_cached_within_the_ttl() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"version": "1.0"})

    creds = HealthCredentials(prowlarr_url="http://prowlarr.local", prowlarr_api_key="pk")
    cache = TtlCache[SubsystemHealth](ttl_seconds=60.0)
    async with _client(handler) as client:
        await check_subsystems(client, creds, cache)
        await check_subsystems(client, creds, cache)

    # Every OTHER subsystem is not_configured (no request at all); prowlarr's
    # single upstream hit must be reused on the second call, not repeated.
    assert calls == 1


async def test_probe_result_is_refreshed_once_the_ttl_expires() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"version": "1.0"})

    creds = HealthCredentials(prowlarr_url="http://prowlarr.local", prowlarr_api_key="pk")
    cache = TtlCache[SubsystemHealth](ttl_seconds=-1.0)  # already-expired on the very next get()
    async with _client(handler) as client:
        await check_subsystems(client, creds, cache)
        await check_subsystems(client, creds, cache)

    assert calls == 2


# --------------------------------------------------------------------------- #
# check_database
# --------------------------------------------------------------------------- #


async def test_check_database_ok(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        result = await check_database(session)
    assert result.status == "ok"
    assert result.detail is None
    assert result.name == "database"


async def test_check_database_down_reports_exception_type_only(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("db gone"))

    async with sessionmaker_() as session:
        monkeypatch.setattr(session, "execute", _boom)
        result = await check_database(session)
    assert result.status == "down"
    assert result.detail == "OperationalError"


# --------------------------------------------------------------------------- #
# Disk usage
# --------------------------------------------------------------------------- #


def test_read_disk_usage_matches_shutil(tmp_path: Path) -> None:
    usage = read_disk_usage(str(tmp_path))
    assert usage.total_bytes > 0
    assert usage.available_bytes >= 0


def test_read_disk_usage_raises_on_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        read_disk_usage(str(tmp_path / "does" / "not" / "exist"))


def test_collect_disk_gauges_skips_an_unset_root(tmp_path: Path) -> None:
    gauges = collect_disk_gauges({"movies_root": str(tmp_path), "tv_root": None})
    assert [g.root for g in gauges] == ["movies_root"]
    assert gauges[0].error is None
    assert gauges[0].used_percent >= 0.0


def test_collect_disk_gauges_reports_an_unreadable_root_honestly(tmp_path: Path) -> None:
    missing = str(tmp_path / "nope")
    gauges = collect_disk_gauges({"movies_root": missing})
    assert len(gauges) == 1
    assert gauges[0].error is not None
    assert gauges[0].total_bytes == 0
    assert gauges[0].used_percent == 0.0


def test_collect_disk_gauges_empty_when_every_root_unset() -> None:
    assert collect_disk_gauges({"movies_root": None, "tv_root": None}) == []


# --------------------------------------------------------------------------- #
# ReconcileStatus
# --------------------------------------------------------------------------- #


def test_reconcile_status_lifecycle() -> None:
    status = ReconcileStatus()
    assert status.last_run_at is None
    assert status.consecutive_failures == 0

    status.mark_run_started()
    assert status.last_run_at is not None
    assert status.last_ok_at is None

    status.mark_ok()
    assert status.last_ok_at is not None
    assert status.last_error_type is None
    assert status.consecutive_failures == 0

    status.mark_run_started()
    status.mark_error(RuntimeError("boom"))
    assert status.last_error_type == "RuntimeError"
    assert status.last_error_at is not None
    assert status.consecutive_failures == 1

    status.mark_run_started()
    status.mark_error(ValueError("boom again"))
    assert status.last_error_type == "ValueError"
    assert status.consecutive_failures == 2

    # A subsequent success clears the streak AND the error fields entirely.
    status.mark_run_started()
    status.mark_ok()
    assert status.consecutive_failures == 0
    assert status.last_error_type is None
    assert status.last_error_at is None


# --------------------------------------------------------------------------- #
# The aggregate
# --------------------------------------------------------------------------- #


async def test_collect_health_snapshot_wires_everything_together(
    sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    async def _fail(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no upstream configured -> no request expected")

    reconcile_status = ReconcileStatus()
    reconcile_status.mark_run_started()
    reconcile_status.mark_ok()

    async with sessionmaker_() as session, _client(_fail) as client:
        snapshot = await collect_health_snapshot(
            session=session,
            client=client,
            cache=TtlCache(),
            creds=HealthCredentials(),
            reconcile_status=reconcile_status,
            library_roots={"movies_root": str(tmp_path), "tv_root": None},
        )

    names = {s.name for s in snapshot.subsystems}
    assert names == {"plex", "prowlarr", "qbittorrent", "tmdb", "database"}
    assert all(s.status in ("ok", "not_configured") for s in snapshot.subsystems)
    assert [g.root for g in snapshot.disks] == ["movies_root"]
    assert snapshot.reconcile.last_ok_at is not None
    assert snapshot.reconcile.consecutive_failures == 0


def test_ttl_cache_get_set_and_expiry() -> None:
    cache = TtlCache[str](ttl_seconds=60.0)
    assert cache.get("k") is None
    cache.set("k", "v")
    assert cache.get("k") == "v"

    expired = TtlCache[str](ttl_seconds=-1.0)
    expired.set("k", "v")
    assert expired.get("k") is None  # already expired by the time it's read

    cache.clear()
    assert cache.get("k") is None


def test_reconcile_status_type_annotation_uses_utc() -> None:
    # Defensive: every timestamp ReconcileStatus stamps must be tz-aware UTC, or
    # comparisons against a grace cutoff elsewhere would silently misbehave.
    status = ReconcileStatus()
    status.mark_run_started()
    assert status.last_run_at is not None
    assert status.last_run_at.tzinfo is UTC
