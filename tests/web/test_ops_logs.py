"""``GET /api/v1/ops/logs``, ``/logs/tail``, ``/logs/export`` (ADR-0012,
Component 2) — the durable, filterable store, the live all-levels ring-buffer
tail, and the LLM-diagnosis export bundle.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import LogEvent
from plex_manager.services import log_capture_service
from plex_manager.web.routers import ops as ops_router

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "ops-logs-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_NOW = datetime.now(UTC)


async def _insert_event(
    sm: SessionMaker,
    *,
    level: str,
    message: str,
    logger: str = "plex_manager.test",
    created_at: datetime | None = None,
    context: dict[str, object] | None = None,
) -> None:
    async with sm() as session:
        row = LogEvent(level=level, logger=logger, message=message, context_json=context)
        if created_at is not None:
            row.created_at = created_at
        session.add(row)
        await session.commit()


# --------------------------------------------------------------------------- #
# GET /logs — the durable, filtered store
# --------------------------------------------------------------------------- #
async def test_logs_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    assert (await client.get("/api/v1/ops/logs")).status_code == 401


async def test_logs_lists_newest_first_and_reports_total(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert_event(sessionmaker_, level="INFO", message="first", created_at=_NOW)
    await _insert_event(
        sessionmaker_, level="ERROR", message="second", created_at=_NOW + timedelta(seconds=1)
    )

    response = await client.get("/api/v1/ops/logs", headers=_HEADERS)
    body = response.json()
    assert body["total"] == 2
    assert [e["message"] for e in body["events"]] == ["second", "first"]


async def test_logs_filters_by_level_and_correlation_id(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert_event(
        sessionmaker_, level="ERROR", message="grab failed", context={"download_id": 42}
    )
    await _insert_event(sessionmaker_, level="INFO", message="unrelated", context={"tmdb_id": 1})

    by_level = (
        await client.get("/api/v1/ops/logs", params={"level": "ERROR"}, headers=_HEADERS)
    ).json()
    assert by_level["total"] == 1
    assert by_level["events"][0]["message"] == "grab failed"

    by_correlation = (
        await client.get("/api/v1/ops/logs", params={"correlation_id": "42"}, headers=_HEADERS)
    ).json()
    assert by_correlation["total"] == 1
    assert by_correlation["events"][0]["context"] == {"download_id": 42}


async def test_logs_pagination_limit_offset(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    for i in range(3):
        await _insert_event(
            sessionmaker_, level="INFO", message=f"m{i}", created_at=_NOW + timedelta(seconds=i)
        )

    page = (
        await client.get("/api/v1/ops/logs", params={"limit": 1, "offset": 1}, headers=_HEADERS)
    ).json()
    assert page["total"] == 3
    assert len(page["events"]) == 1
    assert page["events"][0]["message"] == "m1"  # newest-first: m2, m1, m0


# --------------------------------------------------------------------------- #
# GET /logs/tail — the live, all-levels ring buffer
# --------------------------------------------------------------------------- #
async def test_tail_requires_a_configured_log_handler(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # The `app` fixture never runs `lifespan` (see tests/web/conftest.py), so
    # `app.state.log_handler` is genuinely absent here -- the honest 503, not a
    # crash, for a router hit before logging was ever configured.
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/ops/logs/tail", headers=_HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"] == "log_handler_unavailable"


async def test_tail_reads_the_live_ring_buffer_newest_first(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    test_logger = logging.getLogger("plex_manager.test.tail")
    test_logger.propagate = False  # isolate from the real root logger
    handler = log_capture_service.configure_logging("DEBUG", logger=test_logger)
    app.state.log_handler = handler
    try:
        test_logger.debug("debug line")  # below INFO -- tail-only, never in log_events
        test_logger.warning("warn line", extra={"request_id": 7})

        response = await client.get("/api/v1/ops/logs/tail", headers=_HEADERS)
        body = response.json()
        assert [e["message"] for e in body["events"]] == ["warn line", "debug line"]
        assert body["events"][0]["context"] == {"request_id": 7}
        assert body["dropped_count"] == 0
    finally:
        log_capture_service.stop_logging(handler, logger=test_logger)


async def test_tail_limit_caps_the_returned_slice(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    test_logger = logging.getLogger("plex_manager.test.tail_limit")
    test_logger.propagate = False
    handler = log_capture_service.configure_logging("INFO", logger=test_logger)
    app.state.log_handler = handler
    try:
        for i in range(5):
            test_logger.info("line %d", i)

        response = await client.get("/api/v1/ops/logs/tail", params={"limit": 2}, headers=_HEADERS)
        messages = [e["message"] for e in response.json()["events"]]
        assert messages == ["line 4", "line 3"]
    finally:
        log_capture_service.stop_logging(handler, logger=test_logger)


# --------------------------------------------------------------------------- #
# GET /logs/export — the LLM-diagnosis bundle
# --------------------------------------------------------------------------- #
async def test_export_by_correlation_id_returns_the_whole_trail_oldest_first(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert_event(
        sessionmaker_,
        level="INFO",
        message="grab started",
        context={"download_id": 9},
        created_at=_NOW,
    )
    await _insert_event(
        sessionmaker_,
        level="ERROR",
        message="grab failed",
        context={"download_id": 9},
        created_at=_NOW + timedelta(seconds=5),
    )
    await _insert_event(
        sessionmaker_, level="INFO", message="unrelated", context={"download_id": 123}
    )

    response = await client.get(
        "/api/v1/ops/logs/export", params={"correlation_id": "9"}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    lines = response.text.strip("\n").split("\n")
    assert len(lines) == 2
    assert "grab started" in lines[0]
    assert "grab failed" in lines[1]  # oldest-first: a coherent top-to-bottom story
    assert "unrelated" not in response.text


async def test_export_json_format_returns_the_same_events_structured(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert_event(
        sessionmaker_, level="ERROR", message="boom", context={"tmdb_id": 603}, created_at=_NOW
    )

    response = await client.get(
        "/api/v1/ops/logs/export",
        params={"correlation_id": "603", "format": "json"},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["total"] == 1
    assert body["events"][0]["message"] == "boom"


async def test_export_time_window_excludes_events_older_than_default_24h(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert_event(
        sessionmaker_, level="INFO", message="recent", created_at=_NOW - timedelta(hours=1)
    )
    await _insert_event(
        sessionmaker_, level="INFO", message="stale", created_at=_NOW - timedelta(hours=48)
    )

    response = await client.get("/api/v1/ops/logs/export", headers=_HEADERS)
    assert "recent" in response.text
    assert "stale" not in response.text


async def test_export_time_window_truncation_keeps_oldest_rows(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #96: before the fix, a truncated export kept the
    NEWEST rows (a ``DESC ... LIMIT`` reversed in Python), silently dropping
    the oldest/root-cause lead-up. With the cap forced below the seeded
    count, the export must keep the oldest rows and name the newest as
    dropped."""
    monkeypatch.setattr(ops_router, "_MAX_EXPORT_ROWS", 2)
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert_event(
        sessionmaker_, level="INFO", message="root", created_at=_NOW - timedelta(hours=3)
    )
    await _insert_event(
        sessionmaker_, level="INFO", message="middle", created_at=_NOW - timedelta(hours=2)
    )
    await _insert_event(
        sessionmaker_, level="INFO", message="latest", created_at=_NOW - timedelta(hours=1)
    )

    response = await client.get("/api/v1/ops/logs/export", headers=_HEADERS)
    assert "root" in response.text
    assert "middle" in response.text
    assert "latest" not in response.text  # newest is the one dropped
    assert "truncated" in response.text
    assert "1 newer" in response.text


async def test_export_json_truncation_reports_total_and_keeps_oldest(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ops_router, "_MAX_EXPORT_ROWS", 2)
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert_event(
        sessionmaker_, level="INFO", message="root", created_at=_NOW - timedelta(hours=3)
    )
    await _insert_event(
        sessionmaker_, level="INFO", message="middle", created_at=_NOW - timedelta(hours=2)
    )
    await _insert_event(
        sessionmaker_, level="INFO", message="latest", created_at=_NOW - timedelta(hours=1)
    )

    response = await client.get(
        "/api/v1/ops/logs/export", params={"format": "json"}, headers=_HEADERS
    )
    body = response.json()
    assert body["total"] == 3
    assert [e["message"] for e in body["events"]] == ["root", "middle"]
