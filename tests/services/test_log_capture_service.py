"""The log capture pipeline (ADR-0012): the sync handler, the ring buffer +
queue split, context extraction, the drain task, and the retention sweep.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.ports.repositories import LogEventCreate, LogEventPage, LogEventRecord
from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.services.log_capture_service import (
    AUTO_GRAB_TELEMETRY_LOGGER_NAME,
    DECISION_TELEMETRY_LOGGER_NAME,
    TELEMETRY_LOG_RETENTION_DAYS,
    TELEMETRY_LOGGER_NAME,
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


async def test_snapshot_tail_survives_concurrent_emit_from_another_thread(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    """Regression: ``emit`` is documented to run from ANY thread (a sync log
    call issued from a thread-pool-executed adapter call, for instance).
    Before ``snapshot_tail``'s lock, a plain ``list(handler.ring_buffer)`` could
    raise ``RuntimeError: deque mutated during iteration`` if a concurrent
    ``emit`` appended while the copy was in flight -- turning ``GET
    /ops/logs/tail`` into a 500 instead of returning tail data. Hammers
    ``emit`` from a background thread while repeatedly snapshotting; the lock
    means neither side may ever observe (or raise from) a torn read."""
    test_logger.addHandler(handler)
    stop = threading.Event()
    errors: list[BaseException] = []

    def hammer() -> None:
        i = 0
        while not stop.is_set():
            test_logger.info("bg line %d", i)
            i += 1

    writer = threading.Thread(target=hammer, daemon=True)
    writer.start()
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                records = handler.snapshot_tail(200)
            except Exception as exc:  # pragma: no cover - the regression itself
                errors.append(exc)
                break
            assert len(records) <= 200
    finally:
        stop.set()
        writer.join(timeout=2)

    assert errors == []


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


@pytest.mark.parametrize(
    ("configured", "expected_level"),
    [
        ("debug", logging.DEBUG),
        ("info", logging.INFO),
        ("WARNING", logging.WARNING),
        ("10", logging.DEBUG),
    ],
)
async def test_configure_logging_normalizes_case_and_numeric_levels(
    test_logger: logging.Logger, configured: str, expected_level: int
) -> None:
    """R6-A regression: ``logging.Logger.setLevel`` only recognizes UPPERCASE
    names from its own level table (or a bare int) -- a lowercase
    ``PLEX_MANAGER_LOG_LEVEL=debug``/``=info`` used to raise ``ValueError``
    straight out of ``configure_logging``, aborting the FastAPI lifespan
    before it could serve traffic. Every case here -- lowercase, already
    uppercase, and an already-numeric level string -- must configure without
    raising and land on the expected effective level."""
    attached = configure_logging(configured, logger=test_logger)
    try:
        assert test_logger.level == expected_level
    finally:
        stop_logging(attached, logger=test_logger)


async def test_configure_logging_falls_back_to_info_on_an_unrecognized_level(
    test_logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    """An unrecognized ``log_level`` (typo, garbage env value) must never crash
    startup: it degrades to INFO with a warning surfaced through this module's
    own logger -- honesty over silence, never a silent guess and never a fatal
    ``ValueError`` out of the lifespan."""
    with caplog.at_level(logging.WARNING, logger="plex_manager.services.log_capture_service"):
        attached = configure_logging("not-a-real-level", logger=test_logger)
    try:
        assert test_logger.level == logging.INFO
        assert "not-a-real-level" in caplog.text
    finally:
        stop_logging(attached, logger=test_logger)


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


class _FailingRepo:
    """A :class:`~plex_manager.ports.repositories.LogEventRepository` whose
    ``create_many`` always raises -- simulates a DB failure mid-drain (a lock
    timeout, a connection drop). Every other method is unused by ``drain_once``
    and simply never implemented."""

    async def create(
        self,
        *,
        level: str,
        logger: str,
        message: str,
        created_at: datetime | None = None,
        context: dict[str, Any] | None = None,
    ) -> LogEventRecord:
        raise NotImplementedError

    async def create_many(self, events: Sequence[LogEventCreate]) -> None:
        raise RuntimeError("db unavailable")

    async def list_events(
        self,
        *,
        level: str | None = None,
        since: datetime | None = None,
        logger: str | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> LogEventPage:
        raise NotImplementedError

    async def prune_older_than(
        self,
        cutoff: datetime,
        *,
        loggers: Sequence[str] | None = None,
        exclude_loggers: bool = False,
    ) -> int:
        raise NotImplementedError


async def test_drain_failure_counts_the_whole_lost_batch_as_dropped(
    test_logger: logging.Logger,
) -> None:
    """Regression: before this fix, a drain-tick DB failure silently discarded
    the whole dequeued batch WITHOUT touching ``dropped_count`` -- ``GET
    /ops/logs/tail`` would report ``dropped_count=0`` even though records were
    genuinely lost. ``drain_once`` must both (a) still propagate the failure
    (the caller logs it and keeps looping) and (b) add the lost batch's size to
    ``handler.dropped_count`` so that counter stays honest."""
    handler = LogCaptureHandler(loop=asyncio.get_running_loop())
    test_logger.addHandler(handler)
    test_logger.info("one -- will be lost")
    test_logger.warning("two -- will be lost")
    await asyncio.sleep(0)
    assert handler.queue.qsize() == 2
    assert handler.dropped_count == 0

    with pytest.raises(RuntimeError, match="db unavailable"):
        await drain_once(handler.queue, _FailingRepo(), handler=handler)

    # The batch was dequeued (drain_once always pulls everything currently
    # queued before attempting the insert) and is now gone -- but the failure
    # is still counted, not silently swallowed.
    assert handler.queue.empty()
    assert handler.dropped_count == 2


async def test_drain_failure_without_a_handler_still_propagates(
    sessionmaker_: SessionMaker,
) -> None:
    """``handler`` is optional (defaults to ``None``) -- a caller that only
    cares about the insert itself (no handler in scope) still sees the
    failure propagate, it just has nothing to increment."""
    queue: asyncio.Queue[CapturedLogRecord] = asyncio.Queue()
    queue.put_nowait(
        CapturedLogRecord(
            created_at=datetime.now(UTC), level="INFO", logger="x", message="one", context=None
        )
    )
    with pytest.raises(RuntimeError, match="db unavailable"):
        await drain_once(queue, _FailingRepo())


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


async def test_prune_once_spares_telemetry_rows_within_their_own_longer_retention(
    sessionmaker_: SessionMaker,
) -> None:
    """Beta-week telemetry (``services.retention_telemetry_service``) must
    survive the ordinary ``log_retention_days`` prune (default 7) -- rows from
    :data:`TELEMETRY_LOGGER_NAME` are pruned on their own, longer
    :data:`TELEMETRY_LOG_RETENTION_DAYS` cutoff instead, even when the
    operator-configured ``retention_days`` passed in here would otherwise have
    caught them."""
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        # 10 days old: past the ordinary 7-day retention_days, but well within
        # TELEMETRY_LOG_RETENTION_DAYS -- must survive.
        await repo.create(
            level="INFO",
            logger=TELEMETRY_LOGGER_NAME,
            message="telemetry, 10 days old",
            created_at=now - timedelta(days=10),
        )
        # An ordinary (non-telemetry) row of the SAME age must still be pruned
        # on the operator's own retention_days -- the carve-out is scoped to
        # the telemetry logger only, never a blanket retention bump.
        await repo.create(
            level="INFO",
            logger="plex_manager.services.reconciler",
            message="ordinary, 10 days old",
            created_at=now - timedelta(days=10),
        )
        await session.commit()

        removed = await prune_once(repo, retention_days=7)
        await session.commit()
        assert removed == 1  # only the ordinary row

        remaining = await repo.list_events(limit=10)
    assert [r.message for r in remaining.results] == ["telemetry, 10 days old"]


async def test_prune_once_spares_decision_and_auto_grab_telemetry_rows_too(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex P2 (carve-out gap): the decision multi-season aggregate (#24) and the
    auto-grab cycle summary (#43) are INFO-pinned into ``log_events`` but were NOT
    in the long-retention carve-out -- with the default ``log_retention_days=7``
    their day-1 rows were deleted just as the beta week completed. The carve-out
    now iterates the same ``_TELEMETRY_LOGGERS`` tuple the INFO pin uses (one
    definition of "telemetry logger", both behaviors), so a 20-day-old row from
    EITHER logger survives a 7-day operator window while an equally old ordinary
    row is still pruned.

    Wave-6 P2 refinement: the auto-grab member is the dedicated ``.telemetry``
    CHILD, not the module logger -- an OPERATIONAL auto-grab row (search-failure
    cooldowns, "accepted release unusable", logged on the module logger) obeys
    the operator's window like any ordinary row, so a failing install's warning
    stream can never dodge ``log_retention_days`` for 30 days."""
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        await repo.create(
            level="INFO",
            logger=DECISION_TELEMETRY_LOGGER_NAME,
            message="decision telemetry, 20 days old",
            created_at=now - timedelta(days=20),
        )
        await repo.create(
            level="INFO",
            logger=AUTO_GRAB_TELEMETRY_LOGGER_NAME,
            message="auto-grab telemetry, 20 days old",
            created_at=now - timedelta(days=20),
        )
        await repo.create(
            level="INFO",
            logger="plex_manager.services.reconciler",
            message="ordinary, 20 days old",
            created_at=now - timedelta(days=20),
        )
        # An operational auto-grab record rides the MODULE logger -- outside the
        # carve-out by design (wave-6), so the operator window prunes it.
        await repo.create(
            level="WARNING",
            logger="plex_manager.services.auto_grab_service",
            message="operational auto-grab warning, 20 days old",
            created_at=now - timedelta(days=20),
        )
        await session.commit()

        removed = await prune_once(repo, retention_days=7)
        await session.commit()
        assert removed == 2  # the ordinary row AND the operational auto-grab row

        remaining = await repo.list_events(limit=10)
    assert sorted(r.message for r in remaining.results) == [
        "auto-grab telemetry, 20 days old",
        "decision telemetry, 20 days old",
    ]


