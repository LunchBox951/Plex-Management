"""The durable, LLM-diagnosable log capture pipeline (ADR-0012, Component 2).

Today there is no logging config at all: logs only reach ``docker logs``,
violating the "never a terminal" north star. This module closes that gap with
three pieces:

1. :class:`LogCaptureHandler` — a SYNCHRONOUS ``logging.Handler`` attached to the
   root logger. It pushes EVERY record into an in-memory ring buffer (bounded
   ``deque``, the live all-levels tail — sync-safe, lost on restart, which is
   fine: it is a live view, not the durable store) and, for INFO-and-above only,
   hands the record to an ``asyncio.Queue`` for the drain task. The handler NEVER
   talks to the database itself and NEVER blocks: handing a record to the queue
   goes through ``loop.call_soon_threadsafe`` (safe from ANY thread, not just the
   loop's own) and a full queue drops the newest record rather than blocking —
   see :meth:`LogCaptureHandler.emit`.
2. A background **drain task** (:func:`drain_once`, looped by the web layer
   exactly like the existing reconcile loop) batch-inserts queued records into
   the ``log_events`` table via :class:`~plex_manager.ports.repositories.
   LogEventRepository`. A DB failure here is caught and logged, never left to
   kill the loop or the app — see the module's own logger usage in that path
   (deliberately NOT re-entering this module's own handler in a way that could
   recurse: the drain task logs through the SAME root logger, but a DB failure
   during drain does not re-attempt a synchronous DB write from inside emit()).
   The whole lost batch is also added to :attr:`LogCaptureHandler.dropped_count`
   (via ``drain_once``'s ``handler`` argument) so that counter stays honest
   about EVERY INFO+ record that missed durable storage, not just a full queue.
3. A **retention sweep** (:func:`prune_once`) deletes ``log_events`` rows older
   than the web-editable ``log_retention_days`` setting, keeping the table's
   growth bounded.

:func:`configure_logging` wires ``config.log_level`` (previously defined but
never applied) to the root logger's effective level.

Never a secret-bearing pipeline: call sites are responsible for never logging a
credential (the existing discipline throughout ``adapters``/``services`` already
follows this — e.g. every adapter error message names a status code or exception
type, never a raw URL/token/password). This module only carries whatever a call
site already chose to log; it does not (and cannot) redact after the fact.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from plex_manager.ports.repositories import LOG_EVENT_CORRELATION_KEYS, LogEventCreate

if TYPE_CHECKING:
    from plex_manager.ports.repositories import LogEventRepository

__all__ = [
    "LOG_DRAIN_INTERVAL_SECONDS",
    "LOG_PRUNE_INTERVAL_SECONDS",
    "QUEUE_MAXSIZE",
    "RING_BUFFER_MAXLEN",
    "CapturedLogRecord",
    "LogCaptureHandler",
    "configure_logging",
    "drain_once",
    "prune_once",
    "stop_logging",
]

_logger = logging.getLogger(__name__)

#: Live all-levels tail size (``GET /ops/logs/tail``). Bounded so a busy install
#: can never grow this without limit; the oldest entries fall off as new ones
#: arrive (``deque(maxlen=...)``).
RING_BUFFER_MAXLEN: Final = 2000

#: Bound on the INFO+ backlog awaiting the drain task. Sized generously relative
#: to the drain interval below so an ordinary burst never drops anything; a
#: sustained storm drops the NEWEST record (see ``emit``) rather than blocking.
QUEUE_MAXSIZE: Final = 2000

#: How often the drain task empties the queue into ``log_events``. An internal
#: pipeline constant (not a web-editable setting, unlike ``log_retention_days``)
#: — short enough that the log viewer stays close to live.
LOG_DRAIN_INTERVAL_SECONDS: Final = 2.0

#: How often the retention sweep runs, in DRAIN TICKS worth of wall time — a
#: ``DELETE ... WHERE created_at < cutoff`` is a single indexed range delete, so
#: this need not run anywhere near as often as the drain itself.
LOG_PRUNE_INTERVAL_SECONDS: Final = 300.0

#: Third-party HTTP client loggers that MUST be kept quieter than whatever the
#: operator sets ``config.log_level`` to. ``httpx`` logs ``"HTTP Request: %s %s
#: ..."`` at INFO — the SECOND ``%s`` is the request's full URL — and every
#: adapter in this codebase (TMDB/Prowlarr/qBittorrent/Plex) is an ``httpx``
#: client. The TMDB adapter in particular sends its API key as a URL query
#: parameter (never a header), so at the DEFAULT ``log_level=INFO`` every TMDB
#: call — including the health probe and every reconcile-cycle metadata call —
#: would otherwise write ``GET https://api.themoviedb.org/...?api_key=<secret>``
#: verbatim into the durable, EXPORTABLE ``log_events`` store (the store the
#: blueprint designs to be pasted straight into an LLM). ``httpcore`` (the
#: transport httpx is built on) has its own noisy INFO/DEBUG connection-pool
#: logging with the same risk. Both are pinned to WARNING regardless of the
#: configured root level: no adapter call site can opt back into leaking a URL
#: by raising ``log_level`` to DEBUG for troubleshooting -- "secrets are never
#: logged" is a hard invariant, not a verbosity trade-off. Every adapter's own
#: error messages already name a status code or exception type, never a raw
#: URL/token (see e.g. ``adapters/tmdb/adapter.py``); this only closes the gap
#: the underlying HTTP library itself opened around that discipline.
_THIRD_PARTY_LOGGERS_TO_QUIET: Final = ("httpx", "httpcore")
_THIRD_PARTY_LOGGER_LEVEL: Final = logging.WARNING

# A single shared Formatter instance purely to reuse its ``formatException`` —
# never used for full record formatting (this module builds its own message).
_EXC_FORMATTER: Final = logging.Formatter()


@dataclass(frozen=True)
class CapturedLogRecord:
    """One log record as the capture pipeline carries it — the ring buffer's and
    the queue's shared unit, and the direct source of a :class:`~plex_manager.
    ports.repositories.LogEventCreate` batch-insert row."""

    created_at: datetime
    level: str
    logger: str
    message: str
    context: dict[str, Any] | None


def _extract_context(record: logging.LogRecord) -> dict[str, Any] | None:
    """Pull any of :data:`LOG_EVENT_CORRELATION_KEYS` off ``record`` as ``extra``.

    ``logging``'s ``extra={...}`` kwarg sets each key as a plain ATTRIBUTE on the
    ``LogRecord`` (not nested under some "context" field), so a call site simply
    does ``logger.warning("...", extra={"tmdb_id": tmdb_id})`` and this picks it
    up generically — no per-call-site coupling to this module. Returns ``None``
    (not an empty dict) when nothing matched, so ``LogEventCreate.context`` stays
    honestly absent rather than an empty-but-present ``{}``.
    """
    context: dict[str, Any] = {}
    for key in LOG_EVENT_CORRELATION_KEYS:
        if hasattr(record, key):
            context[key] = getattr(record, key)
    return context or None


def _capture(record: logging.LogRecord) -> CapturedLogRecord:
    """Render one stdlib ``LogRecord`` into the pipeline's own frozen shape.

    ``record.getMessage()`` (not the raw ``record.msg``) so ``%``-style args are
    already merged in, exactly like every other handler would render it. An
    attached exception (``logger.exception(...)`` / ``exc_info=True``) has its
    traceback appended — the LLM-diagnosis affordance (Component 2) needs the
    full trace, not just the one-line message, to actually explain a failure.
    """
    message = record.getMessage()
    if record.exc_info:
        message = f"{message}\n{_EXC_FORMATTER.formatException(record.exc_info)}"
    return CapturedLogRecord(
        created_at=datetime.fromtimestamp(record.created, tz=UTC),
        level=record.levelname,
        logger=record.name,
        message=message,
        context=_extract_context(record),
    )


class LogCaptureHandler(logging.Handler):
    """Attached to the root logger by :func:`configure_logging`.

    Constructed on the event loop's own thread (during app startup, inside
    ``lifespan``) so ``asyncio.get_running_loop()`` resolves to the app's real
    loop; ``emit`` may then be called from ANY thread (a sync log call from a
    thread-pool-executed adapter call, for instance) and always safely hands off
    via ``call_soon_threadsafe`` rather than touching the queue directly.

    ``dropped_count`` is incremented (never raised, never itself logged — that
    would risk a self-feeding loop under sustained pressure) whenever an INFO+
    record fails to reach durable storage: either the queue is full (here, in
    :meth:`_enqueue`) OR a drain tick's batch insert itself fails (an entire
    dequeued batch is discarded on a DB error — see :func:`drain_once`'s
    ``handler`` parameter). It is exposed for the health/log-viewer surfaces to
    report HONESTLY (never understating) how many INFO+ records did not make it
    to durable storage since startup — the docstring on ``GET /ops/logs/tail``
    promises exactly that, not merely "dropped for being full". The ring buffer
    (the live tail) is unaffected by either case — it is a separate,
    always-appended structure.

    ``ring_buffer`` itself is a plain ``deque`` (no lock) because a single
    ``append`` is an atomic C-level op under the GIL — safe from any thread.
    Reading a CONSISTENT snapshot for the live-tail endpoint is a different
    problem: iterating a deque (what ``list(...)`` does) while another thread
    appends to it can raise ``RuntimeError: deque mutated during iteration`` —
    a real risk here since ``emit`` is documented above to run from ANY thread.
    :meth:`snapshot_tail` is the only supported way to read it: it takes
    ``_lock`` around both the read (here) and the write (in :meth:`emit`), so a
    reader is never caught mid-iteration by a concurrent append.
    """

    def __init__(
        self,
        *,
        ring_buffer_maxlen: int = RING_BUFFER_MAXLEN,
        queue_maxsize: int = QUEUE_MAXSIZE,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        super().__init__()
        self.ring_buffer: deque[CapturedLogRecord] = deque(maxlen=ring_buffer_maxlen)
        self.queue: asyncio.Queue[CapturedLogRecord] = asyncio.Queue(maxsize=queue_maxsize)
        self.dropped_count = 0
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        # Guards ``ring_buffer`` against the concurrent-iteration hazard above.
        # A ``threading.Lock`` (not ``asyncio.Lock``): ``emit`` can run from a
        # non-loop thread and must never ``await``; the critical section on
        # either side is a single, non-blocking, no-I/O list copy/append, so a
        # brief synchronous lock is never held long enough to matter, even when
        # acquired from the event-loop thread inside ``snapshot_tail``.
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        # Never let a capture bug break logging itself (north star: honesty over
        # silence must never come at the cost of crashing the caller that logged
        # in the first place). ``handleError`` is the stdlib's own "a handler
        # failed" escape hatch (prints to stderr, respects ``logging.raiseExceptions``).
        try:
            captured = _capture(record)
        except Exception:
            self.handleError(record)
            return
        # See ``snapshot_tail`` for why this append is lock-guarded despite being
        # individually atomic: the hazard is a READER's iteration being caught
        # mid-mutation, not the append itself.
        with self._lock:
            self.ring_buffer.append(captured)
        if record.levelno < logging.INFO:
            return
        # The loop is closed (shutdown race) — the ring buffer already has it for
        # the live tail; durable persistence for this one record is lost, which
        # is acceptable during shutdown and must never raise from emit().
        with contextlib.suppress(RuntimeError):
            self._loop.call_soon_threadsafe(self._enqueue, captured)

    def snapshot_tail(self, limit: int) -> list[CapturedLogRecord]:
        """Thread-safe copy of the last ``limit`` ring-buffer records, OLDEST
        first (mirrors the deque's own insertion order — the caller, ``GET
        /ops/logs/tail``, reverses it to newest-first for display).

        The ONLY safe way to read ``ring_buffer`` from outside this class: a
        plain ``list(handler.ring_buffer)`` can raise ``RuntimeError: deque
        mutated during iteration`` if ``emit`` appends from another thread
        while the copy is in flight (see the class docstring). Held only for a
        single, fast, in-memory copy — never awaits, never does I/O.
        """
        with self._lock:
            return list(self.ring_buffer)[-limit:]

    def _enqueue(self, captured: CapturedLogRecord) -> None:
        """Runs ON THE LOOP THREAD (via ``call_soon_threadsafe``) — safe to touch
        the queue directly here."""
        try:
            self.queue.put_nowait(captured)
        except asyncio.QueueFull:
            # Drop the newest record rather than block or grow unbounded. A
            # sustained storm between drain ticks is the only way to hit this.
            self.dropped_count += 1


def configure_logging(level: str, *, logger: logging.Logger | None = None) -> LogCaptureHandler:
    """Attach a fresh :class:`LogCaptureHandler` to ``logger`` (default: the root
    logger) and set its effective level from ``level`` (``config.log_level``).

    Also pins :data:`_THIRD_PARTY_LOGGERS_TO_QUIET` (``httpx``/``httpcore``) to
    :data:`_THIRD_PARTY_LOGGER_LEVEL` — see that constant's docstring for why this
    is a hard secret-safety invariant, not merely reducing noise, and why it is
    unconditional regardless of ``level``. This happens EVERY call (not just when
    ``logger`` is the root) since these loggers propagate up to the root either
    way; a caller passing a non-root ``logger`` (tests, mainly) still gets the
    same quieting so no test path silently relies on the child not having it.

    Returns the handler so the caller (``web/app.py``'s ``lifespan``) can store it
    (e.g. on ``app.state.log_handler``) for the drain task and the future log
    viewer / export endpoints to read from, and detach it again on shutdown via
    :func:`stop_logging`. Must be called from a running event loop (the handler
    captures it for thread-safe hand-off — see :class:`LogCaptureHandler`).
    """
    target = logger if logger is not None else logging.getLogger()
    handler = LogCaptureHandler()
    target.addHandler(handler)
    target.setLevel(level)
    for name in _THIRD_PARTY_LOGGERS_TO_QUIET:
        logging.getLogger(name).setLevel(_THIRD_PARTY_LOGGER_LEVEL)
    return handler


def stop_logging(handler: LogCaptureHandler, *, logger: logging.Logger | None = None) -> None:
    """Detach ``handler`` from ``logger`` (default: the root logger) — lifespan teardown."""
    target = logger if logger is not None else logging.getLogger()
    target.removeHandler(handler)


async def drain_once(
    queue: asyncio.Queue[CapturedLogRecord],
    repo: LogEventRepository,
    *,
    handler: LogCaptureHandler | None = None,
) -> int:
    """Drain everything CURRENTLY queued (non-blocking) into one batch insert.

    Never awaits for more to arrive — a fixed, non-blocking drain of whatever is
    queued right now, called on :data:`LOG_DRAIN_INTERVAL_SECONDS` by the web
    layer's loop. Returns the number of records inserted (0 is a normal, common
    outcome on a quiet tick). A DB failure during the insert propagates to the
    caller, which is expected to catch it, log it, and keep looping — draining
    must never accumulate an unbounded backlog just because one tick's insert
    failed, nor may it crash the process.

    The whole dequeued batch is lost on a failed insert (never re-queued — the
    items are already off ``queue`` by then, and re-queueing risks an
    unbounded backlog behind a persistently-broken DB). ``handler``, when
    given, has its :attr:`LogCaptureHandler.dropped_count` incremented by the
    LOST batch's size before the exception is re-raised, so that counter stays
    truthful about EVERY INFO+ record that missed durable storage — not just
    the queue-full case :meth:`LogCaptureHandler._enqueue` already counts.
    Optional (defaults to ``None``) so a caller that only cares about the
    insert itself (e.g. a unit test) is unaffected.
    """
    batch: list[LogEventCreate] = []
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        batch.append(
            LogEventCreate(
                created_at=item.created_at,
                level=item.level,
                logger=item.logger,
                message=item.message,
                context=item.context,
            )
        )
    if not batch:
        return 0
    try:
        await repo.create_many(batch)
    except Exception:
        if handler is not None:
            handler.dropped_count += len(batch)
        raise
    return len(batch)


async def prune_once(repo: LogEventRepository, retention_days: int) -> int:
    """Delete every ``log_events`` row older than ``retention_days``.

    Returns the number of rows removed. ``retention_days <= 0`` is treated as
    "keep nothing older than now" rather than skipped — an operator who
    deliberately sets it to 0 gets the honest behaviour, not a silently ignored
    setting.
    """
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    return await repo.prune_older_than(cutoff)
