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
   than the web-editable ``log_retention_days`` setting, AND (issue #152)
   beyond the web-editable ``log_max_rows`` row-count cap -- keeping the
   table's growth bounded even for a chatty install running a generous
   retention window, where the age cutoff alone would never trip.

:func:`configure_logging` wires ``config.log_level`` (previously defined but
never applied) to the root logger's effective level.

Never a secret-bearing pipeline: call sites are responsible for never logging a
credential (the existing discipline throughout ``adapters``/``services`` already
follows this — e.g. every adapter error message names a status code or exception
type, never a raw URL/token/password). That discipline is the FIRST line of
defense; as of issue #153 this module also applies a conservative,
key-name-driven redaction pass (:func:`~plex_manager.logsafe.redact_secrets`)
to every captured message as DEFENSE IN DEPTH — see that function's docstring
for the exact secret shapes covered. This closes the gap a purely
call-site-driven discipline cannot: a message assembled elsewhere (a
third-party library's own log line, a forgotten call site) that happens to
carry one of this app's real secret shapes.

As of issue #268, a THIRD, value-based pass (:func:`~plex_manager.logsafe.
redact_known_secrets`) runs FIRST, before the shape pass (see :func:`_capture`
for exactly why the order is load-bearing, not incidental): it masks any
VERBATIM occurrence of this app's own currently-configured secret VALUES
(fetched decrypted from the settings store — see :meth:`~plex_manager.web.
deps.SettingsStore.secret_values`), independent of the surrounding shape. This
catches renderings no shape grammar anticipates — notably a basic-auth URL
whose password itself contains ``@`` (issue #270) — without adding one more
denylist pattern. (Issue #270's OTHER gap, a cookie-jar/mapping ``repr()``
dump, is closed by a dedicated shape rule instead — see ``logsafe``'s
``_COOKIE_JAR_RE`` module comment for why that one credential can never reach
this value-based pass at all.) :class:`LogCaptureHandler`
carries the current secret-value set on :attr:`LogCaptureHandler.secret_values`,
refreshed each tick by ``web/app.py``'s ``_log_drain_loop`` (this module has no
DB access of its own, and ``emit`` runs synchronously off any thread, so the
value set is a periodically-refreshed snapshot, not a live read).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

from plex_manager.logsafe import MIN_SECRET_VALUE_LENGTH, redact_known_secrets, redact_secrets
from plex_manager.ports.repositories import LOG_EVENT_CORRELATION_KEYS, LogEventCreate

if TYPE_CHECKING:
    from plex_manager.ports.repositories import LogEventRepository

__all__ = [
    "AUTO_GRAB_TELEMETRY_LOGGER_NAME",
    "DECISION_TELEMETRY_LOGGER_NAME",
    "LOG_DRAIN_INTERVAL_SECONDS",
    "LOG_PRUNE_INTERVAL_SECONDS",
    "QUEUE_MAXSIZE",
    "RING_BUFFER_MAXLEN",
    "TELEMETRY_LOGGER_NAME",
    "TELEMETRY_LOG_RETENTION_DAYS",
    "CapturedLogRecord",
    "LogCaptureHandler",
    "configure_logging",
    "drain_once",
    "prune_once",
    "redact_log_context",
    "redact_log_message",
    "redact_retired_log_context",
    "redact_retired_log_message",
    "resolve_log_level",
    "stop_logging",
]


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

#: The retention-telemetry sweep's own logger name (``services.
#: retention_telemetry_service`` constructs its module logger from THIS
#: constant, not ``__name__`` — see that module's docstring), and its
#: dedicated retention window in days. Beta-week telemetry (ADR-0012 follow-up:
#: a DELETE-NOTHING periodic observer logging what a pressure sweep WOULD do)
#: is exactly the dataset the beta needs to survive the WHOLE week, not just
#: whatever ``log_retention_days`` the operator has set — the general default
#: is 7 days, which would otherwise prune day-1 telemetry before day 7 even
#: arrives. Rather than bumping the general retention (which would also retain
#: ordinary noisy INFO/WARNING/ERROR chatter far longer than needed, growing
#: ``log_events`` for no benefit), :func:`prune_once` gives rows from the
#: telemetry loggers (:data:`_TELEMETRY_LOGGERS` — this one plus the decision/
#: auto-grab emitters below) their own cutoff — using the existing
#: ``LogEvent.logger`` column as the marker, no schema change. The
#: retention-days constant is a FLOOR, not a fixed window:
#: :func:`prune_once` retains telemetry for ``max(TELEMETRY_LOG_RETENTION_DAYS,
#: log_retention_days)`` days, so it is never pruned earlier than 30 days AND
#: never earlier than the operator's own general retention (an operator who sets
#: ``log_retention_days`` to 60/90 keeps telemetry that long too — a fixed 30-day
#: cutoff would otherwise prune telemetry BEFORE their own ordinary logs, which
#: is never what raising retention means). 30 days is a generous margin over one
#: beta week; not (yet) a web-editable setting of its own for week 1 — the
#: "smallest honest change" the beta blueprint asks for, not a new knob nobody
#: has asked to tune yet.
TELEMETRY_LOGGER_NAME: Final = "plex_manager.services.retention_telemetry_service"
TELEMETRY_LOG_RETENTION_DAYS: Final = 30

#: The level every beta-telemetry logger is pinned to (in
#: :func:`configure_logging`), independent of the operator's ``config.log_level``.
#: Telemetry modules log their beta datasets at INFO; without this pin, a WARNING/
#: ERROR operator floor would filter those records at the ``_logger.info`` call
#: BEFORE the durable-log handler ever saw them (see :func:`configure_logging`).
_TELEMETRY_LOGGER_LEVEL: Final = logging.INFO

#: The other beta-week telemetry emitters, sharing the retention logger's exact
#: hazard: ``decision_service`` logs the issue-#24 multi-season-pack aggregate at
#: INFO, and ``auto_grab_service`` logs the issue-#43 records (the enriched
#: per-release source-failure WARNING and the per-cycle summary INFO with its
#: ``source_failures`` rollup) -- at an operator ``log_level`` of WARNING/ERROR
#: the INFO records would otherwise silently never reach ``log_events``. The
#: modules construct their telemetry logger FROM these constants (retention
#: precedent) so the emitter and the treatment below can never drift apart
#: under a rename. The SCOPE differs per module, deliberately:
#:
#: * ``decision_service`` uses its MODULE logger (the name equals the module's
#:   dotted path): the #24 aggregate is that module's ONLY log record, so
#:   module-logger scope IS telemetry-only scope -- a dedicated child would add
#:   a second name for zero records separated.
#: * ``auto_grab_service`` uses a dedicated ``.telemetry`` CHILD logger, because
#:   its module logger also carries operational records (search-failure
#:   cooldowns, "accepted release unusable", a park-race INFO). Scoping the
#:   telemetry treatment to the module logger would let those operational rows
#:   dodge the operator's ``log_retention_days`` and accumulate for 30 days on a
#:   failing install (the wave-6 finding). Only the #43 records go through the
#:   child; everything operational stays on the module logger with ordinary
#:   level/retention semantics. Child records propagate to the root handlers
#:   exactly like module records (levels gate only at the EMITTING logger), so
#:   the durable sink sees them unchanged.
DECISION_TELEMETRY_LOGGER_NAME: Final = "plex_manager.services.decision_service"
AUTO_GRAB_TELEMETRY_LOGGER_NAME: Final = "plex_manager.services.auto_grab_service.telemetry"

#: THE definition of "a telemetry logger" -- deliberately one tuple driving BOTH
#: telemetry behaviors, so they can never drift apart:
#:
#: 1. :func:`configure_logging` pins each name to :data:`_TELEMETRY_LOGGER_LEVEL`
#:    (INFO), so the dataset is CREATED at any operator ``log_level``; and
#: 2. :func:`prune_once` gives each name's rows the longer
#:    :data:`TELEMETRY_LOG_RETENTION_DAYS` floor, so the dataset SURVIVES the
#:    operator's ``log_retention_days`` (default 7) for the whole beta window.
#:
#: Half-treatment is exactly the bug class this tuple exists to prevent: a pinned
#: -but-not-retained logger emits data that the default prune deletes just as the
#: beta week completes; a retained-but-not-pinned logger keeps rows it never
#: creates under a WARNING floor. A logger belongs here iff its records ARE the
#: beta dataset.
_TELEMETRY_LOGGERS: Final = (
    TELEMETRY_LOGGER_NAME,
    DECISION_TELEMETRY_LOGGER_NAME,
    AUTO_GRAB_TELEMETRY_LOGGER_NAME,
)

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
#: logging with the same risk. Both are pinned to ``max(resolved_level, WARNING)``
#: (see :func:`configure_logging`), so WARNING is a hard FLOOR: no adapter call
#: site can opt back into leaking a URL by raising ``log_level`` to DEBUG for
#: troubleshooting -- "secrets are never logged" is a hard invariant, not a
#: verbosity trade-off -- while an operator who raises the floor to ERROR quiets
#: these loggers to ERROR too, never receiving WARNING chatter below their floor.
#: The pin only ever QUIETS relative to the operator level, never loosens it.
#: Every adapter's own
#: error messages already name a status code or exception type, never a raw
#: URL/token (see e.g. ``adapters/tmdb/adapter.py``); this only closes the gap
#: the underlying HTTP library itself opened around that discipline.
_THIRD_PARTY_LOGGERS_TO_QUIET: Final = ("httpx", "httpcore")
_THIRD_PARTY_LOGGER_LEVEL: Final = logging.WARNING

# A single shared Formatter instance purely to reuse its ``formatException`` —
# never used for full record formatting (this module builds its own message).
_EXC_FORMATTER: Final = logging.Formatter()

_logger = logging.getLogger(__name__)

#: Fallback effective level when ``config.log_level`` cannot be resolved to a
#: real stdlib level (see :func:`resolve_log_level`) — INFO, the same default
#: ``config.py`` ships, so an unrecognized override degrades to "as if unset"
#: rather than to something surprising.
_DEFAULT_LOG_LEVEL: Final = logging.INFO


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


def redact_log_message(message: str, secret_values: Iterable[str]) -> str:
    """Apply value-based redaction before the established shape grammar."""
    return redact_secrets(redact_known_secrets(message, secret_values))


def _redact_context_strings(
    context: dict[str, Any] | None, redact_string: Callable[[str], str]
) -> dict[str, Any] | None:
    """Apply ``redact_string`` to every string leaf and mapping key in JSON context."""
    if context is None:
        return None

    def redact(value: object) -> object:
        if isinstance(value, str):
            return redact_string(value)
        if isinstance(value, list):
            items = cast("list[object]", value)
            return [redact(item) for item in items]
        if isinstance(value, dict):
            mapping = cast("dict[object, object]", value)
            return {redact_string(str(key)): redact(item) for key, item in mapping.items()}
        return value

    return cast("dict[str, Any]", redact(context))


def redact_log_context(
    context: dict[str, Any] | None, secret_values: Iterable[str]
) -> dict[str, Any] | None:
    """Recursively redact every string leaf and mapping key in JSON context."""
    values = frozenset(secret_values)
    return _redact_context_strings(context, lambda value: redact_log_message(value, values))


def redact_retired_log_message(
    message: str,
    retired_values: Iterable[str],
    current_values: Iterable[str] = frozenset(),
) -> str:
    """Value-first redaction for a rotation rewrite's RETIRING values (ADR-0026).

    ``redact_known_secrets``'s 8-character floor exists so live reads against
    *currently configured* values never over-redact incidental short strings.
    A rotation rewrite is the opposite situation: it targets a SPECIFIC value
    that is leaving ``secret_values()`` forever, so the retiring values are
    masked exactly, with NO length floor (``min_length=1``). For a very short
    retiring value (1-3 characters) this deliberately masks every occurrence
    of that substring in history -- acceptable over-redaction for a retiring
    credential, versus leaving a real (if short) secret readable once the old
    value can no longer be redacted at read time.

    The retiring and current values are masked in ONE combined
    ``redact_known_secrets`` call, NOT sequential passes (codex #399 round 4):
    the two sets can overlap as substrings in either direction across a
    rotation (``abc`` -> ``abcdefghi``, or the shrink ``abcdefghi`` -> ``abc``),
    and whichever set a sequential first pass masked would chew a hole in the
    other's occurrence -- rotating ``abc`` -> ``abcdefghi`` with the floorless
    retired pass first turns a logged ``abcdefghi`` into ``***defghi``, and the
    full current secret no longer exists anywhere for the later floored pass
    (or the completion sweep) to find, stranding the suffix in the ring/queue.
    One combined alternation resolves every overlap longest-match-first by
    construction, so a value contained in a longer value from EITHER set can
    never partially mask the longer one. The single call is floorless
    (``min_length=1``) because the retiring values must be; the floor's
    false-positive guard is preserved for current values by pre-filtering them
    against :data:`~plex_manager.logsafe.MIN_SECRET_VALUE_LENGTH` -- exactly
    the values the default floor would have admitted. The standard floored
    pass + shape grammar still run last (via :func:`redact_log_message`),
    matching the established value-first ordering everywhere else.
    """
    retired = frozenset(retired_values)
    current = frozenset(current_values)
    floor_eligible_current = frozenset(
        value for value in current if len(value) >= MIN_SECRET_VALUE_LENGTH
    )
    # ``redact_log_message``'s own floored value pass finds nothing new after
    # the combined call above (every value it could match was already masked);
    # it is kept for the ``redact_secrets`` shape-grammar step it also runs,
    # preserving the established value-first-then-shape ordering.
    return redact_log_message(
        redact_known_secrets(message, retired | floor_eligible_current, min_length=1),
        retired | current,
    )


def redact_retired_log_context(
    context: dict[str, Any] | None,
    retired_values: Iterable[str],
    current_values: Iterable[str] = frozenset(),
) -> dict[str, Any] | None:
    """:func:`redact_retired_log_message` applied to every string leaf and key."""
    retired = frozenset(retired_values)
    current = frozenset(current_values)
    return _redact_context_strings(
        context, lambda value: redact_retired_log_message(value, retired, current)
    )


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


def _capture(
    record: logging.LogRecord,
    secret_values: frozenset[str] = frozenset(),
    retiring_values: frozenset[str] = frozenset(),
) -> CapturedLogRecord:
    """Render one stdlib ``LogRecord`` into the pipeline's own frozen shape.

    ``record.getMessage()`` (not the raw ``record.msg``) so ``%``-style args are
    already merged in, exactly like every other handler would render it. An
    attached exception (``logger.exception(...)`` / ``exc_info=True``) has its
    traceback appended — the LLM-diagnosis affordance (Component 2) needs the
    full trace, not just the one-line message, to actually explain a failure.

    The fully-rendered message (args merged, traceback appended) is then run
    through :func:`~plex_manager.logsafe.redact_known_secrets` (issue #268)
    against ``secret_values`` (the app's own currently-configured secret
    values — see :class:`LogCaptureHandler.secret_values`) and THEN through
    :func:`~plex_manager.logsafe.redact_secrets` (issue #153) — value-based
    pass FIRST, deliberately: the shape grammar's own masking can otherwise
    MANGLE a secret before the value-based pass ever sees it whole. A basic-
    auth password containing a raw ``@`` (issue #270) is the concrete case —
    ``redact_secrets``'s URL pass stops at the password's OWN first internal
    ``@`` and masks only that leading fragment, leaving the remainder as
    ordinary text; if that ran first, the full password would no longer
    appear as one contiguous substring anywhere in the message, and the
    value-based pass's exact-match search would find nothing to redact. Value-
    based first guarantees it always sees the secret's pristine, complete
    rendering at least once, regardless of what the shape pass would have done
    to it. AFTER merging (not before) so a secret split across a format string
    and its args (e.g. ``logger.info("token=%s", token)``) is still caught,
    since only the merged result actually contains ``token=<value>`` as one
    contiguous string. This is the ONE place every captured record passes
    through (both the ring-buffer live tail and the durable-store queue read
    from this same :class:`CapturedLogRecord`), so both redaction passes
    happen exactly once, upstream of both destinations.

    ``retiring_values`` (issue #389 facet 5) is non-empty ONLY while a secret
    rotation is in flight (published by :meth:`begin_secret_rotation`, cleared
    at :meth:`complete_secret_rotation`/:meth:`abort_secret_rotation`). When
    set, this record is masked with the EXACT, FLOORLESS retired-value pass
    (:func:`redact_retired_log_message`) instead of the standard floored value
    pass: the standard pass skips any value shorter than
    :func:`redact_known_secrets`' 8-character floor, so a short retiring secret
    would otherwise be captured VERBATIM during the rotation window and could
    land in the ring/queue AFTER the completion sweep has already run -- with the
    value already gone from ``secret_values()``, nothing would ever rewrite it.
    Masking floorlessly HERE, as the record is built (before it reaches the ring
    or the queue), closes that window by construction. ``secret_values`` (the
    widened transition set during rotation) is still applied as the floored
    ``current_values`` companion, so the ordering matches every other pass.
    """
    message = record.getMessage()
    if record.exc_info:
        message = f"{message}\n{_EXC_FORMATTER.formatException(record.exc_info)}"
    context = _extract_context(record)
    if retiring_values:
        message = redact_retired_log_message(message, retiring_values, secret_values)
        context = redact_retired_log_context(context, retiring_values, secret_values)
    else:
        message = redact_log_message(message, secret_values)
        context = redact_log_context(context, secret_values)
    return CapturedLogRecord(
        created_at=datetime.fromtimestamp(record.created, tz=UTC),
        level=record.levelname,
        logger=record.name,
        message=message,
        context=context,
    )


@dataclass(frozen=True)
class _RedactionSnapshot:
    """The (secret_values, retiring_values) pair as ONE immutable object.

    :class:`LogCaptureHandler` publishes every redaction-state transition by
    assigning a NEW instance to a single attribute (codex #399 round 4): a
    plain attribute assignment/read is atomic under the GIL, so
    :meth:`LogCaptureHandler.emit` -- which runs synchronously from ANY thread
    -- always observes the two sets as a coherent pair. Two separate
    attributes, however carefully write- and read-ordered, could still be
    straddled by a thread that read one attribute, lost the GIL across both
    writes of a transition, then read the other -- pairing an empty retiring
    set with an already-widened secret set and capturing a short retiring
    value verbatim down the ordinary floored path.
    """

    secret_values: frozenset[str] = frozenset()
    retiring_values: frozenset[str] = frozenset()


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

    ``secret_values`` (issue #268) is the app's own currently-configured secret
    VALUES (Plex token, Prowlarr/TMDB api keys, qBittorrent password —
    decrypted, in-process), fed to every :func:`_capture` call for the
    value-based redaction pass. This handler has no DB access of its own (it
    must never ``await`` from ``emit``, which can run off any thread — see
    above), so it cannot fetch these itself; ``web/app.py``'s
    ``_log_drain_loop`` — which already opens a DB session on its own interval
    — refreshes this attribute each tick. ``secret_values`` and
    ``retiring_values`` are properties over ONE immutable
    :class:`_RedactionSnapshot` published by a single attribute assignment
    (codex #399 round 4): a plain attribute read/write is atomic under the
    GIL, so ``emit`` always sees a COHERENT pair — never a stale
    ``retiring_values`` next to an already-widened ``secret_values``, the
    straddle that would send a short retiring secret down the ordinary floored
    path verbatim. Defaults to empty ``frozenset``\\ s — before the first
    refresh (a startup race of at most one drain interval) or with no secrets
    configured yet, this pass is simply a no-op, exactly as
    :func:`~plex_manager.logsafe.redact_known_secrets` defines for an empty
    value set.
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
        # The (secret_values, retiring_values) pair, as ONE immutable snapshot.
        # Every transition (drain-tick refresh, begin/abort/complete rotation)
        # publishes a NEW snapshot with a single attribute assignment, so the
        # lock-free read in :meth:`emit` -- which can run from ANY thread --
        # always observes a coherent pair (codex #399 round 4): two separate
        # attributes were individually atomic under the GIL, but a thread could
        # still pair the OLD empty retiring set with the NEW widened secret set
        # and mask a short retiring value with the floored pass alone.
        self._redaction = _RedactionSnapshot()
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        # Guards ``ring_buffer`` against the concurrent-iteration hazard above.
        # A ``threading.Lock`` (not ``asyncio.Lock``): ``emit`` can run from a
        # non-loop thread and must never ``await``; the critical section on
        # either side is a single, non-blocking, no-I/O list copy/append, so a
        # brief synchronous lock is never held long enough to matter, even when
        # acquired from the event-loop thread inside ``snapshot_tail``.
        self._lock = threading.Lock()

    @property
    def secret_values(self) -> frozenset[str]:
        """The current value-based redaction set (see the class docstring)."""
        return self._redaction.secret_values

    @secret_values.setter
    def secret_values(self, values: frozenset[str]) -> None:
        # The drain-tick refresh path. Publishes a whole new snapshot (single
        # attribute assignment -- see ``_redaction``); the retiring set is
        # carried over unchanged, though in practice it is always empty here:
        # the drain tick and every rotation serialize on the rotation lock.
        self._redaction = replace(self._redaction, secret_values=values)

    @property
    def retiring_values(self) -> frozenset[str]:
        """Values a rotation is retiring; non-empty only mid-rotation.

        Read-only by design: the pair only ever changes together, through
        :meth:`begin_secret_rotation` / :meth:`abort_secret_rotation` /
        :meth:`complete_secret_rotation`, so no caller can publish a straddling
        (retiring, secret) combination.
        """
        return self._redaction.retiring_values

    def begin_secret_rotation(
        self, secret_values: frozenset[str], *, retiring_values: frozenset[str] = frozenset()
    ) -> frozenset[str]:
        """Widen capture redaction temporarily and return the old snapshot.

        ``retiring_values`` (issue #389 facet 5) are published to the capture
        path so every record built WHILE the rotation is in flight masks them
        with the exact, floorless retired-value pass (see :func:`_capture`) --
        covering a retiring secret too short for the standard floored value
        pass, and covering it at capture time so a record that lands after the
        completion sweep is still clean.

        The widened set and the retiring set are published as ONE snapshot in a
        single attribute assignment (codex #399 round 4) -- an ordered pair of
        separate writes was still straddle-able by a worker-thread ``emit``
        that read the old empty retiring set, lost the GIL across BOTH writes,
        then read the widened secret set: the floored path would capture a
        short retiring value verbatim. With one snapshot, no emit can observe
        the widened set without also observing the floorless retiring set. A
        rotation is serialized by ``secret_rotation_lock``, so the retiring set
        is always empty on entry (no nesting)."""
        previous = self._redaction.secret_values
        self._redaction = _RedactionSnapshot(
            secret_values=previous | secret_values, retiring_values=retiring_values
        )
        return previous

    def abort_secret_rotation(self, previous_values: frozenset[str]) -> None:
        """Restore the exact capture snapshot after a failed transaction."""
        self._redaction = _RedactionSnapshot(secret_values=previous_values)

    def complete_secret_rotation(
        self,
        previous_values: frozenset[str],
        current_values: frozenset[str],
        *,
        retired_values: frozenset[str] = frozenset(),
    ) -> None:
        """Redact queued/ring records after commit, then retain current values only.

        ``retired_values`` (the values the rotation just removed) are masked
        with the exact, floorless pass -- see :func:`redact_retired_log_message`
        -- while the rest of the union keeps the standard floored treatment.
        """
        union = previous_values | current_values | retired_values
        queued: list[CapturedLogRecord] = []
        while True:
            try:
                record = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            queued.append(
                replace(
                    record,
                    message=redact_retired_log_message(record.message, retired_values, union),
                    context=redact_retired_log_context(record.context, retired_values, union),
                )
            )
        for record in queued:
            self.queue.put_nowait(record)
        with self._lock:
            self.ring_buffer = deque(
                (
                    replace(
                        record,
                        message=redact_retired_log_message(record.message, retired_values, union),
                        context=redact_retired_log_context(record.context, retired_values, union),
                    )
                    for record in self.ring_buffer
                ),
                maxlen=self.ring_buffer.maxlen,
            )
        # Narrow-and-clear as ONE snapshot publication (codex #399 round 4):
        # there is no transient window at all -- an emit sees either the full
        # mid-rotation pair (widened + retiring, floorless coverage) or the
        # final pair (narrowed + empty), never a mixed one.
        self._redaction = _RedactionSnapshot(secret_values=current_values)

    def emit(self, record: logging.LogRecord) -> None:
        # Never let a capture bug break logging itself (north star: honesty over
        # silence must never come at the cost of crashing the caller that logged
        # in the first place). ``handleError`` is the stdlib's own "a handler
        # failed" escape hatch (prints to stderr, respects ``logging.raiseExceptions``).
        try:
            # ONE atomic attribute read hands this emit a coherent
            # (secret_values, retiring_values) pair (codex #399 round 4) --
            # reading two separate attributes, in any order, could straddle a
            # rotation transition from another thread and mask a short retiring
            # value with the floored pass alone. See
            # :meth:`begin_secret_rotation` / :meth:`complete_secret_rotation`.
            snapshot = self._redaction
            captured = _capture(record, snapshot.secret_values, snapshot.retiring_values)
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

    def free_slots(self) -> int:
        """Best-effort count of currently-unused durable-queue slots
        (``queue.maxsize - queue.qsize()``, floored at 0).

        A CHEAP, non-blocking read (``asyncio.Queue.qsize`` is a plain ``len``
        under the GIL — no ``await``, no I/O) for a producer that wants to pace
        its own burst under the queue's LIVE headroom rather than a static
        assumption of an empty queue: e.g. the retention-telemetry sweep sizes
        its per-tick emission budget from this so it cannot, by itself, overrun
        the queue ON TOP OF the ambient INFO backlog already sitting in it
        (ordinary chatter plus a drain tick that has not run yet). INHERENTLY
        racy — the drain task and other threads' loggers mutate the queue
        between this read and the producer's later :meth:`emit` calls — so the
        result is an UPPER BOUND to keep a margin under, never an exact
        reservation. Any record that still loses that race is counted in
        :attr:`dropped_count` (see :meth:`_enqueue`), so residual loss stays
        VISIBLE, never silent.
        """
        return max(0, self.queue.maxsize - self.queue.qsize())

    def _enqueue(self, captured: CapturedLogRecord) -> None:
        """Runs ON THE LOOP THREAD (via ``call_soon_threadsafe``) — safe to touch
        the queue directly here."""
        try:
            self.queue.put_nowait(captured)
        except asyncio.QueueFull:
            # Drop the newest record rather than block or grow unbounded. A
            # sustained storm between drain ticks is the only way to hit this.
            self.dropped_count += 1


def resolve_log_level(level: str) -> int:
    """Normalize a configured level string to a valid stdlib numeric level.

    ``logging``'s level-name table is keyed by UPPERCASE names ('DEBUG',
    'INFO', ...), but ``config.log_level`` commonly arrives lowercase (e.g.
    ``PLEX_MANAGER_LOG_LEVEL=debug``) — an env var, not a Python literal, so
    nothing enforces case. Accepted, in order:

    1. Any case of a standard level name (``debug``, ``Info``, ``WARNING``, ...).
    2. An already-numeric level string (e.g. ``'10'``), passed straight to
       ``setLevel`` the same way an ``int`` argument would be.

    Anything else is UNRECOGNIZED and must never crash startup: raising here
    (the old behaviour, via ``logging.Logger.setLevel``'s own ``ValueError``)
    would abort the FastAPI lifespan before it can serve traffic over one bad
    config value. Honesty over silence means the bad value is surfaced (a
    warning, through this module's own logger) rather than either dying or
    quietly pretending the value was fine — it falls back to
    :data:`_DEFAULT_LOG_LEVEL`.

    Public (not module-private) so the console entry point
    (:func:`plex_manager.__main__.main`) can normalize ``config.log_level``
    into a real numeric level BEFORE handing it to ``uvicorn.run`` — see that
    module's docstring for why: ``uvicorn.Config`` looks a ``str`` level up in
    its OWN name table and raises ``KeyError`` on anything it doesn't
    recognize, which would crash the process before the FastAPI ``lifespan``
    (and this exact tolerant resolver, wired in via :func:`configure_logging`)
    ever runs. Reusing this one resolver for both call sites means "what counts
    as a valid log level" can never drift between the two -- an int level
    always passes ``uvicorn.Config`` straight through with no lookup at all,
    sidestepping its ``KeyError`` path entirely. ``__main__.main`` does NOT
    route every value through this function, though: see
    :func:`plex_manager.__main__._uvicorn_log_level` for the ``'trace'``
    carve-out that bypasses this resolver entirely for uvicorn's own launch
    argument.

    ``TRACE`` gets an explicit rule of its own, checked BEFORE the name-table
    lookup below: uvicorn defines a real, lower-than-DEBUG ``'trace'`` level
    for ASGI/protocol-level tracing (see ``uvicorn.config.LOG_LEVELS``), but
    stdlib ``logging`` has never heard of it -- ``logging.getLevelNamesMapping()``
    has no ``'TRACE'`` entry unless something in THIS process already called
    ``logging.addLevelName(5, "TRACE")`` (uvicorn's own ``Config.__init__``
    does exactly that as a side effect, but this function must not depend on
    whether some uvicorn ``Config`` happens to have been constructed already
    -- that is not an ordering this module can assume, and callers like
    :func:`configure_logging` are exercised directly in tests with no uvicorn
    involved at all). For THIS function's only consumer -- the app's own
    stdlib root-logger threshold, via :func:`configure_logging` -- DEBUG is
    the honest analogue: it is the next real stdlib level below INFO, which is
    what "more than normal" has to mean for a logger that has no level below
    it. This is deliberately separate from uvicorn's OWN effective level,
    where a distinct TRACE constant matters because ``uvicorn.Config`` only
    installs its ASGI ``MessageLoggerMiddleware`` when its OWN level is <= that
    constant -- ``_uvicorn_log_level`` passes ``'trace'`` to ``uvicorn.run``
    verbatim, untouched by this function, so uvicorn's TRACE-aware ``Config``
    does that lookup itself.
    """
    candidate = level.strip()
    if candidate.upper() == "TRACE":
        return logging.DEBUG
    by_name = logging.getLevelNamesMapping().get(candidate.upper())
    if by_name is not None:
        return by_name
    try:
        return int(candidate)
    except ValueError:
        pass
    _logger.warning(
        "invalid log_level %r (expected a standard level name or a numeric "
        "level); falling back to %s",
        level,
        logging.getLevelName(_DEFAULT_LOG_LEVEL),
    )
    return _DEFAULT_LOG_LEVEL


def configure_logging(level: str, *, logger: logging.Logger | None = None) -> LogCaptureHandler:
    """Attach a fresh :class:`LogCaptureHandler` to ``logger`` (default: the root
    logger) and set its effective level from ``level`` (``config.log_level``),
    normalized by :func:`resolve_log_level` so a case mismatch or unrecognized
    value never raises out of here (see that function's docstring).

    Also pins :data:`_THIRD_PARTY_LOGGERS_TO_QUIET` (``httpx``/``httpcore``) to
    ``max(resolved_level, _THIRD_PARTY_LOGGER_LEVEL)`` — see that constant's
    docstring for why WARNING is a hard secret-safety FLOOR, not merely reducing
    noise. The pin only ever QUIETS relative to the operator's own resolved
    ``level``, never loosens below it: a DEBUG/INFO floor still yields WARNING (so
    URL-bearing INFO records can never leak), while an ERROR floor yields ERROR on
    these loggers too rather than spilling WARNING chatter below the operator's
    chosen floor. This happens EVERY call (not just when
    ``logger`` is the root) since these loggers propagate up to the root either
    way; a caller passing a non-root ``logger`` (tests, mainly) still gets the
    same quieting so no test path silently relies on the child not having it.

    Symmetrically, pins every beta-telemetry logger
    (:data:`_TELEMETRY_LOGGERS`: the retention sweep, the decision
    multi-season aggregate, and the auto-grab per-cycle summary) to
    :data:`_TELEMETRY_LOGGER_LEVEL` (INFO) so their INFO records reach the durable
    ``log_events`` sink at ANY operator ``level``. The handler is attached to the
    ROOT logger, and each telemetry logger is a NON-propagation-broken descendant
    of root, so its records flow up to that handler on the normal
    ``propagate=True`` path — but ONLY if they are created in the first place.
    Left at the inherited effective level, an operator running at WARNING/ERROR
    would drop every INFO telemetry record at the ``_logger.info`` call site
    (``isEnabledFor`` short-circuits before any handler runs), and the beta
    datasets would silently never persist. Pinning THESE loggers to INFO (not the
    root, which would un-quiet ALL of the app's INFO chatter and spam the
    operator's floor) lets exactly the telemetry records through; the
    LogCaptureHandler itself has no level filter, so once created they reach its
    DB queue regardless of the root logger's own level.

    Returns the handler so the caller (``web/app.py``'s ``lifespan``) can store it
    (e.g. on ``app.state.log_handler``) for the drain task and the future log
    viewer / export endpoints to read from, and detach it again on shutdown via
    :func:`stop_logging`. Must be called from a running event loop (the handler
    captures it for thread-safe hand-off — see :class:`LogCaptureHandler`).
    """
    target = logger if logger is not None else logging.getLogger()
    handler = LogCaptureHandler()
    target.addHandler(handler)
    resolved = resolve_log_level(level)
    target.setLevel(resolved)
    # Pin the third-party HTTP client loggers to ``max(resolved, WARNING)``: the
    # pin only ever QUIETS relative to the operator's floor, never loosens below
    # it. WARNING is the secret-safety FLOOR (see _THIRD_PARTY_LOGGER_LEVEL) -- a
    # DEBUG/INFO operator level still yields WARNING here, so the URL-bearing INFO
    # records can never leak -- but an ERROR operator floor now yields ERROR on
    # httpx/httpcore too, honouring the operator's chosen floor instead of
    # spilling WARNING chatter below it.
    third_party_level = max(resolved, _THIRD_PARTY_LOGGER_LEVEL)
    for name in _THIRD_PARTY_LOGGERS_TO_QUIET:
        logging.getLogger(name).setLevel(third_party_level)
    for name in _TELEMETRY_LOGGERS:
        logging.getLogger(name).setLevel(_TELEMETRY_LOGGER_LEVEL)
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


async def prune_once(
    repo: LogEventRepository, retention_days: int, *, max_rows: int | None = None
) -> int:
    """Delete every stale ``log_events`` row -- TWO separate cutoffs, TWO deletes.

    Every logger EXCEPT the telemetry loggers (:data:`_TELEMETRY_LOGGERS`: the
    retention sweep, the decision multi-season aggregate, the auto-grab cycle
    summary) is pruned on the operator-editable ``retention_days`` (the
    web-editable ``log_retention_days`` setting, default 7) exactly as before.
    Rows FROM those loggers are pruned on their OWN, never-shorter cutoff:
    ``max(TELEMETRY_LOG_RETENTION_DAYS, retention_days)`` days. That ``max`` is
    the whole point of the carve-out —

    * an operator on the default (or any ``retention_days`` below 30) still keeps
      the beta-week telemetry a full 30 days, so a short general retention can
      never prune any of the datasets before the week is up (with only the
      original single-logger carve-out, the #24/#43 rows were deleted at the
      operator window — day-1 data gone just as the week completed); and
    * an operator who deliberately RAISES ``log_retention_days`` to 60/90 keeps
      telemetry AT LEAST as long as everything else — the carve-out is a floor,
      never a ceiling, so telemetry is never pruned EARLIER than the operator's
      own window (the bug a fixed 30-day cutoff would introduce above 30).

    Sharing :data:`_TELEMETRY_LOGGERS` with :func:`configure_logging`'s INFO pin
    is deliberate — see that tuple's docstring: pinned-but-not-retained (or the
    reverse) is the half-treatment bug class this coupling prevents. See
    :data:`TELEMETRY_LOGGER_NAME`'s docstring for the retention-window rationale.

    Then (issue #152) a THIRD, total, level/logger-agnostic delete: when
    ``max_rows`` is given (not ``None``), :meth:`~plex_manager.ports.
    repositories.LogEventRepository.prune_excess` deletes the OLDEST rows
    beyond that row-count cap — the age cutoffs above alone leave
    ``log_events`` unbounded in row COUNT for a chatty install running a
    generous ``retention_days``, since age-based pruning never trips until a
    row is actually stale. ``max_rows=None`` (the default) skips this pass
    entirely — a caller that only wants the age-based sweep (most existing
    tests, and any future caller with no row-cap policy to hand it) sees
    unchanged behaviour.

    Returns the total rows removed across all deletes. ``retention_days <= 0``
    is treated as "keep nothing older than now" rather than skipped for the
    ordinary cutoff — an operator who deliberately sets it to 0 gets the honest
    behaviour, not a silently ignored setting; the telemetry cutoff still holds
    its 30-day floor either way (``max(30, 0) == 30``).
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)
    pruned = await repo.prune_older_than(cutoff, loggers=_TELEMETRY_LOGGERS, exclude_loggers=True)
    telemetry_retention_days = max(TELEMETRY_LOG_RETENTION_DAYS, retention_days)
    telemetry_cutoff = now - timedelta(days=telemetry_retention_days)
    pruned += await repo.prune_older_than(
        telemetry_cutoff, loggers=_TELEMETRY_LOGGERS, exclude_loggers=False
    )
    if max_rows is not None:
        pruned += await repo.prune_excess(max_rows)
    return pruned