async def test_prune_once_thirty_day_cap_applies_to_decision_and_auto_grab_rows(
    sessionmaker_: SessionMaker,
) -> None:
    """The new carve-out members share the 30-day FLOOR, not permanent retention:
    40-day-old decision/auto-grab telemetry rows are past
    ``TELEMETRY_LOG_RETENTION_DAYS`` and are pruned on that cutoff even with a
    tiny operator window."""
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        await repo.create(
            level="INFO",
            logger=DECISION_TELEMETRY_LOGGER_NAME,
            message="decision telemetry, 40 days old",
            created_at=now - timedelta(days=40),
        )
        await repo.create(
            level="INFO",
            logger=AUTO_GRAB_TELEMETRY_LOGGER_NAME,
            message="auto-grab telemetry, 40 days old",
            created_at=now - timedelta(days=40),
        )
        await session.commit()

        removed = await prune_once(repo, retention_days=1)
        await session.commit()
        assert removed == 2

        remaining = await repo.list_events(limit=10)
    assert remaining.results == []


async def test_prune_once_eventually_prunes_telemetry_rows_past_their_own_retention(
    sessionmaker_: SessionMaker,
) -> None:
    """The telemetry carve-out is a longer window, not permanent retention --
    once a telemetry row is older than TELEMETRY_LOG_RETENTION_DAYS, it is
    pruned too, on its own cutoff (independent of the passed-in retention_days)."""
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        await repo.create(
            level="INFO",
            logger=TELEMETRY_LOGGER_NAME,
            message="telemetry, ancient",
            created_at=now - timedelta(days=TELEMETRY_LOG_RETENTION_DAYS + 1),
        )
        await session.commit()

        # A tiny operator-configured retention_days -- proves the telemetry
        # cutoff used is TELEMETRY_LOG_RETENTION_DAYS, not this value.
        removed = await prune_once(repo, retention_days=1)
        await session.commit()
        assert removed == 1

        remaining = await repo.list_events(limit=10)
    assert remaining.results == []


