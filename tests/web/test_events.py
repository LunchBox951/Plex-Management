"""Realtime event stream for request/queue cache invalidation."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message, Scope

from plex_manager import __version__
from plex_manager.ports.metadata import MovieMetadata
from plex_manager.web.events import (
    EventHub,
    close_realtime_streams,
    get_event_hub,
    publish_realtime,
)
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "events-key"
_HEADERS = {"X-Api-Key": _API_KEY}


def _data_json(frame: str) -> dict[str, Any]:
    """Extract and parse the ``data:`` payload from a complete SSE frame."""
    for line in frame.splitlines():
        if line.startswith("data:"):
            parsed: dict[str, Any] = json.loads(line[len("data:") :].strip())
            return parsed
    raise AssertionError(f"no data field in frame: {frame!r}")


async def _wait_subscribers_zero(app: FastAPI, timeout: float = 2.0) -> None:
    """Poll until the disconnected stream's subscription has been cleaned up."""
    hub = get_event_hub(app)
    for _ in range(int(timeout / 0.01)):
        if hub.subscriber_count == 0:
            return
        await asyncio.sleep(0.01)
    assert hub.subscriber_count == 0, "stream subscription leaked after disconnect"


class _AsgiStream:
    """Drive the ASGI app's SSE endpoint directly at the HTTP/wire level.

    httpx's ``ASGITransport`` buffers the *entire* response body before returning,
    which deadlocks on an unbounded SSE stream. Driving the app callable with our
    own ``receive``/``send`` lets us read frames incrementally, trigger a client
    disconnect on demand, and observe the wire-level response headers — while
    still exercising the real middleware, routing, auth, and SSE encoding.
    """

    def __init__(self, app: FastAPI, headers: dict[str, str]) -> None:
        raw_headers = [(b"host", b"localhost")]
        raw_headers += [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        self._scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/events",
            "raw_path": b"/api/v1/events",
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "server": ("localhost", 80),
            "client": ("127.0.0.1", 12345),
            "state": {},
        }
        self._app = app
        self._sent: asyncio.Queue[Message] = asyncio.Queue()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._task: asyncio.Task[None] | None = None
        self._buf = ""
        self._body_done = False
        self.status: int | None = None
        self.headers: dict[str, str] = {}

    async def _receive(self) -> Message:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: Message) -> None:
        await self._sent.put(message)

    async def __aenter__(self) -> _AsgiStream:
        self._task = asyncio.ensure_future(self._app(self._scope, self._receive, self._send))
        start = await asyncio.wait_for(self._sent.get(), timeout=3.0)
        assert start["type"] == "http.response.start", start
        self.status = int(start["status"])
        raw: list[tuple[bytes, bytes]] = start.get("headers", [])
        self.headers = {k.decode().lower(): v.decode() for k, v in raw}
        return self

    async def next_frame(self, timeout: float = 3.0) -> str:
        """Return the next ``\\n\\n``-terminated SSE frame; raise on body end."""
        while "\n\n" not in self._buf:
            if self._body_done:
                raise StopAsyncIteration
            msg = await asyncio.wait_for(self._sent.get(), timeout)
            if msg["type"] == "http.response.body":
                body = msg.get("body", b"")
                assert isinstance(body, bytes)
                self._buf += body.decode()
                if not msg.get("more_body", False):
                    self._body_done = True
        frame, self._buf = self._buf.split("\n\n", 1)
        return frame

    async def expect_body_end(self, timeout: float = 3.0) -> None:
        """Assert the response body terminates (server closed the stream)."""
        try:
            for _ in range(200):
                await self.next_frame(timeout=timeout)
        except StopAsyncIteration:
            return
        raise AssertionError("stream did not close")

    async def __aexit__(self, *_exc: object) -> None:
        self._disconnect.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=3.0)
            except (TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task


async def test_event_hub_subscriber_receives_sync_then_published_event() -> None:
    hub = EventHub(max_queue_size=4)
    subscription = hub.subscribe()

    initial = await subscription.get()
    assert initial.topics == ("sync",)
    assert initial.reason == "connected"

    event = hub.publish(("requests", "queue"), reason="grab", request_id=12, download_id=4)
    received = await subscription.get()

    assert received == event
    assert received.seq > initial.seq
    assert received.topics == ("requests", "queue")
    assert received.request_id == 12
    assert received.download_id == 4

    subscription.close()
    assert hub.subscriber_count == 0


async def test_event_hub_overflow_collapses_subscriber_to_sync_event() -> None:
    hub = EventHub(max_queue_size=1)
    subscription = hub.subscribe()
    _ = await subscription.get()

    first = hub.publish(("queue",), reason="progress")
    second = hub.publish(("queue",), reason="progress")
    received = await subscription.get()

    assert first.seq < second.seq
    assert received.topics == ("sync",)
    assert received.reason == "overflow"
    assert received.seq == second.seq


async def test_event_hub_close_all_wakes_and_closes_subscribers() -> None:
    hub = EventHub(max_queue_size=2)
    subscription = hub.subscribe()
    _ = await subscription.get()

    hub.close_all(reason="app_key_rotated")

    with pytest.raises(StopAsyncIteration):
        await subscription.get()
    assert hub.subscriber_count == 0


async def test_events_endpoint_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/events")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_key"


async def test_create_request_publishes_request_change_event(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    hub = get_event_hub(app)
    subscription = hub.subscribe()
    _ = await subscription.get()
    override_adapters(
        app,
        tmdb=FakeTmdb(movies={603: MovieMetadata(tmdb_id=603, title="Some Movie", year=2020)}),
    )

    response = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie"},
        headers=_HEADERS,
    )

    assert response.status_code == 201
    event = await subscription.get()
    assert event.topics == ("requests", "discover")
    assert event.reason == "request_created"
    assert event.request_id == response.json()["id"]


async def test_publish_short_circuits_with_no_subscribers() -> None:
    hub = EventHub(max_queue_size=2, app_version="9.9.9")

    # No subscribers: publish does no fanout but still advances ``seq`` so ids
    # stay monotonic if a client connects between two publishes.
    first = hub.publish(("requests",), reason="grab")
    second = hub.publish(("queue",), reason="grab")
    assert second.seq > first.seq
    assert hub.subscriber_count == 0

    # A client connecting afterwards still gets a version-stamped sync event.
    subscription = hub.subscribe()
    sync = await subscription.get()
    assert sync.topics == ("sync",)
    assert sync.reason == "connected"
    assert sync.app_version == "9.9.9"
    subscription.close()


async def test_events_stream_delivers_sync_published_event_and_heartbeat(
    app: FastAPI,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    # Tiny heartbeat so a keep-alive ``: ping`` comment is observable quickly.
    monkeypatch.setattr("plex_manager.web.routers.events._HEARTBEAT_SECONDS", 0.05)

    async with _AsgiStream(app, _HEADERS) as stream:
        assert stream.status == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        # Transport hygiene: proxies must not buffer or cache the stream.
        assert stream.headers["cache-control"] == "no-cache"
        assert stream.headers["x-accel-buffering"] == "no"

        # 1) Initial connect-time sync frame, stamped with the app version.
        sync = _data_json(await stream.next_frame())
        assert sync["topics"] == ["sync"]
        assert sync["reason"] == "connected"
        assert sync["app_version"] == __version__

        # 2) A published event is delivered (skipping any heartbeat comments).
        publish_realtime(app, ("requests",), reason="grab", request_id=7)
        saw_event = False
        saw_heartbeat = False
        for _ in range(50):
            frame = await stream.next_frame()
            if frame.startswith(":"):
                saw_heartbeat = True
                continue
            payload = _data_json(frame)
            if payload.get("request_id") == 7:
                assert "requests" in payload["topics"]
                saw_event = True
                break
        assert saw_event

        # 3) A heartbeat comment is observed on the otherwise-idle stream.
        for _ in range(50):
            frame = await stream.next_frame()
            if frame.startswith(":"):
                saw_heartbeat = True
                break
        assert saw_heartbeat

    # 4) Clean teardown: leaving the context disconnects the client and the
    #    subscription is removed from the hub.
    await _wait_subscribers_zero(app)


async def test_events_stream_delivers_event_published_after_heartbeats(
    app: FastAPI,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An event enqueued after the stream has idled through heartbeats is still
    delivered — the getter persists across each heartbeat timeout instead of being
    cancelled, so no event is dropped on the heartbeat boundary (north-star #3)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    monkeypatch.setattr("plex_manager.web.routers.events._HEARTBEAT_SECONDS", 0.02)

    async with _AsgiStream(app, _HEADERS) as stream:
        assert stream.status == 200
        sync = _data_json(await stream.next_frame())
        assert sync["reason"] == "connected"

        # Idle long enough to cross at least one heartbeat boundary.
        saw_heartbeat = False
        for _ in range(50):
            frame = await stream.next_frame()
            if frame.startswith(":"):
                saw_heartbeat = True
                break
        assert saw_heartbeat

        # Publish only now, after the getter has already survived a heartbeat.
        publish_realtime(app, ("queue",), reason="progress", download_id=9)
        saw_event = False
        for _ in range(50):
            frame = await stream.next_frame()
            if frame.startswith(":"):
                continue
            payload = _data_json(frame)
            if payload.get("download_id") == 9:
                assert "queue" in payload["topics"]
                saw_event = True
                break
        assert saw_event

    await _wait_subscribers_zero(app)


async def test_events_stream_closes_on_app_key_rotation(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    async with _AsgiStream(app, _HEADERS) as stream:
        assert stream.status == 200
        sync = _data_json(await stream.next_frame())
        assert sync["reason"] == "connected"

        # Rotate the app key end-to-end via the real endpoint (a separate,
        # buffered request); its ``close_realtime_streams`` call must terminate
        # this live stream.
        rotate = await client.post("/api/v1/settings/app-key/rotate", headers=_HEADERS)
        assert rotate.status_code == 200

        await stream.expect_body_end()

    await _wait_subscribers_zero(app)


async def test_events_stream_closes_via_close_all_helper(
    app: FastAPI,
    seed: SeedFn,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    async with _AsgiStream(app, _HEADERS) as stream:
        assert stream.status == 200
        _ = await stream.next_frame()  # sync

        close_realtime_streams(app, reason="app_key_rotated")

        await stream.expect_body_end()

    await _wait_subscribers_zero(app)
