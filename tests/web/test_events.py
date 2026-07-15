"""Realtime event stream for request/queue cache invalidation."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.types import Message, Scope

from plex_manager import __version__
from plex_manager.models import AuthSession, User
from plex_manager.ports.metadata import MovieMetadata
from plex_manager.web.deps import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    AuthContext,
    AuthMethod,
    hash_session_token,
    require_admin_short_session,
)
from plex_manager.web.events import (
    EventHub,
    RealtimeEvent,
    close_realtime_streams,
    current_build_id,
    get_event_hub,
    publish_realtime,
    warn_if_multiworker,
)
from plex_manager.web.routers import events as events_router
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

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


async def _browser_session(
    sessionmaker_: SessionMaker,
    *,
    tag: str,
    is_admin: bool,
    expires_at: datetime | None = None,
) -> tuple[int, dict[str, str], dict[str, str]]:
    """Mint a browser session and return ``(user_id, cookies, CSRF headers)``."""
    token = f"events-session-{tag}"
    csrf = f"events-csrf-{tag}"
    async with sessionmaker_() as session:
        user = User(
            plex_id=None,
            username=f"events-{tag}",
            permissions=1 if is_admin else 0,
        )
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(token),
                expires_at=expires_at or datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
        user_id = user.id
    cookies = {SESSION_COOKIE_NAME: token, CSRF_COOKIE_NAME: csrf}
    return user_id, cookies, {CSRF_HEADER_NAME: csrf}


def _cookie_headers(cookies: dict[str, str]) -> dict[str, str]:
    return {"Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items())}


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

    event = hub.publish(("requests", "queue"), reason="grab")
    received = await subscription.get()

    assert received == event
    assert received.seq > initial.seq
    assert received.topics == ("requests", "queue")
    assert received.payload() == {
        "seq": received.seq,
        "topics": ["requests", "queue"],
        "reason": "grab",
    }

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
    subscription.close()


async def test_event_hub_close_all_wakes_and_closes_subscribers() -> None:
    hub = EventHub(max_queue_size=2)
    subscription = hub.subscribe()
    _ = await subscription.get()

    hub.close_all(reason="app_key_rotated")

    with pytest.raises(StopAsyncIteration):
        await subscription.get()
    assert hub.subscriber_count == 0


async def test_event_hub_close_matching_only_closes_selected_principal() -> None:
    hub = EventHub(max_queue_size=4)
    api_key = hub.subscribe(auth_method=AuthMethod.api_key.value)
    selected = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=7)
    other_user = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=8)
    for subscription in (api_key, selected, other_user):
        _ = await subscription.get()

    hub.close_matching(
        reason="session_logged_out",
        auth_method=AuthMethod.plex_session.value,
        user_id=7,
    )

    with pytest.raises(StopAsyncIteration):
        await selected.get()
    assert not api_key.closed
    assert not other_user.closed
    assert hub.subscriber_count == 2

    event = hub.publish(("queue",), reason="progress")
    assert await api_key.get() == event
    assert await other_user.get() == event
    api_key.close()
    other_user.close()


async def test_events_endpoint_requires_authentication(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/events")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_key"


async def test_events_endpoint_accepts_admin_cookie_session(
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True)
    user_id, cookies, _csrf = await _browser_session(
        sessionmaker_, tag="admin-stream", is_admin=True
    )

    async with _AsgiStream(app, _cookie_headers(cookies)) as stream:
        assert stream.status == 200
        sync = _data_json(await stream.next_frame())
        assert sync["topics"] == ["sync"]
        assert sync["reason"] == "connected"

        hub = get_event_hub(app)
        subscriptions = tuple(hub._subscribers)  # pyright: ignore[reportPrivateUsage]
        assert len(subscriptions) == 1
        assert subscriptions[0].auth_method == AuthMethod.plex_session.value
        assert subscriptions[0].user_id == user_id

    await _wait_subscribers_zero(app)


async def test_events_endpoint_rejects_non_admin_without_subscribing(
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True)
    _user_id, cookies, _csrf = await _browser_session(
        sessionmaker_, tag="shared-stream", is_admin=False
    )

    async with _AsgiStream(app, _cookie_headers(cookies)) as stream:
        assert stream.status == 403

    assert get_event_hub(app).subscriber_count == 0


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
    assert "request_id" not in event.payload()
    assert "download_id" not in event.payload()
    subscription.close()


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


def test_current_build_id_prefers_injected_image_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLEX_MANAGER_BUILD_ID", raising=False)
    assert current_build_id() == __version__

    monkeypatch.setenv("PLEX_MANAGER_BUILD_ID", "abc123-image-revision")
    assert current_build_id() == "abc123-image-revision"


def test_multiworker_warning_is_count_aware_and_does_not_log_raw_args(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS", "WORKERS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers=1 --bind=127.0.0.1:8000")
    with caplog.at_level(logging.WARNING, logger="plex_manager.web.events"):
        warn_if_multiworker()
        assert not caplog.records

        monkeypatch.setenv("GUNICORN_CMD_ARGS", "--workers=3 --bind=secret-bearing-value")
        warn_if_multiworker()
        assert "GUNICORN_CMD_ARGS" in caplog.text
        assert "secret-bearing-value" not in caplog.text


def test_events_openapi_advertises_api_key_or_cookie_auth(app: FastAPI) -> None:
    security = app.openapi()["paths"]["/api/v1/events"]["get"]["security"]

    assert {"APIKeyHeader": []} in security
    assert {"APIKeyCookie": []} in security
    assert all("CSRFHeader" not in requirement for requirement in security)


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
        assert sync["app_version"] == current_build_id()

        # 2) A published event is delivered (skipping any heartbeat comments).
        publish_realtime(app, ("requests",), reason="grab")
        saw_event = False
        saw_heartbeat = False
        for _ in range(50):
            frame = await stream.next_frame()
            if frame.startswith(":"):
                saw_heartbeat = True
                continue
            payload = _data_json(frame)
            if payload.get("reason") == "grab":
                assert "requests" in payload["topics"]
                assert "request_id" not in payload
                assert "download_id" not in payload
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
        publish_realtime(app, ("queue",), reason="progress")
        saw_event = False
        for _ in range(50):
            frame = await stream.next_frame()
            if frame.startswith(":"):
                continue
            payload = _data_json(frame)
            if payload.get("reason") == "progress":
                assert "queue" in payload["topics"]
                assert "request_id" not in payload
                assert "download_id" not in payload
                saw_event = True
                break
        assert saw_event

    await _wait_subscribers_zero(app)


async def test_events_stream_closes_at_browser_session_expiry(
    app: FastAPI,
    seed: SeedFn,
) -> None:
    await seed(initialized=True)

    async def _expiring_admin() -> AuthContext:
        return AuthContext(
            method=AuthMethod.plex_session,
            user_id=77,
            is_admin=True,
            session_expires_at=datetime.now(UTC) + timedelta(seconds=0.5),
        )

    app.dependency_overrides[require_admin_short_session] = _expiring_admin
    try:
        async with _AsgiStream(app, {}) as stream:
            assert stream.status == 200
            sync = _data_json(await stream.next_frame())
            assert sync["reason"] == "connected"

            await stream.expect_body_end(timeout=2.0)
    finally:
        app.dependency_overrides.pop(require_admin_short_session, None)

    await _wait_subscribers_zero(app)


async def test_events_stream_closes_at_idle_deadline(
    app: FastAPI,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SSE lease is also capped at the session's IDLE deadline (issue #56).

    REST requests die at the idle window, but a long-lived stream that only
    enforced the 30-day absolute cap would keep delivering admin topics for weeks
    past when the session stopped authenticating for REST. The fake clock keeps
    the test on the intended sync-then-idle-close path under CI load.
    """
    await seed(initialized=True)

    clock = [1000.0]
    wait_calls = 0
    timeouts: list[float] = []

    def _fake_monotonic() -> float:
        return clock[0]

    async def _fake_wait_for_getter(getter: asyncio.Task[RealtimeEvent], *, timeout: float) -> bool:
        nonlocal wait_calls
        timeouts.append(timeout)
        wait_calls += 1
        if wait_calls == 1:
            await getter
            return True
        if wait_calls == 2:
            assert not getter.done()
            clock[0] = 1006.0
            return False
        raise AssertionError(f"unexpected waiter call {wait_calls}")

    async def _idle_soon_admin() -> AuthContext:
        now = datetime.now(UTC)
        return AuthContext(
            method=AuthMethod.plex_session,
            user_id=78,
            is_admin=True,
            session_expires_at=now + timedelta(days=30),
            session_idle_deadline=now + timedelta(seconds=5),
        )

    app.dependency_overrides[require_admin_short_session] = _idle_soon_admin
    monkeypatch.setattr(events_router, "_monotonic", _fake_monotonic)
    monkeypatch.setattr(events_router, "_wait_for_getter", _fake_wait_for_getter)
    try:
        async with _AsgiStream(app, {}) as stream:
            assert stream.status == 200
            sync = _data_json(await stream.next_frame())
            assert sync["topics"] == ["sync"]
            assert sync["reason"] == "connected"

            await stream.expect_body_end()
    finally:
        app.dependency_overrides.pop(require_admin_short_session, None)

    assert wait_calls == 2
    assert timeouts[0] == pytest.approx(5.0, abs=0.1)
    assert 0.0 < timeouts[0] < events_router._HEARTBEAT_SECONDS  # pyright: ignore[reportPrivateUsage]
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


