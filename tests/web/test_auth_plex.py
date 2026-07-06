"""The single browser-token sign-in endpoint: ``POST /api/v1/auth/plex``.

The browser runs the plex.tv PIN flow itself and hands the resulting token to
this endpoint, which NEVER trusts the browser's claims: identity and server
ownership are re-derived server-side from plex.tv's v2 API before any user or
session row is written. Pre-init the first owner to sign in claims setup
(exclusively, via a CAS on ``system_settings.setup_started_at``); post-init an
account is admitted iff it has access to the configured server (admin iff it
owns it).

The plex.tv fixtures below mirror the real ``api/v2`` JSON payload shapes (a FLAT
``/api/v2/user`` object and a ``/api/v2/resources`` ARRAY), matching
``tests/adapters/plex/test_oauth.py`` so the router is exercised against the
shapes plex.tv actually serves.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import SystemSettings, User
from plex_manager.web import middleware
from plex_manager.web.deps import SettingsStore
from plex_manager.web.routers import auth as auth_module

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "s3cr3t-app-key"
_TOKEN = "browser-obtained-plex-token"  # noqa: S105 - fake token used by MockTransport tests
_MACHINE_ID = "abc123machine"


# --------------------------------------------------------------------------- #
# Order-independence + pre-init reachability shims
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_throttle() -> None:
    """Clear the in-process sign-in throttle so tests never leak attempt counts."""
    auth_module._reset_sign_in_throttle()


@pytest.fixture(autouse=True)
def _allow_auth_pre_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``/api/v1/auth`` reachable before the install is initialized.

    Task 6 adds ``/api/v1/auth`` to the real setup-guard allowlist; until it
    lands, shim it here so the pre-init sign-in tests reach the endpoint instead
    of the guard's 409 ``setup_required``. ``_is_allowed`` reads the module
    global at request time, so patching it takes effect for the already-built app.
    """
    monkeypatch.setattr(
        middleware,
        "SETUP_ALLOWLIST_PREFIXES",
        (*middleware.SETUP_ALLOWLIST_PREFIXES, "/api/v1/auth"),
    )


# --------------------------------------------------------------------------- #
# plex.tv v2 JSON fixtures (mirroring the real payload shapes)
# --------------------------------------------------------------------------- #
_OWNER_USER: dict[str, object] = {
    "id": 42,
    "uuid": "owner-uuid",
    "username": "plex-owner",
    "title": "plex-owner",
    "email": "owner@example.test",
    "thumb": "https://plex.tv/users/owner-uuid/avatar?c=1",
}

_SECOND_USER: dict[str, object] = {
    "id": 99,
    "uuid": "second-uuid",
    "username": "second-account",
    "title": "second-account",
    "email": "second@example.test",
}

_A_PLAYER: dict[str, object] = {
    "name": "A Player",
    "clientIdentifier": "player-1",
    "provides": "client,player",
    "owned": True,
    "connections": [],
}


def _owned_server(machine_id: str = _MACHINE_ID) -> dict[str, object]:
    return {
        "name": "Apollo",
        "product": "Plex Media Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [
            {
                "protocol": "https",
                "address": "203.0.113.7",
                "port": 32400,
                "uri": "https://203-0-113-7.abc.plex.direct:32400",
                "local": False,
                "relay": False,
            }
        ],
    }


def _shared_server(machine_id: str = _MACHINE_ID) -> dict[str, object]:
    return {
        "name": "SomeoneElses",
        "product": "Plex Media Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": False,
        "connections": [],
    }


