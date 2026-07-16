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
    redact_log_context,
    redact_log_message,
    redact_retired_log_context,
    redact_retired_log_message,
    resolve_log_level,
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


def test_redact_log_context_recurses_keys_values_and_resolves_collisions() -> None:
    secret = "fake-context-secret-value"  # noqa: S105 -- fixture credential
    context = {
        secret: {"nested": [secret, 7, True, None]},
        "<redacted>": "last value wins",
    }

    redacted = redact_log_context(context, frozenset({secret}))

    assert redacted == {"<redacted>": "last value wins"}


def test_redact_log_context_handles_url_and_base64_representations() -> None:
    secret = "fake-context-secret"  # noqa: S105 -- fixture credential
    encoded = "ZmFrZS1jb250ZXh0LXNlY3JldA=="
    context = {
        f"https://example.invalid/?token={secret}": [
            f"https://{secret}@example.invalid/",
            encoded,
            4,
            False,
            None,
        ]
    }

    redacted = redact_log_context(context, frozenset({secret}))

    assert redacted is not None
    rendered = str(redacted)
    assert secret not in rendered
    assert encoded not in rendered
    assert next(iter(redacted.values()))[-3:] == [4, False, None]


def test_redact_log_message_uses_value_first_for_basic_auth_at_sign() -> None:
    secret = "fake@basic-auth-secret"  # noqa: S105 -- fixture credential
    message = f"https://user:{secret}@example.invalid/path"

    redacted = redact_log_message(message, frozenset({secret}))

    assert secret not in redacted
    assert "<redacted>" in redacted


def test_redact_retired_helpers_mask_short_values_the_read_floor_skips() -> None:
    """A retiring value below the 8-char read floor is masked exactly by the
    rotation-rewrite helpers, in messages and in every context key/leaf, while
    the CURRENT-value read path keeps its floor (over-redaction guard)."""
    short_retired = "abc12"
    message = f"bare {short_retired} occurrence"

    # The read path's floor intentionally skips the short value...
    assert redact_log_message(message, frozenset({short_retired})) == message
    # ...but the rotation rewrite must not.
    rewritten = redact_retired_log_message(message, frozenset({short_retired}))
    assert short_retired not in rewritten
    assert "<redacted>" in rewritten

    context = {short_retired: [f"nested {short_retired}", 7, None]}
    redacted = redact_retired_log_context(context, frozenset({short_retired}))
    assert redacted is not None
    rendered = str(redacted)
    assert short_retired not in rendered
    assert next(iter(redacted.values()))[-2:] == [7, None]


def test_redact_retired_message_still_applies_current_values_and_shape() -> None:
    """The retired pass composes with the standard current-value + shape pass."""
    retired = "xy9"
    current = "fake-current-long-secret"
    message = f"retired {retired} current {current} password=hunter2"

    redacted = redact_retired_log_message(message, frozenset({retired}), frozenset({current}))

    assert retired not in redacted
    assert current not in redacted
    assert "hunter2" not in redacted


async def test_handler_secret_rotation_success_and_abort_preserve_contract() -> None:
    old = "fake-old-handler-secret"
    current = "fake-current-handler-secret"
    handler = LogCaptureHandler(loop=asyncio.get_running_loop())
    handler.secret_values = frozenset({old})
    record = CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=old,
        context={old: [old]},
    )
    handler.queue.put_nowait(record)
    handler.ring_buffer.append(record)

    previous = handler.begin_secret_rotation(frozenset({current}))
    assert previous == frozenset({old})
    handler.abort_secret_rotation(previous)
    assert handler.queue.get_nowait() == record
    handler.queue.put_nowait(record)
    assert handler.snapshot_tail(1)[0] == record

    handler.complete_secret_rotation(previous, frozenset({current}))
    queued = handler.queue.get_nowait()
    ring = handler.snapshot_tail(1)[0]
    assert old not in queued.message
    assert old not in str(queued.context)
    assert old not in ring.message
    assert old not in str(ring.context)
    assert handler.secret_values == frozenset({current})


