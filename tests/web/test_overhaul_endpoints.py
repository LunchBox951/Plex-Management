"""UI-overhaul endpoints: the season/episode lookups and the not-yet-implemented stubs.

The stubs answer 501 ``not_implemented`` with the structured envelope and must
never write anything; the season lookups are real reads over the metadata port.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date

import httpx
from fastapi import FastAPI

from plex_manager.ports.metadata import EpisodeInfo, TvMetadata
from tests.web.fakes import FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "overhaul-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_SHOW_ID = 1396


def _tmdb() -> FakeTmdb:
    return FakeTmdb(
        shows={_SHOW_ID: TvMetadata(tmdb_id=_SHOW_ID, title="Some Show", season_count=3)},
        season_episodes={
            (_SHOW_ID, 2): [
                EpisodeInfo(episode_number=2, air_date=date(2026, 10, 11)),
                EpisodeInfo(episode_number=1, air_date=date(2026, 10, 4)),
                EpisodeInfo(episode_number=3, air_date=None),
            ]
        },
    )


def _assert_not_implemented(response: httpx.Response) -> None:
    assert response.status_code == 501
    body = response.json()
    assert body["detail"] == "not_implemented"
    assert body["message"].endswith("isn't available yet.") or body["message"].endswith(
        "aren't available yet."
    )
    assert body["hint"]


async def test_tv_show_seasons_lists_regular_seasons(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    response = await client.get(f"/api/v1/discover/tv/{_SHOW_ID}/seasons", headers=_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"tmdb_id": _SHOW_ID, "title": "Some Show", "seasons": [1, 2, 3]}


async def test_tv_show_seasons_unknown_show_is_404(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    response = await client.get("/api/v1/discover/tv/999/seasons", headers=_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "media_not_found"}


async def test_tv_season_episodes_are_sorted_with_unknown_fields_null(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    response = await client.get(f"/api/v1/discover/tv/{_SHOW_ID}/seasons/2", headers=_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["season_number"] == 2
    assert [episode["episode_number"] for episode in body["episodes"]] == [1, 2, 3]
    assert body["episodes"][0] == {
        "episode_number": 1,
        "air_date": "2026-10-04",
        "name": None,
        "overview": None,
        "still_url": None,
        "runtime_minutes": None,
        "in_library": None,
    }
    assert body["episodes"][2]["air_date"] is None


async def test_tv_lookups_require_auth(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get(f"/api/v1/discover/tv/{_SHOW_ID}/seasons")

    assert response.status_code == 401


async def test_create_tv_with_follow_mode_is_not_implemented_and_writes_nothing(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    for mode in ("latest_season", "new_episodes"):
        response = await client.post(
            "/api/v1/requests",
            json={"tmdb_id": _SHOW_ID, "media_type": "tv", "tv_mode": mode},
            headers=_HEADERS,
        )
        _assert_not_implemented(response)

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.json()["requests"] == []


async def test_create_rejects_unknown_tv_mode(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    response = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": _SHOW_ID, "media_type": "tv", "tv_mode": "everything"},
        headers=_HEADERS,
    )

    assert response.status_code == 422


async def test_notifications_are_not_implemented(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    _assert_not_implemented(await client.get("/api/v1/notifications", headers=_HEADERS))
    _assert_not_implemented(
        await client.post("/api/v1/notifications/read", json={}, headers=_HEADERS)
    )


async def test_notifications_require_auth(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/notifications")

    assert response.status_code == 401


async def test_notify_admin_is_not_implemented(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post(
        "/api/v1/requests/1/notify-admin", json={"note": "wrong season"}, headers=_HEADERS
    )

    _assert_not_implemented(response)


async def test_notify_admin_note_is_bounded(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post(
        "/api/v1/requests/1/notify-admin", json={"note": "x" * 501}, headers=_HEADERS
    )

    assert response.status_code == 422


async def test_force_import_is_not_implemented(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post("/api/v1/queue/1/force-import", headers=_HEADERS)

    _assert_not_implemented(response)


def test_stub_endpoints_document_the_501_envelope(app: FastAPI) -> None:
    paths = app.openapi()["paths"]
    for path, method in (
        ("/api/v1/notifications", "get"),
        ("/api/v1/notifications/read", "post"),
        ("/api/v1/requests/{request_id}/notify-admin", "post"),
        ("/api/v1/queue/{download_id}/force-import", "post"),
        ("/api/v1/requests", "post"),
    ):
        schema = paths[path][method]["responses"]["501"]["content"]["application/json"]["schema"]
        assert schema["$ref"].endswith("/ErrorEnvelope"), path