async def test_prune_once_telemetry_retention_never_shorter_than_the_operator_window(
    sessionmaker_: SessionMaker,
) -> None:
    """The other direction of the ``max`` floor: an operator who RAISES
    ``log_retention_days`` above the 30-day telemetry default must keep telemetry
    AT LEAST as long as everything else -- never pruned EARLIER than their own
    window. With ``retention_days=90``, a 45-day-old telemetry row (past the fixed
    30-day default, but well within the operator's 90) must SURVIVE (a fixed
    30-day cutoff would have wrongly pruned it), while a 100-day-old one is past
    even the 90-day window and is pruned. Regression for the fixed-30-below-
    operator-window finding."""
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        # 45 days old: past the 30-day telemetry default but within the operator's
        # raised 90-day window -- must survive (the bug pruned it at a fixed 30).
        await repo.create(
            level="INFO",
            logger=TELEMETRY_LOGGER_NAME,
            message="telemetry, 45 days old",
            created_at=now - timedelta(days=45),
        )
        # 100 days old: past even the raised 90-day window -- still pruned.
        await repo.create(
            level="INFO",
            logger=TELEMETRY_LOGGER_NAME,
            message="telemetry, 100 days old",
            created_at=now - timedelta(days=100),
        )
        await session.commit()

        removed = await prune_once(repo, retention_days=90)
        await session.commit()
        assert removed == 1  # only the 100-day row, not the 45-day one

        remaining = await repo.list_events(limit=10)
    assert [r.message for r in remaining.results] == ["telemetry, 45 days old"]
