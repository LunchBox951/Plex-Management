"""``SqlLogEventRepository`` create / create_many / list_events / prune_older_than."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import LogEvent
from plex_manager.ports.repositories import LogEventCreate
from plex_manager.repositories import SqlLogEventRepository
from plex_manager.repositories import log_events as log_events_module

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def test_create_then_list_returns_persisted_record(session: AsyncSession) -> None:
    repo = SqlLogEventRepository(session)
    created = await repo.create(
        level="INFO", logger="plex_manager.services.reconciler", message="tick"
    )
    assert created.id > 0
    assert created.created_at is not None
    assert created.level == "INFO"
    assert created.logger == "plex_manager.services.reconciler"
    assert created.message == "tick"
    assert created.context is None


async def test_create_accepts_explicit_created_at_and_context(session: AsyncSession) -> None:
    repo = SqlLogEventRepository(session)
    created = await repo.create(
        level="ERROR",
        logger="plex_manager.services.grab_service",
        message="grab failed",
        created_at=_T0,
        context={"request_id": 42, "tmdb_id": 603},
    )
    assert created.created_at == _T0
    assert created.context == {"request_id": 42, "tmdb_id": 603}


async def test_create_many_batch_inserts_preserving_original_timestamps(
    session: AsyncSession,
) -> None:
    repo = SqlLogEventRepository(session)
    events = [
        LogEventCreate(
            created_at=_T0 + timedelta(seconds=i),
            level="INFO",
            logger="plex_manager.services.reconciler",
            message=f"tick {i}",
        )
        for i in range(3)
    ]
    await repo.create_many(events)

    page = await repo.list_events(limit=10)
    ordered = sorted(page.results, key=lambda row: row.message)
    assert [row.message for row in ordered] == ["tick 0", "tick 1", "tick 2"]
    assert [row.created_at for row in ordered] == [
        _T0,
        _T0 + timedelta(seconds=1),
        _T0 + timedelta(seconds=2),
    ]


async def test_create_many_is_a_noop_for_an_empty_sequence(session: AsyncSession) -> None:
    repo = SqlLogEventRepository(session)
    await repo.create_many([])
    assert (await session.execute(select(LogEvent))).scalars().all() == []


async def test_list_events_filters_by_level(session: AsyncSession) -> None:
    repo = SqlLogEventRepository(session)
    await repo.create(level="INFO", logger="a", message="info one")
    await repo.create(level="ERROR", logger="a", message="error one")
    await repo.create(level="ERROR", logger="a", message="error two")

    page = await repo.list_events(level="ERROR")
    assert page.total == 2
    assert {row.message for row in page.results} == {"error one", "error two"}


async def test_list_events_filters_by_logger(session: AsyncSession) -> None:
    repo = SqlLogEventRepository(session)
    await repo.create(level="INFO", logger="plex_manager.a", message="a1")
    await repo.create(level="INFO", logger="plex_manager.b", message="b1")

    page = await repo.list_events(logger="plex_manager.a")
    assert page.total == 1
    assert page.results[0].message == "a1"


async def test_list_events_filters_by_since_inclusive(session: AsyncSession) -> None:
    repo = SqlLogEventRepository(session)
    await repo.create(level="INFO", logger="a", message="old", created_at=_T0)
    await repo.create(
        level="INFO", logger="a", message="boundary", created_at=_T0 + timedelta(hours=1)
    )
    await repo.create(level="INFO", logger="a", message="new", created_at=_T0 + timedelta(hours=2))

    page = await repo.list_events(since=_T0 + timedelta(hours=1))
    assert page.total == 2
    assert {row.message for row in page.results} == {"boundary", "new"}


async def test_list_events_filters_by_correlation_id_across_known_keys(
    session: AsyncSession,
) -> None:
    repo = SqlLogEventRepository(session)
    await repo.create(
        level="ERROR",
        logger="a",
        message="by request_id",
        context={"request_id": 42},
    )
    await repo.create(
        level="ERROR",
        logger="a",
        message="by download_id",
        context={"download_id": 42},
    )
    await repo.create(
        level="ERROR",
        logger="a",
        message="by tmdb_id",
        context={"tmdb_id": 603},
    )
    await repo.create(level="ERROR", logger="a", message="unrelated", context={"tmdb_id": 1})
    await repo.create(level="ERROR", logger="a", message="no context")

    page = await repo.list_events(correlation_id="42")
    assert page.total == 2
    assert {row.message for row in page.results} == {"by request_id", "by download_id"}

    page_tmdb = await repo.list_events(correlation_id="603")
    assert page_tmdb.total == 1
    assert page_tmdb.results[0].message == "by tmdb_id"


async def test_correlation_filter_matches_a_future_string_valued_key(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A string-valued correlation key (e.g. a future ``info_hash``) must still match.

    Guards the SQLite quoting fix in ``_correlation_filter``: the bare JSON
    ``->`` accessor CAST to a string would JSON_QUOTE-wrap a string value
    (``'"abc123"'``), silently never matching a bare ``correlation_id``. Adds a
    hypothetical string key via monkeypatch rather than widening
    ``LOG_EVENT_CORRELATION_KEYS`` itself -- today's three keys are all
    integers.
    """
    monkeypatch.setattr(
        log_events_module, "LOG_EVENT_CORRELATION_KEYS", ("request_id", "info_hash")
    )
    repo = SqlLogEventRepository(session)
    await repo.create(
        level="ERROR", logger="a", message="by info_hash", context={"info_hash": "abc123"}
    )
    await repo.create(
        level="ERROR", logger="a", message="unrelated", context={"info_hash": "def456"}
    )

    page = await repo.list_events(correlation_id="abc123")
    assert page.total == 1
    assert page.results[0].message == "by info_hash"


async def test_list_events_orders_newest_first_and_paginates(session: AsyncSession) -> None:
    repo = SqlLogEventRepository(session)
    for i in range(5):
        await repo.create(
            level="INFO", logger="a", message=f"m{i}", created_at=_T0 + timedelta(seconds=i)
        )

    page = await repo.list_events(limit=2, offset=0)
    assert page.total == 5
    assert [row.message for row in page.results] == ["m4", "m3"]

    page2 = await repo.list_events(limit=2, offset=2)
    assert [row.message for row in page2.results] == ["m2", "m1"]


async def test_prune_older_than_deletes_only_stale_rows_and_returns_count(
    session: AsyncSession,
) -> None:
    repo = SqlLogEventRepository(session)
    await repo.create(level="INFO", logger="a", message="ancient", created_at=_T0)
    await repo.create(
        level="INFO", logger="a", message="recent", created_at=_T0 + timedelta(days=10)
    )

    removed = await repo.prune_older_than(_T0 + timedelta(days=1))
    assert removed == 1

    remaining = (await session.execute(select(LogEvent))).scalars().all()
    assert [row.message for row in remaining] == ["recent"]


async def test_prune_older_than_is_a_noop_when_nothing_is_stale(session: AsyncSession) -> None:
    repo = SqlLogEventRepository(session)
    await repo.create(level="INFO", logger="a", message="recent", created_at=_T0)

    removed = await repo.prune_older_than(_T0 - timedelta(days=1))
    assert removed == 0