def _plex_tv_transport(
    *,
    user: dict[str, object],
    resources: list[dict[str, object]],
    identity: str | None = None,
    seen: list[str] | None = None,
) -> httpx.MockTransport:
    """A MockTransport answering the plex.tv v2 endpoints (and optional /identity)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        host = request.url.host
        path = request.url.path
        if host == "plex.tv" and path == "/api/v2/user":
            assert request.headers.get("X-Plex-Token")  # the submitted token is forwarded
            return httpx.Response(200, json=user)
        if host == "plex.tv" and path == "/api/v2/resources":
            assert request.headers.get("X-Plex-Token")
            return httpx.Response(200, json=resources)
        if path == "/identity":
            if identity is None:
                raise AssertionError(f"unexpected /identity request: {request.url}")
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": identity}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def _use_transport(app: FastAPI, transport: httpx.MockTransport) -> None:
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=transport)


async def _store_setting(sessionmaker_: SessionMaker, key: str, value: str) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set(key, value)
        await session.commit()


# --------------------------------------------------------------------------- #
# Pre-init: exclusive first claim
# --------------------------------------------------------------------------- #
async def test_pre_init_owner_claims_admin_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=False)
    await _use_transport(
        app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server(), _A_PLAYER])
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["auth_method"] == "plex_session"
    assert body["is_admin"] is True
    assert body["user"]["plex_id"] == 42
    assert body["user"]["is_admin"] is True
    assert response.cookies.get("plexmgr.session")
    assert response.cookies.get("plexmgr.csrf")

    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        system = (await db.execute(select(SystemSettings))).scalars().one()
    assert user.permissions == 1
    # The verified token is persisted on the user row (EncryptedStr at rest).
    assert user.encrypted_plex_token == _TOKEN
    assert system.setup_started_at is not None


async def test_pre_init_no_owned_servers_rejected(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=False)
    # Only a shared (not-owned) server and a player — nothing this account OWNS.
    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_shared_server("other"), _A_PLAYER])
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 403
    assert response.json()["detail"] == "no_owned_servers"
    assert response.cookies.get("plexmgr.session") is None


async def test_pre_init_second_different_account_after_claim_rejected(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=False)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    first = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert first.status_code == 200

    # A DIFFERENT owner account signs in; the exclusive claim is already taken.
    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_owned_server("second-machine")])
    )
    second = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert second.status_code == 403
    assert second.json()["detail"] == "setup_already_claimed"
    assert second.cookies.get("plexmgr.session") is None


async def test_pre_init_same_account_resumes(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=False)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    first = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert first.status_code == 200
    # The SAME owner signing in again resumes the claim rather than being locked out.
    second = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert second.status_code == 200
    assert second.json()["user"]["is_admin"] is True


async def test_pre_init_concurrent_claim_loser_rejected(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=False)
    # Simulate the winning worker's claim landing first: setup_started_at already
    # stamped when a DIFFERENT account's request runs the CAS.
    async with sessionmaker_() as db:
        system = (await db.execute(select(SystemSettings))).scalars().one()
        system.setup_started_at = datetime.now(UTC)
        await db.commit()

    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_owned_server("second-machine")])
    )
    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 403
    assert response.json()["detail"] == "setup_already_claimed"


# --------------------------------------------------------------------------- #
# Post-init: server-access gate
# --------------------------------------------------------------------------- #
async def test_post_init_owner_gets_admin_and_skips_identity(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    seen: list[str] = []
    await _use_transport(
        app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()], seen=seen)
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True
    # A stored machine identifier means the backend never re-probes /identity.
    assert "/identity" not in seen


async def test_post_init_shared_gets_limited_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_SECOND_USER, resources=[_shared_server()]))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is False
    # The client jar carries the freshly-minted session cookie into the next call.
    settings = await client.get("/api/v1/settings")
    assert settings.status_code == 403
    assert settings.json()["detail"] == "admin_required"

    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 99))).scalars().one()
    assert user.permissions == 0


async def test_post_init_no_access_rejected(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    # The account owns a server, but not the configured one.
    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_owned_server("different-machine")])
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 403
    assert response.json()["detail"] == "server_access_denied"
    assert response.cookies.get("plexmgr.session") is None


async def test_post_init_falls_back_to_identity_when_machine_id_unset(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    # A pre-rework DB has no stored machine id, only the Plex url/token.
    await _store_setting(sessionmaker_, "plex_url", "http://plex.local:32400")
    await _store_setting(sessionmaker_, "plex_token", "service-token")
    seen: list[str] = []
    await _use_transport(
        app,
        _plex_tv_transport(
            user=_OWNER_USER, resources=[_owned_server()], identity=_MACHINE_ID, seen=seen
        ),
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True
    assert "/identity" in seen


async def test_post_init_service_not_configured_when_nothing_stored(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    # Neither a stored machine id nor a configured Plex url/token to probe.
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 409
    assert response.json()["detail"] == "service_not_configured"


# --------------------------------------------------------------------------- #
# plex.tv verification failures render the envelope (never a bare 500)
# --------------------------------------------------------------------------- #
async def test_plex_token_invalid_surfaces_envelope(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(401, json={"error": "unauthorized"})
        raise AssertionError(f"unexpected request: {request.url}")

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 502
    assert response.json()["detail"] == "plex_token_invalid"
    assert _TOKEN not in response.text
    assert response.cookies.get("plexmgr.session") is None


async def test_plex_tv_unreachable_surfaces_envelope(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 502
    assert response.json()["detail"] == "plex_tv_unreachable_server"
    assert _TOKEN not in response.text


async def test_error_body_never_leaks_token(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """No error path echoes the submitted token anywhere in the response body."""
    await seed(initialized=False)
    distinctive = "DISTINCTIVE-do-not-leak-4f2a"
    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_shared_server("other")])
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": distinctive})

    assert response.status_code == 403
    assert response.json()["detail"] == "no_owned_servers"
    assert distinctive not in response.text


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
async def test_sign_in_throttled_after_limit(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for _ in range(10):
        ok = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
        assert ok.status_code == 200
    throttled = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert throttled.status_code == 429
    assert throttled.json()["detail"] == "sign_in_throttled"


# --------------------------------------------------------------------------- #
# /me and /logout behavior preserved
# --------------------------------------------------------------------------- #
async def test_me_unauthenticated(client: httpx.AsyncClient, app: FastAPI, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json()["authenticated"] is False


async def test_me_authenticated_after_sign_in(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    signin = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert signin.status_code == 200
    me = await client.get("/api/v1/auth/me")  # jar carries the session cookie

    assert me.status_code == 200
    body = me.json()
    assert body["authenticated"] is True
    assert body["auth_method"] == "plex_session"
    assert body["is_admin"] is True
    assert body["user"]["plex_id"] == 42


async def test_logout_revokes_session_cookie(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    signin = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    session_cookie = signin.cookies["plexmgr.session"]
    csrf_cookie = signin.cookies["plexmgr.csrf"]

    # The jar carries session + CSRF cookies; the header echoes the CSRF token.
    logout = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_cookie})
    assert logout.status_code == 204

    # Replay the now server-side-revoked session token: rejected on the merits,
    # not merely because logout cleared the cookie from the jar.
    client.cookies.set("plexmgr.session", session_cookie)
    settings = await client.get("/api/v1/settings")
    assert settings.status_code == 401


async def test_openapi_advertises_both_cookie_and_apikey_auth(app: FastAPI) -> None:
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["APIKeyCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "plexmgr.session",
    }
    # A protected route advertises EITHER credential — a LIST of requirement
    # objects is OpenAPI's OR; a single object would mean both are required.
    logout_security = schema["paths"]["/api/v1/auth/logout"]["post"]["security"]
    assert {"APIKeyHeader": []} in logout_security
    assert {"APIKeyCookie": []} in logout_security
