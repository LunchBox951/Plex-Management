"""``_log_drain_loop`` (ADR-0012, Component 2) — the drain tick's own COMMIT
failure must be attributed to ``handler.dropped_count``, not just a
``create_many`` raise (which ``drain_once`` already counts on its own).

Mirrors ``test_eviction_loop.py``'s pattern: the private loop is driven
directly against a bare ``FastAPI()`` (never the full app/lifespan), with
``asyncio.sleep`` monkeypatched to end the otherwise-infinite ``while True``
after exactly one iteration.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.services.log_capture_service import CapturedLogRecord, LogCaptureHandler
from plex_manager.web import app as app_module

SessionMaker = async_sessionmaker[AsyncSession]


class _StopLoop(Exception):
    """Sentinel raised from the patched ``asyncio.sleep`` to end the (real)
    ``while True`` in ``_log_drain_loop`` after one iteration, without ever
    needing the loop to actually sleep in real time."""


def _app(sessionmaker_: SessionMaker, handler: LogCaptureHandler) -> FastAPI:
    app = FastAPI()
    app.state.sessionmaker = sessionmaker_
    app.state.log_handler = handler
    return app


async def test_drain_commit_failure_counts_the_batch_as_dropped_exactly_once(
    sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6-B regression: ``drain_once`` only increments ``handler.dropped_count``
    when ``create_many`` itself raises. If ``create_many`` SUCCEEDS (the batch
    is already dequeued from the in-memory queue by then) but the drain's own
    ``session.commit()`` right after it then fails (a transient DB hiccup, a
    full disk), the whole transaction rolls back -- those INFO+ records are
    just as lost, yet the old code never touched ``dropped_count`` for this
    case, understating exactly what ``GET /ops/logs/tail`` reports to the
    operator. The commit failure must add the drained batch size to
    ``dropped_count`` exactly once (never double-counted with
    ``drain_once``'s own create_many-raise path, which never fires here since
    create_many succeeds)."""
    handler = LogCaptureHandler(loop=asyncio.get_running_loop())
    now = datetime.now(UTC)
    handler.queue.put_nowait(
        CapturedLogRecord(created_at=now, level="INFO", logger="x", message="one", context=None)
    )
    handler.queue.put_nowait(
        CapturedLogRecord(created_at=now, level="WARNING", logger="y", message="two", context=None)
    )
    assert handler.dropped_count == 0

    real_commit = AsyncSession.commit
    call_count = 0

    async def _flaky_commit(self: AsyncSession) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("db commit failed")
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", _flaky_commit)

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Stop the (otherwise infinite) loop after proving it survived exactly
        # this one drain-commit failure -- never by letting the injected
        # RuntimeError above escape unhandled.
        raise _StopLoop

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    app = _app(sessionmaker_, handler)
    with pytest.raises(_StopLoop):
        await app_module._log_drain_loop(app)  # pyright: ignore[reportPrivateUsage]

    # The batch was already dequeued (drain_once always pulls everything
    # currently queued before attempting the insert) and create_many itself
    # succeeded -- only the COMMIT failed -- so this exercises the
    # drain-commit path, distinct from the create_many-raise path
    # ``drain_once`` already counts on its own.
    assert handler.queue.empty()
    assert handler.dropped_count == 2
    assert len(sleep_calls) == 1
