"""In-process realtime event hub for authenticated web clients.

These events are web-adapter cache invalidation hints, not pure domain events.
The REST endpoints remain the source of truth; clients use these messages to
invalidate React Query keys and then refetch the existing typed DTOs.

**Single-worker invariant.** The hub is an *in-process* fanout: publishers and
subscribers must live in the same process. It is deliberately not backed by a
shared broker, so running the app under more than one worker (``uvicorn
--workers N`` / ``WEB_CONCURRENCY``/``GUNICORN`` > 1) silently drops events for
every client that happens to be pinned to a different worker than the publisher.
The polling floor on the client (a permanent safety net) means the UI still
self-heals in that case, but the realtime path is only correct single-worker.
:func:`warn_if_multiworker` surfaces a loud startup WARNING when a multi-worker
configuration is detectable from the environment.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import FastAPI

from plex_manager import __version__

__all__ = [
    "EventHub",
    "EventSubscription",
    "RealtimeEvent",
    "close_realtime_streams",
    "current_build_id",
    "get_event_hub",
    "publish_realtime",
    "warn_if_multiworker",
]

_logger = logging.getLogger(__name__)

_STATE_ATTR = "realtime_hub"
_BUILD_ID_ENV = "PLEX_MANAGER_BUILD_ID"
_GUNICORN_WORKERS_RE = re.compile(r"(?:^|\s)(?:--workers|-w)(?:=|\s+)(\d+)(?:\s|$)")


class _StreamClosed:
    """Sentinel delivered to subscribers when the server intentionally closes."""


_STREAM_CLOSED = _StreamClosed()


@dataclass(frozen=True)
class RealtimeEvent:
    """A coarse client invalidation event.

    ``topics`` are intentionally broad: the browser invalidates existing REST
    queries rather than trusting this event as row-level state.
    """

    seq: int
    topics: tuple[str, ...]
    reason: str
    app_version: str | None = None

    def payload(self) -> dict[str, object]:
        """Return the JSON payload sent in the SSE ``data:`` field."""
        data: dict[str, object] = {
            "seq": self.seq,
            "topics": list(self.topics),
            "reason": self.reason,
        }
        if self.app_version is not None:
            data["app_version"] = self.app_version
        return data


class EventSubscription:
    """One client's bounded event queue."""

    def __init__(
        self,
        hub: EventHub,
        queue: asyncio.Queue[RealtimeEvent | _StreamClosed],
        *,
        auth_method: str | None,
        user_id: int | None,
    ) -> None:
        self._hub = hub
        self._queue = queue
        self.auth_method = auth_method
        self.user_id = user_id
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def get(self) -> RealtimeEvent:
        """Return the next event, or raise ``StopAsyncIteration`` after close."""
        item = await self._queue.get()
        if isinstance(item, _StreamClosed):
            self.close()
            raise StopAsyncIteration
        return item

    def close(self) -> None:
        """Unsubscribe and wake any waiter with a close sentinel."""
        if self._closed:
            return
        self._closed = True
        self._hub.unsubscribe(self)
        self._clear_queue()
        self._queue.put_nowait(_STREAM_CLOSED)

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _replace_with(self, event: RealtimeEvent) -> None:
        self._clear_queue()
        self._queue.put_nowait(event)

    def enqueue(self, event: RealtimeEvent) -> None:
        if self._closed:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._replace_with(RealtimeEvent(seq=event.seq, topics=("sync",), reason="overflow"))


