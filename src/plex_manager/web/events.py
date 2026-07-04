"""In-process realtime event hub for authenticated web clients.

These events are web-adapter cache invalidation hints, not pure domain events.
The REST endpoints remain the source of truth; clients use these messages to
invalidate React Query keys and then refetch the existing typed DTOs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass

from fastapi import FastAPI

__all__ = [
    "EventHub",
    "EventSubscription",
    "RealtimeEvent",
    "close_realtime_streams",
    "get_event_hub",
    "publish_realtime",
]

_STATE_ATTR = "realtime_hub"


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
    request_id: int | None = None
    download_id: int | None = None

    def payload(self) -> dict[str, object]:
        """Return the JSON payload sent in the SSE ``data:`` field."""
        data: dict[str, object] = {
            "seq": self.seq,
            "topics": list(self.topics),
            "reason": self.reason,
        }
        if self.request_id is not None:
            data["request_id"] = self.request_id
        if self.download_id is not None:
            data["download_id"] = self.download_id
        return data


class EventSubscription:
    """One client's bounded event queue."""

    def __init__(
        self,
        hub: EventHub,
        queue: asyncio.Queue[RealtimeEvent | _StreamClosed],
    ) -> None:
        self._hub = hub
        self._queue = queue
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

    This is deliberately single-process. If the app later runs multiple workers,
    this should be replaced with a shared broker or durable outbox.
    """

    def __init__(self, *, max_queue_size: int = 32) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        self._max_queue_size = max_queue_size
        self._next_seq = 1
        self._subscribers: set[EventSubscription] = set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> EventSubscription:
        """Subscribe a client and enqueue an initial sync event."""
        subscription = EventSubscription(
            self,
            asyncio.Queue(maxsize=self._max_queue_size),
        )
        self._subscribers.add(subscription)
        subscription.enqueue(
            RealtimeEvent(
                seq=max(0, self._next_seq - 1),
                topics=("sync",),
                reason="connected",
            )
        )
        return subscription

    def publish(
        self,
        topics: Iterable[str],
        *,
        reason: str,
        request_id: int | None = None,
        download_id: int | None = None,
    ) -> RealtimeEvent:
        """Publish a coarse invalidation event to all current subscribers."""
        topic_tuple = tuple(dict.fromkeys(topics))
        if not topic_tuple:
            raise ValueError("publish requires at least one topic")
        event = RealtimeEvent(
            seq=self._next_seq,
            topics=topic_tuple,
            reason=reason,
            request_id=request_id,
            download_id=download_id,
        )
        self._next_seq += 1
        for subscription in tuple(self._subscribers):
            subscription.enqueue(event)
        return event

    def close_all(self, *, reason: str) -> None:
        """Close every active subscription, usually after app-key rotation."""
        _ = reason
        for subscription in tuple(self._subscribers):
            subscription.close()

    def unsubscribe(self, subscription: EventSubscription) -> None:
        self._subscribers.discard(subscription)


def get_event_hub(app: FastAPI) -> EventHub:
    """Return the app's realtime hub, creating it lazily for tests."""
    hub = getattr(app.state, _STATE_ATTR, None)
    if not isinstance(hub, EventHub):
        hub = EventHub()
        setattr(app.state, _STATE_ATTR, hub)
    return hub


def publish_realtime(
    app: FastAPI,
    topics: Iterable[str],
    *,
    reason: str,
    request_id: int | None = None,
    download_id: int | None = None,
) -> RealtimeEvent:
    """Publish on ``app.state`` without exposing hub plumbing to routers."""
    return get_event_hub(app).publish(
        topics,
        reason=reason,
        request_id=request_id,
        download_id=download_id,
    )


def close_realtime_streams(app: FastAPI, *, reason: str) -> None:
    """Close authenticated streams, for example after app-key rotation."""
    get_event_hub(app).close_all(reason=reason)
