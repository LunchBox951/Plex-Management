"""Realtime event stream for request/queue cache invalidation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.ports.metadata import MovieMetadata
from plex_manager.web.events import EventHub, get_event_hub
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "events-key"
_HEADERS = {"X-Api-Key": _API_KEY}


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
