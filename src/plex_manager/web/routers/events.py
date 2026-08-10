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
from plex_manager.web.events import RealtimeEvent, StreamClosed, get_event_hub

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/events",
    tags=["events"],
)

_HEARTBEAT_SECONDS = 15.0

#: SSE event name of the final frame naming why the server ended the stream.
CLOSE_EVENT = "closed"

#: The close reason for a stream the server retired because the session behind
#: it reached its own idle/absolute deadline. Every other reason is supplied by
#: whoever called ``close_realtime_streams`` (``session_logged_out``,
#: ``sessions_revoked``, ``permission_downgraded``, ``app_key_rotated``,
#: ``app_key_revoked``, ``plex_server_repointed``, and the share-sweep pair
#: ``share_revalidation_share_revoked`` / ``share_revalidation_token_stale``).
SESSION_EXPIRED_REASON = "session_expired"


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

    When the SERVER ends the stream it sends one final ``closed`` frame naming
    the reason (``{"reason": "..."}``) before the body terminates, so the browser
    can show the honest message for that cause instead of treating every close as
    an anonymous network blip. A client-initiated disconnect gets no such frame.
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
    # Why this stream ended, when the server is the one that ended it and has
    # something honest to say. Stays ``None`` for a client-side disconnect —
    # there is nobody left to tell, so no final frame is emitted.
    close_reason: str | None = None
    try:
        while True:
            if await request.is_disconnected():
                break
            timeout = _HEARTBEAT_SECONDS
            if lease_deadline is not None:
                lease_remaining = lease_deadline - _monotonic()
                if lease_remaining <= 0:
                    close_reason = SESSION_EXPIRED_REASON
                    break
                timeout = min(timeout, lease_remaining)
            if getter is None:
                getter = asyncio.ensure_future(subscription.get())
            if not await _wait_for_getter(getter, timeout=timeout):
                if lease_deadline is not None and _monotonic() >= lease_deadline:
                    close_reason = SESSION_EXPIRED_REASON
                    break
                # Heartbeat: the getter stays pending for the next iteration, so
                # no enqueued event is ever discarded by a timeout cancellation.
                yield ServerSentEvent(comment="ping")
                continue
            try:
                event = getter.result()
            except StreamClosed as closed:
                # The hub cut this stream (logout, revoke, key rotation, the
                # share-revalidation sweep, …). It named a reason; put it on the
                # wire as the last frame so the browser can say WHY rather than
                # showing a bare reconnect (issue #556).
                close_reason = closed.reason
                break
            except StopAsyncIteration:
                break
            finally:
                getter = None
            yield ServerSentEvent(data=event.payload(), event="realtime", id=str(event.seq))
        if close_reason is not None:
            yield ServerSentEvent(data={"reason": close_reason}, event=CLOSE_EVENT)
    finally:
        if getter is not None and not getter.done():
            getter.cancel()
        elif getter is not None and not getter.cancelled():
            # Finished (event or StopAsyncIteration) but unconsumed at teardown —
            # retrieve the outcome so asyncio doesn't warn about it.
            _ = getter.exception()
        subscription.close()
