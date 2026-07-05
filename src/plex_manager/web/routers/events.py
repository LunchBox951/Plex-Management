"""Realtime event stream — authenticated cache invalidation hints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from plex_manager.web.deps import require_api_key_short_session
from plex_manager.web.events import RealtimeEvent, get_event_hub

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/events",
    tags=["events"],
    # Auth that owns a short-lived session and closes it BEFORE streaming begins,
    # so the long-lived SSE connection never pins a DB connection (see
    # ``require_api_key_short_session``). A plain ``Depends(require_api_key)`` would
    # hold ``get_session``'s yield-scoped connection for the tab's whole lifetime.
    dependencies=[Depends(require_api_key_short_session)],
)

_HEARTBEAT_SECONDS = 15.0


@router.get("", response_class=EventSourceResponse)
async def events_endpoint(request: Request) -> AsyncIterator[ServerSentEvent]:
    """Stream realtime invalidation events for the authenticated SPA.

    The stream holds no DB session. Each event is a hint to refetch existing REST
    resources, so reconnects and overflow collapse to a broad ``sync`` event.
    """
    # The pending ``subscription.get()`` is held as a *persistent* task and raced
    # against the heartbeat with ``asyncio.wait``, which leaves the loser pending
    # rather than cancelling it. A plain ``wait_for(get(), timeout)`` cancels the
    # getter on every heartbeat, and on the exact heartbeat-boundary race that
    # cancellation can strand a just-enqueued event on the interpreter's queue-
    # cancellation path. Never cancelling the getter mid-flight keeps delivery
    # structurally lossless (north-star #3), independent of CPython internals; the
    # getter is only cancelled at teardown.
    subscription = get_event_hub(request.app).subscribe()
    getter: asyncio.Task[RealtimeEvent] | None = None
    try:
        while True:
            if await request.is_disconnected():
                break
            if getter is None:
                getter = asyncio.ensure_future(subscription.get())
            done, _pending = await asyncio.wait({getter}, timeout=_HEARTBEAT_SECONDS)
            if getter not in done:
                # Heartbeat: the getter stays pending for the next iteration, so
                # no enqueued event is ever discarded by a timeout cancellation.
                yield ServerSentEvent(comment="ping")
                continue
            try:
                event = getter.result()
            except StopAsyncIteration:
                break
            finally:
                getter = None
            yield ServerSentEvent(data=event.payload(), event="realtime", id=str(event.seq))
    finally:
        if getter is not None and not getter.done():
            getter.cancel()
        elif getter is not None and not getter.cancelled():
            # Finished (event or StopAsyncIteration) but unconsumed at teardown —
            # retrieve the outcome so asyncio doesn't warn about it.
            _ = getter.exception()
        subscription.close()