class EventHub:
    """Bounded in-process fanout hub.

    This is deliberately single-process (see the module docstring's single-worker
    invariant). If the app later runs multiple workers, this must be replaced with
    a shared broker or durable outbox — the documented scale-out path in ADR-0017.

    ``app_version`` is stamped into every connect-time ``sync`` event so a client
    that reconnects after a rolling ``:edge`` image swap (ADR-0004) can detect the
    version change and prompt a reload.
    """

    def __init__(self, *, max_queue_size: int = 32, app_version: str | None = None) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        self._max_queue_size = max_queue_size
        self._app_version = app_version
        self._next_seq = 1
        self._subscribers: set[EventSubscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(
        self, *, auth_method: str | None = None, user_id: int | None = None
    ) -> EventSubscription:
        """Subscribe a client and enqueue an initial sync event."""
        subscription = EventSubscription(
            self,
            asyncio.Queue(maxsize=self._max_queue_size),
            auth_method=auth_method,
            user_id=user_id,
        )
        self._subscribers.add(subscription)
        subscription.enqueue(
            RealtimeEvent(
                seq=max(0, self._next_seq - 1),
                topics=("sync",),
                reason="connected",
                app_version=self._app_version,
            )
        )
        return subscription

    def publish(
        self,
        topics: Iterable[str],
        *,
        reason: str,
    ) -> RealtimeEvent:
        """Publish a coarse invalidation event to all current subscribers.

        Short-circuits when there are no subscribers (Radarr's ``IsConnected``
        gate): the ~dozen ``publish_realtime`` call sites in the request/queue/
        reconcile/autograb/eviction paths then do no fanout work while nobody is
        watching. ``seq`` is still advanced so ids stay monotonic if a client
        connects between two publishes.
        """
        topic_tuple = tuple(dict.fromkeys(topics))
        if not topic_tuple:
            raise ValueError("publish requires at least one topic")
        seq = self._next_seq
        self._next_seq += 1
        event = RealtimeEvent(
            seq=seq,
            topics=topic_tuple,
            reason=reason,
        )
        if not self._subscribers:
            return event
        for subscription in tuple(self._subscribers):
            subscription.enqueue(event)
        return event

    def close_all(self, *, reason: str) -> None:
        """Close every active subscription."""
        self.close_matching(reason=reason)

    def close_matching(
        self,
        *,
        reason: str,
        auth_method: str | None = None,
        user_id: int | None = None,
    ) -> None:
        """Close subscriptions matching an optional credential/principal filter."""
        _ = reason
        for subscription in tuple(self._subscribers):
            if auth_method is not None and subscription.auth_method != auth_method:
                continue
            if user_id is not None and subscription.user_id != user_id:
                continue
            subscription.close()

    def unsubscribe(self, subscription: EventSubscription) -> None:
        self._subscribers.discard(subscription)


def get_event_hub(app: FastAPI) -> EventHub:
    """Return the app's realtime hub, creating it lazily for tests."""
    hub = getattr(app.state, _STATE_ATTR, None)
    if not isinstance(hub, EventHub):
        hub = EventHub(app_version=current_build_id())
        setattr(app.state, _STATE_ATTR, hub)
    return hub


def current_build_id() -> str:
    """Return the immutable image/build identifier advertised to open tabs.

    Container CI injects the commit SHA. Source/dev runs fall back to the package
    version, which keeps local tests deterministic without pretending ``0.0.0``
    can distinguish deployed images.
    """
    return os.environ.get(_BUILD_ID_ENV) or __version__


def warn_if_multiworker() -> None:
    """Log a loud WARNING when a multi-worker deployment is detectable.

    The in-process hub only fans events out within its own process, so more than
    one worker silently drops realtime events for clients pinned to a sibling
    worker (the polling floor still heals the UI, but the realtime path is wrong).
    We can only *detect* the common signals — ``WEB_CONCURRENCY``, gunicorn's
    ``--workers`` via ``GUNICORN_CMD_ARGS``/``WORKERS``, or ``UVICORN_WORKERS`` —
    the true worker count is not introspectable from inside a worker. This is a
    best-effort guard, deliberately noisy rather than silent (north-star #3).
    """
    signals: set[str] = set()
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "WORKERS"):
        raw = os.environ.get(var)
        if raw is None:
            continue
        try:
            if int(raw) > 1:
                signals.add(var)
        except ValueError:
            continue
    gunicorn_args = os.environ.get("GUNICORN_CMD_ARGS", "")
    gunicorn_workers = _GUNICORN_WORKERS_RE.search(gunicorn_args)
    if gunicorn_workers is not None and int(gunicorn_workers.group(1)) > 1:
        signals.add("GUNICORN_CMD_ARGS")
    if signals:
        _logger.warning(
            "realtime SSE hub is in-process and single-worker only, but a "
            "multi-worker configuration was detected (%s); realtime events will "
            "be dropped for clients on other workers — run a single worker or "
            "add a shared broker (ADR-0017 scale-out path). The client polling "
            "floor still heals the UI.",
            ", ".join(sorted(signals)),
        )


def publish_realtime(
    app: FastAPI,
    topics: Iterable[str],
    *,
    reason: str,
) -> RealtimeEvent:
    """Publish on ``app.state`` without exposing hub plumbing to routers."""
    return get_event_hub(app).publish(
        topics,
        reason=reason,
    )


def close_realtime_streams(
    app: FastAPI,
    *,
    reason: str,
    auth_method: str | None = None,
    user_id: int | None = None,
) -> None:
    """Close streams invalidated by a credential or session state change."""
    get_event_hub(app).close_matching(
        reason=reason,
        auth_method=auth_method,
        user_id=user_id,
    )