async def test_events_stream_closes_on_app_key_revoke(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    async with _AsgiStream(app, _HEADERS) as stream:
        assert stream.status == 200
        _ = await stream.next_frame()  # sync

        revoke = await client.delete("/api/v1/settings/app-key", headers=_HEADERS)
        assert revoke.status_code == 204

        await stream.expect_body_end()

    await _wait_subscribers_zero(app)


async def test_events_stream_closes_when_its_browser_session_logs_out(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True)
    _user_id, cookies, csrf = await _browser_session(
        sessionmaker_, tag="logout-stream", is_admin=True
    )

    async with _AsgiStream(app, _cookie_headers(cookies)) as stream:
        assert stream.status == 200
        _ = await stream.next_frame()  # sync

        client.cookies.update(cookies)
        logout = await client.post("/api/v1/auth/logout", headers=csrf)
        assert logout.status_code == 204

        await stream.expect_body_end()

    await _wait_subscribers_zero(app)


async def _recovery_session(
    sessionmaker_: SessionMaker, *, tag: str
) -> tuple[dict[str, str], dict[str, str]]:
    """Mint a recovery/break-glass session (``user_id IS NULL``) and return
    ``(cookies, CSRF headers)`` — the cookie a valid ``X-Api-Key`` exchange yields
    (``POST /auth/api-key``), an admin session with NO Plex identity."""
    token = f"events-recovery-{tag}"
    csrf = f"events-recovery-csrf-{tag}"
    async with sessionmaker_() as session:
        session.add(
            AuthSession(
                user_id=None,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    cookies = {SESSION_COOKIE_NAME: token, CSRF_COOKIE_NAME: csrf}
    return cookies, {CSRF_HEADER_NAME: csrf}


async def test_events_stream_closes_when_its_recovery_session_logs_out(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A recovery-cookie session (``user_id IS NULL``) has an open SSE stream;
    logging it out must proactively close that stream rather than leaving it to
    reconnect/expiry (issue #293 finding 1). The subscription reports ``api_key``
    auth (no Plex identity), so logout closes it by that credential filter."""
    await seed(initialized=True, app_api_key=_API_KEY)
    cookies, csrf = await _recovery_session(sessionmaker_, tag="logout-stream")

    async with _AsgiStream(app, _cookie_headers(cookies)) as stream:
        assert stream.status == 200
        _ = await stream.next_frame()  # sync

        subscriptions = tuple(get_event_hub(app)._subscribers)  # pyright: ignore[reportPrivateUsage]
        assert len(subscriptions) == 1
        assert subscriptions[0].auth_method == AuthMethod.api_key.value
        assert subscriptions[0].user_id is None

        client.cookies.update(cookies)
        logout = await client.post("/api/v1/auth/logout", headers=csrf)
        assert logout.status_code == 204

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