async def test_complete_secret_rotation_serializes_a_concurrent_thread_emit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "fake-concurrent-handler-value"
    current = "fake-current-handler-value"
    handler = LogCaptureHandler(loop=asyncio.get_running_loop())
    handler.secret_values = frozenset({old})
    handler.ring_buffer.append(
        CapturedLogRecord(
            created_at=datetime.now(UTC),
            level="DEBUG",
            logger="test",
            message=old,
            context={"request_id": old},
        )
    )
    previous = handler.begin_secret_rotation(frozenset({current}))
    rewrite_started = threading.Event()
    emit_captured = threading.Event()
    real_redact = redact_log_message

    def coordinated_redact(message: str, values: object) -> str:
        if threading.current_thread() is threading.main_thread() and not rewrite_started.is_set():
            rewrite_started.set()
            assert emit_captured.wait(timeout=1)
        redacted = real_redact(message, values)  # type: ignore[arg-type]
        if threading.current_thread() is not threading.main_thread():
            emit_captured.set()
        return redacted

    monkeypatch.setattr(
        "plex_manager.services.log_capture_service.redact_log_message",
        coordinated_redact,
    )

    def emit_during_completion() -> None:
        assert rewrite_started.wait(timeout=1)
        record = logging.LogRecord(
            name="test",
            level=logging.DEBUG,
            pathname=__file__,
            lineno=1,
            msg=old,
            args=(),
            exc_info=None,
        )
        record.request_id = old
        handler.emit(record)

    thread = threading.Thread(target=emit_during_completion)
    thread.start()
    handler.complete_secret_rotation(previous, frozenset({current}))
    thread.join(timeout=1)

    assert not thread.is_alive()
    rendered = str(handler.snapshot_tail(10))
    assert old not in rendered
    assert handler.secret_values == frozenset({current})


async def test_active_rotation_masks_a_short_retiring_value_at_capture_time() -> None:
    """Facet 5 (issue #389): while a rotation is in flight, a record built for a
    retiring value SHORTER than the read floor is masked AS IT IS CAPTURED --
    before it ever reaches the ring or the queue -- so a worker-thread log that
    lands after the completion sweep can never carry the bare old value. The
    standard floored ``secret_values`` pass would skip a short value; the
    floorless retired pass, keyed off the published ``retiring_values``, does
    not."""
    short_old = "tok5!"  # 5 chars -- below redact_known_secrets' 8-char floor
    new = "fake-new-long-secret-value"
    handler = LogCaptureHandler(loop=asyncio.get_running_loop())
    handler.secret_values = frozenset({short_old})

    def _emit_short_old() -> None:
        # A BARE occurrence (no ``key=value`` shape prefix), so only the value-
        # based pass can catch it -- isolating the floor behaviour from the shape
        # grammar, which would mask ``token=...`` regardless.
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="bare %s occurrence",
            args=(short_old,),
            exc_info=None,
        )
        record.request_id = short_old
        handler.emit(record)

    # CONTROL: with no rotation active, the floored value pass skips the short
    # value, so it survives into the ring verbatim -- the exact gap facet 5 closes.
    _emit_short_old()
    await asyncio.sleep(0)
    assert short_old in handler.snapshot_tail(1)[0].message
    handler.queue.get_nowait()  # drain the control record so the queue below is the rotation's

    # Enter the boundary: publish the retiring value and widen, exactly as
    # ``secret_rotation`` does on ``begin_secret_rotation``.
    previous = handler.begin_secret_rotation(
        frozenset({short_old, new}), retiring_values=frozenset({short_old})
    )
    assert previous == frozenset({short_old})
    assert handler.retiring_values == frozenset({short_old})

    # A worker logs the short old value DURING the rotation window.
    _emit_short_old()
    await asyncio.sleep(0)  # let the call_soon_threadsafe enqueue run

    tail = handler.snapshot_tail(1)[0]
    assert short_old not in tail.message
    assert short_old not in str(tail.context)
    queued = handler.queue.get_nowait()
    assert short_old not in queued.message
    assert short_old not in str(queued.context)

    # Completion clears the retiring state and resumes the floored contract.
    handler.complete_secret_rotation(
        previous, frozenset({new}), retired_values=frozenset({short_old})
    )
    assert handler.retiring_values == frozenset()
    assert handler.secret_values == frozenset({new})


async def test_abort_secret_rotation_clears_the_published_retiring_values() -> None:
    """A failed rotation withdraws the capture-time floorless pass, so a later
    record for the same short value is no longer over-masked once the rotation
    is abandoned (the value is still live and covered by its ordinary pass)."""
    short_old = "ab7!"
    handler = LogCaptureHandler(loop=asyncio.get_running_loop())
    handler.secret_values = frozenset({short_old})

    previous = handler.begin_secret_rotation(
        frozenset({short_old}), retiring_values=frozenset({short_old})
    )
    assert handler.retiring_values == frozenset({short_old})
    handler.abort_secret_rotation(previous)

    assert handler.retiring_values == frozenset()
    assert handler.secret_values == frozenset({short_old})


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


