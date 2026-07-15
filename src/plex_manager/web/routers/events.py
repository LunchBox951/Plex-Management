"""Realtime event stream — authenticated cache invalidation hints."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from plex_manager.web.deps import AuthContext, require_admin_short_session
from plex_manager.web.events import RealtimeEvent, get_event_hub

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/events",
    tags=["events"],
)

_HEARTBEAT_SECONDS = 15.0


def _monotonic() -> float:
    return time.monotonic()


async def _wait_for_getter(getter: asyncio.Task[RealtimeEvent], *, timeout: float) -> bool:
    done, _pending = await asyncio.wait({getter}, timeout=timeout)
    return getter in done


@router.get("", response_class=EventSourceResponse)
async def events_endpoint(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_admin_short_session)],
) -> AsyncIterator[ServerSentEvent]:
    """Stream realtime invalidation events for the authenticated admin SPA.

    The stream holds no DB session. Each event is a hint to refetch existing REST
    resources, so reconnects and overflow collapse to a broad ``sync`` event.
    Shared Plex users retain the normal polling path instead: global queue,
    blocklist, and request-activity signals would otherwise reveal admin-only or
    other-user activity even when the REST resources themselves stay filtered.
    """
    # The pending ``subscription.get()`` is held as a *persistent* task and raced
    # against the heartbeat with ``asyncio.wait``, which leaves the loser pending
    # rather than cancelling it. A plain ``wait_for(get(), timeout)`` cancels the
    # getter on every heartbeat, and on the exact heartbeat-boundary race that
    # cancellation can strand a just-enqueued event on the interpreter's queue-
    # cancellation path. Never cancelling the getter mid-flight keeps delivery
    # structurally lossless (north-star #3), independent of CPython internals; the
    # getter is only cancelled at teardown.
    subscription = get_event_hub(request.app).subscribe(
        auth_method=auth.method.value,
        user_id=auth.user_id,
    )
    # Cap the stream's lifetime at the SESSION's own deadline so an open SSE
    # stream never outlives the session behind it (issue #56). Two bounds apply,
    # both fixed at connect time: the absolute ``session_expires_at`` (30-day cap)
    # AND the idle deadline (effective ``last_seen`` + ``SESSION_IDLE_WINDOW``).
    # REST requests die at the idle window; without the idle bound here a stream
    # would keep delivering admin topics until the 30-day cap even though every
    # REST call from the same session already 401s. Connect counts as activity
    # (the auth dependency just slid ``last_seen`` forward, throttled), so a
    # continuously-open stream re-leases on each reconnect. The tighter bound wins.
    now = datetime.now(UTC)
    session_deadlines = [
        deadline
        for deadline in (auth.session_expires_at, auth.session_idle_deadline)
        if deadline is not None
    ]
    lease_deadline: float | None = None
    if session_deadlines:
        remaining = (min(session_deadlines) - now).total_seconds()
        lease_deadline = _monotonic() + max(0.0, remaining)
    getter: asyncio.Task[RealtimeEvent] | None = None
    try:
        while True:
            if await request.is_disconnected():
                break
            timeout = _HEARTBEAT_SECONDS
            if lease_deadline is not None:
                lease_remaining = lease_deadline - _monotonic()
                if lease_remaining <= 0:
                    break
                timeout = min(timeout, lease_remaining)
            if getter is None:
                getter = asyncio.ensure_future(subscription.get())
            if not await _wait_for_getter(getter, timeout=timeout):
                if lease_deadline is not None and _monotonic() >= lease_deadline:
                    break
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
