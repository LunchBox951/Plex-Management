"""Realtime event stream — authenticated cache invalidation hints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from plex_manager.web.deps import require_api_key_short_session
from plex_manager.web.events import get_event_hub

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
    subscription = get_event_hub(request.app).subscribe()
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(subscription.get(), timeout=_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ServerSentEvent(comment="ping")
                continue
            except StopAsyncIteration:
                break
            yield ServerSentEvent(data=event.payload(), event="realtime", id=str(event.seq))
    finally:
        subscription.close()