# --------------------------------------------------------------------------- #
# Capture-time redaction (issue #153): every secret shape this app's real
# adapters can produce must never survive into the ring buffer OR the durable
# queue -- both read from the SAME ``_capture``-built ``CapturedLogRecord``, so
# exercising the ring buffer (populated synchronously in ``emit``, no
# ``drain_once`` round trip needed) proves the durable path too. Every fixture
# secret is an obviously-fake literal (never a real credential) -- see
# ``test_logsafe.py``'s identical convention for why asserting ``secret not in
# ...`` is safe even though pytest would otherwise echo it on a failure.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("message_template", "secret"),
    [
        ("GET https://api.themoviedb.org/3/movie/603?api_key=%s&language=en-US", "FAKETMDBKEY123"),
        ("X-Api-Key: %s", "FAKEPROWLARRKEY99"),
        ("X-Plex-Token: %s", "FAKEPLEXTOKEN7890"),
        ("qBittorrent login data={'username': 'admin', 'password': '%s'}", "FAKEQBTPASSWORD1"),
        ("Authorization: Bearer %s", "FAKEBEARERTOKEN123"),
    ],
)
async def test_capture_redacts_every_known_secret_shape(
    test_logger: logging.Logger,
    handler: LogCaptureHandler,
    message_template: str,
    secret: str,
) -> None:
    test_logger.addHandler(handler)
    test_logger.info(message_template, secret)
    await asyncio.sleep(0)

    # The ring buffer (the live ``/ops/logs/tail`` view) never carries it.
    tail_message = handler.ring_buffer[-1].message
    assert secret not in tail_message
    assert "<redacted" in tail_message

    # Neither does the durable-store queue (what the drain task batch-inserts
    # into ``log_events``) -- same ``CapturedLogRecord``, read independently to
    # prove BOTH destinations, not just whichever happens to be checked first.
    queued_message = handler.queue.get_nowait().message
    assert secret not in queued_message
    assert "<redacted" in queued_message


