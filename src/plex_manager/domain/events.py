"""Tiny typed in-process pub/sub for domain events.

Radarr drives its blocklist-and-research flow off a ``DownloadFailedEvent`` with
two ordered subscribers (blocklist first, re-search second). This is the minimal
synchronous equivalent: a ``dict[type[Event], list[handler]]`` bus. Handlers run
in registration order, so a caller can register the blocklist writer before the
re-search trigger and rely on that ordering.

Pure domain: stdlib + pydantic-free dataclasses. Synchronous and side-effect-free
itself (handlers may have effects; the bus does not).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar, cast

__all__ = ["DownloadFailed", "Event", "EventBus"]


@dataclass(frozen=True)
class Event:
    """Base class for in-process domain events."""


@dataclass(frozen=True)
class DownloadFailed(Event):
    """A tracked download failed and should be blocklisted + re-searched.

    ``occurred_at`` is supplied by the caller (the domain bus never reads the
    clock) so the event stays a pure value.
    """

    torrent_hash: str
    source_title: str
    reason: str
    tmdb_id: int | None = None
    indexer: str | None = None
    occurred_at: datetime | None = None


EventT = TypeVar("EventT", bound=Event)
Handler = Callable[[EventT], None]


class EventBus:
    """A synchronous, in-process event bus keyed by concrete event type."""

    def __init__(self) -> None:
        self._subscribers: dict[type[Event], list[Handler[Event]]] = {}

    def subscribe(self, event_type: type[EventT], handler: Handler[EventT]) -> None:
        """Register ``handler`` for ``event_type``; preserves registration order."""
        handlers = self._subscribers.setdefault(event_type, [])
        # Safe: handlers for ``event_type`` only ever receive that exact type.
        handlers.append(cast("Handler[Event]", handler))

    def publish(self, event: Event) -> None:
        """Invoke every handler registered for ``type(event)`` in order.

        Iterates a SNAPSHOT (``list(...)``) of the subscriber list taken before
        dispatch begins (issue #110), not the live list held in
        ``self._subscribers``. A handler that itself calls :meth:`subscribe` for
        the same event type while dispatch is in flight (registering a new
        handler, e.g. from a test or a handler that self-registers a follow-up)
        must never affect the CURRENT publish — it must be invoked starting only
        from the NEXT publish. Without the snapshot, mutating the list mid-
        iteration could skip an already-scheduled handler or double-invoke one,
        depending on exactly where the mutation lands relative to the live
        iterator.
        """
        for handler in list(self._subscribers.get(type(event), [])):
            handler(event)
