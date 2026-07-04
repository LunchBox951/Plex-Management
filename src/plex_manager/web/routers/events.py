"""Realtime event stream — authenticated cache invalidation hints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from plex_manager.web.deps import require_api_key
from plex_manager.web.events import get_event_hub

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/events",
    tags=["events"],
    dependencies=[Depends(require_api_key)],
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
