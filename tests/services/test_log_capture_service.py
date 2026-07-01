"""The log capture pipeline (ADR-0012): the sync handler, the ring buffer +
queue split, context extraction, the drain task, and the retention sweep.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.services.log_capture_service import (
    CapturedLogRecord,
    LogCaptureHandler,
    configure_logging,
    drain_once,
    prune_once,
    stop_logging,
)

SessionMaker = async_sessionmaker[AsyncSession]


@pytest.fixture
def test_logger() -> logging.Logger:
    """An isolated logger (not the root) so tests never leak handlers onto it."""
    logger = logging.getLogger(f"test.log_capture.{id(object())}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


@pytest.fixture
async def handler() -> LogCaptureHandler:
    return LogCaptureHandler(loop=asyncio.get_running_loop())


# --------------------------------------------------------------------------- #
# LogCaptureHandler: ring buffer (all levels) + queue (INFO+ only)
# --------------------------------------------------------------------------- #


async def test_ring_buffer_captures_every_level(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    test_logger.addHandler(handler)
    test_logger.debug("a debug line")
    test_logger.info("an info line")
    test_logger.error("an error line")
    # asyncio.Queue.put_nowait scheduled via call_soon_threadsafe needs the loop
    # to actually run once to process the callback.
    await asyncio.sleep(0)

    assert [r.message for r in handler.ring_buffer] == [
        "a debug line",
        "an info line",
        "an error line",
    ]


async def test_queue_only_receives_info_and_above(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    test_logger.addHandler(handler)
    test_logger.debug("debug -- never queued")
    test_logger.info("info -- queued")
    test_logger.warning("warning -- queued")
    await asyncio.sleep(0)

    queued = []
    while not handler.queue.empty():
        queued.append(handler.queue.get_nowait().message)
    assert queued == ["info -- queued", "warning -- queued"]


async def test_ring_buffer_is_bounded(test_logger: logging.Logger) -> None:
    small_handler = LogCaptureHandler(ring_buffer_maxlen=3, loop=asyncio.get_running_loop())
    test_logger.addHandler(small_handler)
    for i in range(5):
        test_logger.info("line %d", i)
    await asyncio.sleep(0)

    assert [r.message for r in small_handler.ring_buffer] == ["line 2", "line 3", "line 4"]


async def test_queue_drops_newest_when_full_and_counts_it(
    test_logger: logging.Logger,
) -> None:
    tiny_handler = LogCaptureHandler(queue_maxsize=1, loop=asyncio.get_running_loop())
    test_logger.addHandler(tiny_handler)
    test_logger.info("first -- fits")
    test_logger.info("second -- dropped")
    await asyncio.sleep(0)

    assert tiny_handler.queue.qsize() == 1
    assert tiny_handler.queue.get_nowait().message == "first -- fits"
    assert tiny_handler.dropped_count == 1
    # The ring buffer (the live tail) is unaffected by queue pressure.
    assert [r.message for r in tiny_handler.ring_buffer] == ["first -- fits", "second -- dropped"]


async def test_correlation_context_is_extracted_from_extra(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    test_logger.addHandler(handler)
    test_logger.warning(
        "grab failed", extra={"request_id": 7, "tmdb_id": 603, "irrelevant_key": "dropped"}
    )
    await asyncio.sleep(0)

    record = handler.ring_buffer[-1]
    assert record.context == {"request_id": 7, "tmdb_id": 603}


async def test_no_correlation_keys_present_is_none_not_empty_dict(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    test_logger.addHandler(handler)
    test_logger.info("plain message, no context")
    await asyncio.sleep(0)
    assert handler.ring_buffer[-1].context is None


async def test_exception_traceback_is_appended_to_the_message(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    test_logger.addHandler(handler)
    try:
        raise ValueError("boom")
    except ValueError:
        test_logger.exception("something failed")
    await asyncio.sleep(0)

    message = handler.ring_buffer[-1].message
    assert "something failed" in message
    assert "ValueError: boom" in message
    assert "Traceback" in message


async def test_emit_never_raises_when_capture_itself_is_broken(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    # A record whose args don't match its format string makes getMessage() raise
    # internally -- emit() must swallow it (via handleError), never propagate.
    test_logger.addHandler(handler)
    logging.raiseExceptions = False  # keep handleError's stderr print quiet in CI
    try:
        test_logger.info("missing %s placeholder value")
    finally:
        logging.raiseExceptions = True
    # No assertion needed beyond "this didn't raise" -- the test passing is the point.


# --------------------------------------------------------------------------- #
# configure_logging / stop_logging
# --------------------------------------------------------------------------- #


async def test_configure_logging_attaches_and_sets_level(test_logger: logging.Logger) -> None:
    attached = configure_logging("WARNING", logger=test_logger)
    try:
        assert attached in test_logger.handlers
        assert test_logger.level == logging.WARNING
    finally:
        stop_logging(attached, logger=test_logger)
    assert attached not in test_logger.handlers


async def test_configure_logging_quiets_httpx_and_httpcore_even_at_debug(
    test_logger: logging.Logger,
) -> None:
    # The secret-leak regression guard: httpx logs "HTTP Request: %s %s ..." at
    # INFO with the FULL request URL, and the TMDB adapter's api_key rides that
    # URL's query string -- so if the httpx/httpcore loggers ever inherited the
    # configured root level, every TMDB call would write the key straight into
    # the durable, exportable log_events store. This must hold even when an
    # operator sets log_level to DEBUG for troubleshooting -- never a verbosity
    # trade-off, always quieted.
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    saved_levels = (httpx_logger.level, httpcore_logger.level)
    try:
        attached = configure_logging("DEBUG", logger=test_logger)
        try:
            assert httpx_logger.getEffectiveLevel() >= logging.WARNING
            assert httpcore_logger.getEffectiveLevel() >= logging.WARNING
        finally:
            stop_logging(attached, logger=test_logger)
    finally:
        httpx_logger.setLevel(saved_levels[0])
        httpcore_logger.setLevel(saved_levels[1])


# --------------------------------------------------------------------------- #
# drain_once / prune_once
# --------------------------------------------------------------------------- #


async def test_drain_once_is_a_no_op_on_an_empty_queue(sessionmaker_: SessionMaker) -> None:
    queue: asyncio.Queue[CapturedLogRecord] = asyncio.Queue()
    async with sessionmaker_() as session:
        inserted = await drain_once(queue, SqlLogEventRepository(session))
        await session.commit()
    assert inserted == 0


async def test_drain_once_batch_inserts_every_queued_record(sessionmaker_: SessionMaker) -> None:
    queue: asyncio.Queue[CapturedLogRecord] = asyncio.Queue()
    now = datetime.now(UTC)
    queue.put_nowait(
        CapturedLogRecord(created_at=now, level="INFO", logger="x", message="one", context=None)
    )
    queue.put_nowait(
        CapturedLogRecord(
            created_at=now,
            level="WARNING",
            logger="y",
            message="two",
            context={"tmdb_id": 603},
        )
    )

    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        inserted = await drain_once(queue, repo)
        await session.commit()
        assert inserted == 2

        page = await repo.list_events(limit=10)
    assert page.total == 2
    assert queue.empty()


async def test_prune_once_removes_only_records_older_than_retention(
    sessionmaker_: SessionMaker,
) -> None:
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        await repo.create(
            level="INFO", logger="x", message="old", created_at=now - timedelta(days=10)
        )
        await repo.create(
            level="INFO", logger="x", message="recent", created_at=now - timedelta(days=1)
        )
        await session.commit()

        removed = await prune_once(repo, retention_days=7)
        await session.commit()
        assert removed == 1

        page = await repo.list_events(limit=10)
    assert [r.message for r in page.results] == ["recent"]