async def test_capture_leaves_an_ordinary_message_unmangled(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    """The redaction pass must not have false positives on ordinary operational
    logging -- a plain, secret-free message round-trips byte-identical."""
    test_logger.addHandler(handler)
    test_logger.info("reconcile cycle completed: %d requests processed", 12)
    await asyncio.sleep(0)

    assert handler.ring_buffer[-1].message == "reconcile cycle completed: 12 requests processed"


async def test_capture_redacts_a_secret_split_across_format_args(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    """Redaction runs AFTER ``%``-arg merging (on ``record.getMessage()``'s
    result), not on the raw format string -- a secret that only becomes part
    of a contiguous ``key=value`` shape once its arg is substituted in must
    still be caught."""
    test_logger.addHandler(handler)
    test_logger.info("qbittorrent auth: token=%s", "FAKESPLITTOKEN456")
    await asyncio.sleep(0)

    message = handler.ring_buffer[-1].message
    assert "FAKESPLITTOKEN456" not in message
    assert "token=<redacted>" in message


# --------------------------------------------------------------------------- #
# Capture-time VALUE-based redaction (issue #268): LogCaptureHandler.secret_values
# is the synchronous snapshot _capture masks against, refreshed by
# web/app.py's _log_drain_loop -- exercised directly here via the handler
# attribute rather than a real settings-store round trip (which lives in the
# web-layer integration tests). See test_logsafe.py for the pure-function unit
# tests of redact_known_secrets itself.
# --------------------------------------------------------------------------- #
async def test_capture_masks_a_configured_secret_value_regardless_of_shape(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    """A secret VALUE the handler was handed is masked wherever it appears --
    no key name, no recognizable shape needed, unlike the #153 grammar above."""
    handler.secret_values = frozenset({"FAKEVALUEBASEDSECRET123"})
    test_logger.addHandler(handler)
    test_logger.info("third-party client said: FAKEVALUEBASEDSECRET123 was rejected")
    await asyncio.sleep(0)

    message = handler.ring_buffer[-1].message
    assert "FAKEVALUEBASEDSECRET123" not in message
    assert "<redacted>" in message


async def test_capture_value_based_pass_is_a_noop_with_no_configured_secrets(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    """The default (never-refreshed) ``secret_values`` is an empty frozenset --
    capture behaves exactly as it did before issue #268 for any install with
    no configured secrets yet (e.g. pre-setup)."""
    assert handler.secret_values == frozenset()
    test_logger.addHandler(handler)
    test_logger.info("nothing configured yet, mentions FAKEVALUEBASEDSECRET123 in prose")
    await asyncio.sleep(0)

    assert handler.ring_buffer[-1].message == (
        "nothing configured yet, mentions FAKEVALUEBASEDSECRET123 in prose"
    )


@pytest.mark.parametrize(
    ("message_template", "secret"),
    [
        # issue #270, gap 1: a cookie-jar/mapping repr dump -- NOT a raw
        # ``name=value`` cookie assignment, so the #153 shape grammar's
        # ``_COOKIE_RE`` never fires on it (see test_logsafe.py's
        # ``test_cookie_jar_mapping_repr_dump_is_not_caught_by_shape_grammar``
        # for the direct proof of that gap). The value-based pass has no such
        # blind spot.
        ("outgoing cookies: {'plexmgr.session': '%s'}", "sFAKESESSIONVALUE1234567890"),
        # issue #270, gap 2: a basic-auth URL whose password itself contains a
        # raw ``@`` -- the #153 shape grammar's URL pass stops at the
        # password's OWN first internal ``@`` and leaves the remainder
        # exposed (see test_logsafe.py's
        # ``test_basic_auth_password_with_raw_at_sign_leaks_past_shape_grammar``).
        (
            "connecting to https://tracker_user:%s@tracker.example.com/announce",
            "p@ssw0rd0123456789",
        ),
    ],
)
async def test_capture_masks_issue_270_shape_grammar_gaps_via_value_pass(
    test_logger: logging.Logger,
    handler: LogCaptureHandler,
    message_template: str,
    secret: str,
) -> None:
    """End-to-end regression proof (issue #270 folded into issue #268's test
    matrix): given the app's actual configured secret value, BOTH deferred
    shape-grammar gaps are still fully masked at the real capture call site --
    not just in the pure ``logsafe`` unit tests."""
    handler.secret_values = frozenset({secret})
    test_logger.addHandler(handler)
    test_logger.info(message_template, secret)
    await asyncio.sleep(0)

    message = handler.ring_buffer[-1].message
    assert secret not in message
    # No fragment of the raw secret should survive either (guards against a
    # PARTIAL match leaving a suffix/prefix exposed, exactly the #270 failure
    # mode the value-based pass exists to close).
    assert "ssw0rd0123456789" not in message
    assert "sFAKESESSIONVALUE1234567890" not in message


# --------------------------------------------------------------------------- #
# issue #294, finding 2: the startup setup-URL hint's companion
# ``_logger.info("Setup: %s", url)`` call (web/app.py's
# ``_emit_setup_ready_hint``) carries the raw ``#setup_token=`` fragment. This
# proves the value-based pass -- fed the setup token via issue #292 item 6 --
# masks it at the real capture call site, exactly like the issue #270 proofs
# above for the OTHER two settings-store-derived secrets.
# --------------------------------------------------------------------------- #
async def test_capture_masks_the_setup_token_in_the_printed_setup_url(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    handler.secret_values = frozenset({"fake-boot-setup-token-1234567890"})
    test_logger.addHandler(handler)
    test_logger.info(
        "Setup: %s", "http://localhost:8000/setup#setup_token=fake-boot-setup-token-1234567890"
    )
    await asyncio.sleep(0)

    message = handler.ring_buffer[-1].message
    assert "fake-boot-setup-token-1234567890" not in message
    assert "<redacted>" in message
    # The path up to the fragment survives -- still diagnosable which install
    # printed the hint, only the credential itself is gone.
    assert "http://localhost:8000/setup#setup_token=" in message


async def test_capture_masks_a_percent_encoded_setup_token_in_the_printed_setup_url(
    test_logger: logging.Logger, handler: LogCaptureHandler
) -> None:
    """The URL embeds ``quote(token, safe='')`` -- a token containing a
    reserved character therefore appears PERCENT-ENCODED in the log line, not
    raw. The value-based pass's percent-encoded variant (``_secret_value_variants``)
    must still catch it."""
    raw_token = "fake/boot token+with reserved chars"  # noqa: S105
    handler.secret_values = frozenset({raw_token})
    test_logger.addHandler(handler)
    test_logger.info(
        "Setup: %s",
        "http://localhost:8000/setup#setup_token=fake%2Fboot%20token%2Bwith%20reserved%20chars",
    )
    await asyncio.sleep(0)

    message = handler.ring_buffer[-1].message
    assert raw_token not in message
    assert "fake%2Fboot%20token%2Bwith%20reserved%20chars" not in message
    assert "<redacted>" in message


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


# --------------------------------------------------------------------------- #
# resolve_log_level (issue #100)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("configured", "expected_level"),
    [
        ("debug", logging.DEBUG),
        ("DEBUG", logging.DEBUG),
        ("Info", logging.INFO),
        ("WARNING", logging.WARNING),
        ("10", logging.DEBUG),  # already-numeric level string
        ("  warning  ", logging.WARNING),  # surrounding whitespace (env var hygiene)
    ],
)
def test_resolve_log_level_normalizes_valid_inputs(configured: str, expected_level: int) -> None:
    """Public entry point (exported for ``plex_manager.__main__`` -- issue #100)
    for the exact normalization ``configure_logging`` already relies on: any
    case of a standard level name, an already-numeric level string, and
    incidental leading/trailing whitespace (an env var, not a Python literal,
    so nothing strips it upstream) all resolve to the real stdlib int level."""
    assert resolve_log_level(configured) == expected_level


def test_resolve_log_level_falls_back_to_info_on_a_typo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo'd or otherwise unrecognized level (e.g. a mistyped
    ``PLEX_MANAGER_LOG_LEVEL``) must never raise -- it degrades to INFO with a
    warning surfaced through this module's own logger, never a silent guess and
    never a ``KeyError``/``ValueError`` that could crash a caller (uvicorn's
    ``Config``, in ``__main__.main``) before the app's own tolerant lifespan
    ever runs."""
    with caplog.at_level(logging.WARNING, logger="plex_manager.services.log_capture_service"):
        level = resolve_log_level("verbose")
    assert level == logging.INFO
    assert "verbose" in caplog.text


def test_resolve_log_level_falls_back_to_info_on_a_negative_number_string() -> None:
    # int() happily parses "-5"; resolve_log_level does not special-case a
    # nonsensical numeric level -- unlike a typo it is a VALID stdlib int, so it
    # passes straight through (mirrors int()'s own permissive behavior; the
    # honesty invariant here is "never raise", not "reject every odd value").
    assert resolve_log_level("-5") == -5


@pytest.mark.parametrize("configured", ["trace", "TRACE", "  Trace  "])
def test_resolve_log_level_maps_trace_to_debug_for_the_app_logger(configured: str) -> None:
    """``trace`` is a REAL uvicorn ``--log-level`` name (see
    ``uvicorn.config.LOG_LEVELS``) one rung below ``debug``, but stdlib
    ``logging`` has no such level -- ``logging.getLevelNamesMapping()`` has no
    ``'TRACE'`` entry unless some uvicorn ``Config`` has already registered it
    as a process-wide side effect, which this resolver must not depend on (it
    is exercised directly here with no uvicorn involved at all). For the
    app's OWN stdlib logger -- the only consumer of this int, wired in via
    ``configure_logging`` -- DEBUG is the honest analogue: the next real level
    down from INFO. This must NOT go through the generic
    unrecognized-name-warns-and-falls-back-to-INFO path: 'trace' is a
    genuinely valid setting, not a typo, and downgrading it all the way to
    INFO (skipping DEBUG entirely) would be a worse misrepresentation than
    mapping it to DEBUG outright. Uvicorn's own, separately-normalized launch
    argument (``plex_manager.__main__._uvicorn_log_level``) is what actually
    preserves full uvicorn-native TRACE behavior for the ASGI server itself.
    """
    assert resolve_log_level(configured) == logging.DEBUG


async def test_configure_logging_does_not_raise_on_trace(test_logger: logging.Logger) -> None:
    """End-to-end sanity check for the app-side consumer: a 'trace'
    ``config.log_level`` must reach ``configure_logging`` (called from the
    real ASGI lifespan) exactly like any other value -- no crash, and the
    logger ends up at DEBUG (see
    ``test_resolve_log_level_maps_trace_to_debug_for_the_app_logger``)."""
    handler = configure_logging("trace", logger=test_logger)
    try:
        assert test_logger.level == logging.DEBUG
    finally:
        stop_logging(handler, logger=test_logger)


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


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # A floor at/above WARNING is HONOURED (the pin quiets to the operator level):
        # an ERROR operator can no longer receive lower-severity third-party WARNINGs.
        ("ERROR", logging.ERROR),
        ("CRITICAL", logging.CRITICAL),
        # A floor below WARNING is CLAMPED UP to WARNING (the secret-safety floor):
        # the pin only ever quiets relative to the operator level, never loosens it,
        # so URL-bearing INFO records can never leak even at DEBUG.
        ("WARNING", logging.WARNING),
        ("INFO", logging.WARNING),
        ("DEBUG", logging.WARNING),
    ],
)
async def test_configure_logging_pins_third_party_to_max_of_floor_and_warning(
    test_logger: logging.Logger, configured: str, expected: int
) -> None:
    # Issue #94: the httpx/httpcore pin is ``max(resolved_level, WARNING)`` -- it QUIETS
    # relative to the operator's floor but NEVER loosens below WARNING. An operator who
    # sets an ERROR floor must not still receive third-party WARNINGs in live/durable
    # logs (the pre-fix defect), yet a DEBUG/INFO floor must still yield WARNING so the
    # secret-safety guard holds.
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    saved_levels = (httpx_logger.level, httpcore_logger.level)
    try:
        attached = configure_logging(configured, logger=test_logger)
        try:
            assert httpx_logger.getEffectiveLevel() == expected
            assert httpcore_logger.getEffectiveLevel() == expected
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
        oldest_first: bool = False,
    ) -> LogEventPage:
        raise NotImplementedError

    async def rewrite_redactable_fields(
        self,
        message_rewriter: Any,
        context_rewriter: Any,
    ) -> int:
        raise NotImplementedError

    async def prune_older_than(
        self,
        cutoff: datetime,
        *,
        loggers: Sequence[str] | None = None,
        exclude_loggers: bool = False,
    ) -> int:
        raise NotImplementedError

    async def prune_excess(self, max_rows: int) -> int:
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
    assert remaining.results == ()


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
    assert remaining.results == ()


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


# --------------------------------------------------------------------------- #
# prune_once's row-count cap (issue #152)
# --------------------------------------------------------------------------- #
async def test_prune_once_defaults_to_no_row_cap(sessionmaker_: SessionMaker) -> None:
    """``max_rows`` defaults to ``None`` -- unchanged behaviour for every
    existing caller/test that only wants the age-based sweep."""
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        for i in range(5):
            await repo.create(level="INFO", logger="a", message=f"m{i}", created_at=now)
        await session.commit()

        removed = await prune_once(repo, retention_days=7)
        await session.commit()
        assert removed == 0

        remaining = await repo.list_events(limit=10)
    assert remaining.total == 5


async def test_prune_once_enforces_the_row_cap_when_given(sessionmaker_: SessionMaker) -> None:
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        for i in range(5):
            await repo.create(
                level="INFO", logger="a", message=f"m{i}", created_at=now - timedelta(seconds=5 - i)
            )
        await session.commit()

        removed = await prune_once(repo, retention_days=7, max_rows=3)
        await session.commit()
        assert removed == 2

        remaining = await repo.list_events(limit=10, oldest_first=True)
    assert [r.message for r in remaining.results] == ["m2", "m3", "m4"]


async def test_prune_once_row_cap_applies_after_the_age_based_deletes(
    sessionmaker_: SessionMaker,
) -> None:
    """Both cutoffs stack: an age-stale row is removed by the age sweep, and the
    row-count cap then trims whatever survives that down further."""
    now = datetime.now(UTC)
    async with sessionmaker_() as session:
        repo = SqlLogEventRepository(session)
        await repo.create(
            level="INFO", logger="a", message="ancient", created_at=now - timedelta(days=10)
        )
        for i in range(3):
            await repo.create(
                level="INFO",
                logger="a",
                message=f"recent{i}",
                created_at=now - timedelta(seconds=3 - i),
            )
        await session.commit()

        removed = await prune_once(repo, retention_days=1, max_rows=2)
        await session.commit()
        # 1 from the age cutoff ("ancient") + 1 from the row cap (the oldest of
        # the 3 remaining "recent*" rows).
        assert removed == 2

        remaining = await repo.list_events(limit=10, oldest_first=True)
    assert [r.message for r in remaining.results] == ["recent1", "recent2"]
