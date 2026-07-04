"""Plex OAuth login endpoints and session-cookie auth for issue #28."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import User
from plex_manager.web.deps import SettingsStore

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "s3cr3t-app-key"


async def _seed_plex_settings(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("plex_url", "http://plex.local:32400")
        await store.set("plex_token", "service-token")
        await session.commit()


def _plex_oauth_transport() -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/pins":
            assert request.method == "POST"
            assert request.headers["X-Plex-Product"] == "Plex Manager"
            return httpx.Response(200, json={"id": 123, "code": "ABCD", "expiresIn": 600})
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/pins/123":
            assert request.method == "GET"
            return httpx.Response(200, json={"id": 123, "code": "ABCD", "authToken": "user-token"})
        if request.url.host == "plex.tv" and request.url.path == "/users/account.json":
            assert request.headers["X-Plex-Token"] == "user-token"
            return httpx.Response(
                200,
                json={
                    "user": {
                        "id": 42,
                        "username": "plex-owner",
                        "email": "owner@example.test",
                        "thumb": "http://plex/avatar.png",
                    }
                },
            )
        if request.url.host == "plex.tv" and request.url.path == "/api/resources":
            assert request.headers["X-Plex-Token"] == "user-token"
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Home Plex",
                        "clientIdentifier": "server-machine-id",
                        "owned": True,
                    }
                ],
            )
        if request.url.host == "plex.local" and request.url.path == "/identity":
            assert request.headers["X-Plex-Token"] == "service-token"
            return httpx.Response(
                200,
                json={"MediaContainer": {"machineIdentifier": "server-machine-id"}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def _plex_oauth_transport_for_resource(
    *, owned: bool, machine_id: str = "server-machine-id"
) -> httpx.MockTransport:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/pins":
            return httpx.Response(200, json={"id": 123, "code": "ABCD", "expiresIn": 600})
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/pins/123":
            return httpx.Response(200, json={"id": 123, "code": "ABCD", "authToken": "user-token"})
        if request.url.host == "plex.tv" and request.url.path == "/users/account.json":
            return httpx.Response(200, json={"user": {"id": 99, "username": "guest"}})
        if request.url.host == "plex.tv" and request.url.path == "/api/resources":
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Shared Plex",
                        "clientIdentifier": machine_id,
                        "owned": owned,
                    }
                ],
            )
        if request.url.host == "plex.local" and request.url.path == "/identity":
            return httpx.Response(
                200,
                json={"MediaContainer": {"machineIdentifier": "server-machine-id"}},
            )
        raise AssertionError(request.url.path)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_plex_start_is_open_after_setup_and_returns_auth_url(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_settings(sessionmaker_)
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=_plex_oauth_transport())

    response = await client.post("/api/v1/auth/plex/start")

    assert response.status_code == 200
    body = response.json()
    assert body["state"]
    assert body["auth_url"].startswith("https://app.plex.tv/auth#")
    assert "code=ABCD" in body["auth_url"]
    start_cookie = response.cookies.get("plexmgr.login")
    assert start_cookie
    assert start_cookie not in body["auth_url"]
    assert "httponly" in response.headers["set-cookie"].lower()


@pytest.mark.asyncio
async def test_plex_complete_creates_owner_session_cookie(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_settings(sessionmaker_)
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=_plex_oauth_transport())

    start = await client.post("/api/v1/auth/plex/start")
    state = start.json()["state"]
    complete = await client.post("/api/v1/auth/plex/complete", json={"state": state})

    assert complete.status_code == 200
    body = complete.json()
    assert body["auth_method"] == "plex_session"
    assert body["user"]["plex_id"] == 42
    assert body["user"]["is_admin"] is True
    session_cookie = complete.cookies.get("plexmgr.session")
    csrf_cookie = complete.cookies.get("plexmgr.csrf")
    assert session_cookie
    assert csrf_cookie

    settings = await client.get("/api/v1/settings", cookies={"plexmgr.session": session_cookie})
    assert settings.status_code == 200

    replay = await client.post("/api/v1/auth/plex/complete", json={"state": state})
    assert replay.status_code == 409
    assert replay.json()["detail"] == "invalid_plex_login_state"


@pytest.mark.asyncio
async def test_session_rotate_app_key_ignores_stale_key_header(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_settings(sessionmaker_)
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=_plex_oauth_transport())

    start = await client.post("/api/v1/auth/plex/start")
    complete = await client.post(
        "/api/v1/auth/plex/complete", json={"state": start.json()["state"]}
    )
    session_cookie = complete.cookies["plexmgr.session"]
    csrf_cookie = complete.cookies["plexmgr.csrf"]

    rotate = await client.post(
        "/api/v1/settings/app-key/rotate",
        cookies={"plexmgr.session": session_cookie, "plexmgr.csrf": csrf_cookie},
        headers={"X-Api-Key": "stale-key", "X-CSRF-Token": csrf_cookie},
    )

    assert rotate.status_code == 200
    assert rotate.json()["app_api_key"]


@pytest.mark.asyncio
async def test_cookie_mutation_requires_csrf_but_api_key_does_not(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_settings(sessionmaker_)
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=_plex_oauth_transport())

    start = await client.post("/api/v1/auth/plex/start")
    complete = await client.post(
        "/api/v1/auth/plex/complete", json={"state": start.json()["state"]}
    )
    session_cookie = complete.cookies["plexmgr.session"]
    csrf_cookie = complete.cookies["plexmgr.csrf"]

    rejected = await client.put(
        "/api/v1/settings",
        cookies={"plexmgr.session": session_cookie, "plexmgr.csrf": csrf_cookie},
        json={"plex_url": "http://plex.local:32400"},
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "csrf_token_required"

    accepted = await client.put(
        "/api/v1/settings",
        cookies={"plexmgr.session": session_cookie, "plexmgr.csrf": csrf_cookie},
        headers={"X-CSRF-Token": csrf_cookie},
        json={"plex_url": "http://plex.local:32400"},
    )
    assert accepted.status_code == 200

    api_key = await client.put(
        "/api/v1/settings",
        headers={"X-Api-Key": _API_KEY},
        json={"plex_url": "http://plex.local:32400"},
    )
    assert api_key.status_code == 200


@pytest.mark.asyncio
async def test_logout_revokes_session_cookie(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_settings(sessionmaker_)
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=_plex_oauth_transport())

    start = await client.post("/api/v1/auth/plex/start")
    complete = await client.post(
        "/api/v1/auth/plex/complete", json={"state": start.json()["state"]}
    )
    session_cookie = complete.cookies["plexmgr.session"]
    csrf_cookie = complete.cookies["plexmgr.csrf"]

    logout = await client.post(
        "/api/v1/auth/logout",
        cookies={"plexmgr.session": session_cookie, "plexmgr.csrf": csrf_cookie},
        headers={"X-CSRF-Token": csrf_cookie},
    )
    assert logout.status_code == 204

    settings = await client.get("/api/v1/settings", cookies={"plexmgr.session": session_cookie})
    assert settings.status_code == 401


@pytest.mark.asyncio
async def test_shared_plex_account_gets_limited_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_settings(sessionmaker_)
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(
        transport=_plex_oauth_transport_for_resource(owned=False)
    )

    start = await client.post("/api/v1/auth/plex/start")
    complete = await client.post(
        "/api/v1/auth/plex/complete", json={"state": start.json()["state"]}
    )

    assert complete.status_code == 200
    body = complete.json()
    assert body["user"]["username"] == "guest"
    assert body["user"]["is_admin"] is False
    session_cookie = complete.cookies["plexmgr.session"]
    settings = await client.get("/api/v1/settings", cookies={"plexmgr.session": session_cookie})
    assert settings.status_code == 403
    assert settings.json()["detail"] == "admin_required"
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 99))).scalars().one()
    assert user.permissions == 0


@pytest.mark.asyncio
async def test_plex_account_without_configured_server_is_rejected(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_plex_settings(sessionmaker_)
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(
        transport=_plex_oauth_transport_for_resource(owned=False, machine_id="other-server")
    )

    start = await client.post("/api/v1/auth/plex/start")
    complete = await client.post(
        "/api/v1/auth/plex/complete", json={"state": start.json()["state"]}
    )

    assert complete.status_code == 403
    assert complete.json()["detail"] == "not_valid_plex_user"
    assert complete.cookies.get("plexmgr.session") is None
