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

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.models import AuthSession, LogEvent, Setting, SystemSettings, User
from plex_manager.services import log_capture_service
from plex_manager.web import deps
from plex_manager.web.deps import (
    PLEX_MACHINE_ID_SETTING,
    SETUP_TOKEN_HEADER_NAME,
    SettingsStore,
)
from plex_manager.web.errors import AppError
from plex_manager.web.events import get_event_hub
from plex_manager.web.routers import auth as auth_module
from plex_manager.web.routers import ops as ops_router
from plex_manager.web.routers import settings as settings_router
from tests.support import assert_task_raises

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "s3cr3t-app-key"
_TOKEN = "browser-obtained-plex-token"  # noqa: S105 - fake token used by MockTransport tests
_MACHINE_ID = "abc123machine"


# --------------------------------------------------------------------------- #
# Order-independence
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def reset_throttle() -> None:
    """Clear the in-process sign-in throttle so tests never leak attempt counts."""
    auth_module.reset_sign_in_throttle()


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
# Pre-init: the optional PLEX_MANAGER_SETUP_TOKEN gates the FIRST-OWNER CLAIM
# --------------------------------------------------------------------------- #
async def test_pre_init_setup_token_gates_the_claim(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured setup token must be required BEFORE the exclusive claim.

    Without this, any owner of any Plex server could win the pre-init claim and
    permanently lock out the true owner. The token gates completion AND the claim.
    """
    await seed(initialized=False)
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
    get_settings.cache_clear()
    # A legitimate-looking owner token: the claim would succeed if the gate were
    # absent. The plex.tv transport must never be reached — the token check fires
    # before any account/resource fetch — so a hit here fails the test.
    seen: list[str] = []
    await _use_transport(
        app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()], seen=seen)
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_setup_token"
    assert response.cookies.get("plexmgr.session") is None
    # The gate short-circuits before any plex.tv call: no proxying for a caller
    # who cannot prove the setup token.
    assert seen == []
    # The claim never happened: no owner row, setup_started_at still unstamped.
    async with sessionmaker_() as db:
        assert (await db.execute(select(User).where(User.plex_id == 42))).scalars().first() is None
        system = (await db.execute(select(SystemSettings))).scalars().one()
    assert system.setup_started_at is None


async def test_pre_init_setup_token_valid_allows_the_claim(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the matching X-Setup-Token, the owner claims setup as normal."""
    await seed(initialized=False)
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
    get_settings.cache_clear()
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    response = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={SETUP_TOKEN_HEADER_NAME: "boot-token"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True
    async with sessionmaker_() as db:
        system = (await db.execute(select(SystemSettings))).scalars().one()
    assert system.setup_started_at is not None


async def test_post_init_ignores_setup_token(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-init the token is never consulted: sign-in works without it.

    The token hardens ONLY the pre-init window; a configured token must not brick
    ordinary post-init sign-in (which carries no X-Setup-Token).
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
    get_settings.cache_clear()
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True


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


async def test_sign_in_demotion_closes_that_users_realtime_streams(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #183: a sign-in that DEMOTES an account closes its open streams.

    An admin SSE stream captures its admin subscription at connect time and would
    keep delivering admin-only topics until disconnect. When the same account
    signs in again but no longer owns the server (admin -> non-admin), the open
    stream must be closed the moment ``_upsert_user`` observes the downgrade.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    # First sign-in: the owner is an admin.
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    first = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert first.status_code == 200
    assert first.json()["user"]["is_admin"] is True
    user_id = first.json()["user"]["id"]

    # An admin SSE stream is open for this user.
    subscription = get_event_hub(app).subscribe(auth_method="plex_session", user_id=user_id)
    assert subscription.closed is False

    # The SAME account signs in again but now only has SHARED (not owned) access:
    # admin -> non-admin. The open stream must be closed.
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_shared_server()]))
    second = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert second.status_code == 200
    assert second.json()["user"]["is_admin"] is False
    assert subscription.closed is True


async def test_sign_in_demotion_closes_streams_after_commit(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The demotion close must run AFTER the demotion commits (issue #56).

    ``_upsert_user`` only stages the ``permissions`` write; ``_issue_browser_session``
    commits it. If the stream close fired before that commit, a fast reconnect
    could re-read the old admin permissions and resubscribe to admin topics with
    no second close. We assert the ordering directly: the commit happens strictly
    before the close, so no admin stream can survive the downgrade.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    first = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert first.status_code == 200
    user_id = first.json()["user"]["id"]

    subscription = get_event_hub(app).subscribe(auth_method="plex_session", user_id=user_id)

    order: list[str] = []
    real_issue = auth_module._issue_browser_session  # pyright: ignore[reportPrivateUsage]

    async def spy_issue(*args: object, **kwargs: object) -> None:
        await real_issue(*args, **kwargs)  # type: ignore[arg-type]
        order.append("commit")

    real_close = auth_module.close_realtime_streams

    def spy_close(*args: object, **kwargs: object) -> None:
        order.append("close")
        real_close(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_module, "_issue_browser_session", spy_issue)
    monkeypatch.setattr(auth_module, "close_realtime_streams", spy_close)

    # The SAME account signs in again with only SHARED access: admin -> non-admin.
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_shared_server()]))
    second = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert second.status_code == 200
    assert second.json()["user"]["is_admin"] is False
    assert subscription.closed is True
    # Commit-before-close: the demotion is persisted before any stream is torn down.
    assert order == ["commit", "close"]

    # And the demotion is durably committed (a fresh session reads permissions 0).
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalars().one()
    assert user.permissions == 0


async def test_sign_in_without_demotion_keeps_streams_open(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A re-sign-in that does NOT lower permissions leaves open streams alone.

    The guard is a permission DECREASE, not any write: an owner signing in again
    (still admin) must not tear down their own live stream.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    first = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert first.status_code == 200
    user_id = first.json()["user"]["id"]

    subscription = get_event_hub(app).subscribe(auth_method="plex_session", user_id=user_id)
    # Still an owner on the second sign-in — admin stays admin.
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    second = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert second.status_code == 200
    assert second.json()["user"]["is_admin"] is True
    assert subscription.closed is False


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


def _throttle_request(host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/plex",
            "headers": [],
            "client": (host, 12345),
            "scheme": "http",
        }
    )


# --------------------------------------------------------------------------- #
# Cookie security
# --------------------------------------------------------------------------- #
def _cookie_header(headers: list[str], name: str) -> str:
    return next(header for header in headers if header.startswith(f"{name}="))


async def _sign_in_for_cookie_test(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> httpx.Response:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert response.status_code == 200
    return response


async def test_cookie_secure_unset_ignores_forwarded_https_on_direct_http(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins APP-level behavior: the application never reads X-Forwarded-Proto, so
    # with the override unset the Secure flag follows the ASGI scope's scheme.
    # The server layer is out of scope here — httpx.ASGITransport bypasses
    # uvicorn's ProxyHeadersMiddleware, which (for trusted peers, loopback by
    # default) rewrites the scope scheme from this header before the app sees it.
    monkeypatch.delenv("PLEX_MANAGER_AUTH_COOKIE_SECURE", raising=False)
    get_settings.cache_clear()
    client.headers["X-Forwarded-Proto"] = "https"
    response = await _sign_in_for_cookie_test(app, client, seed, sessionmaker_)

    headers = response.headers.get_list("set-cookie")
    assert "Secure" not in _cookie_header(headers, "plexmgr.session")
    assert "Secure" not in _cookie_header(headers, "plexmgr.csrf")


async def test_cookie_secure_follows_https_scheme_when_unset(
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PLEX_MANAGER_AUTH_COOKIE_SECURE", raising=False)
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://localhost") as client:
        response = await _sign_in_for_cookie_test(app, client, seed, sessionmaker_)

    headers = response.headers.get_list("set-cookie")
    assert "Secure" in _cookie_header(headers, "plexmgr.session")
    assert "Secure" in _cookie_header(headers, "plexmgr.csrf")


async def test_cookie_secure_true_overrides_http_scheme(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_AUTH_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    response = await _sign_in_for_cookie_test(app, client, seed, sessionmaker_)

    headers = response.headers.get_list("set-cookie")
    assert "Secure" in _cookie_header(headers, "plexmgr.session")
    assert "Secure" in _cookie_header(headers, "plexmgr.csrf")


async def test_cookie_secure_false_overrides_https_scheme(
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_AUTH_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://localhost") as client:
        response = await _sign_in_for_cookie_test(app, client, seed, sessionmaker_)

    headers = response.headers.get_list("set-cookie")
    assert "Secure" not in _cookie_header(headers, "plexmgr.session")
    assert "Secure" not in _cookie_header(headers, "plexmgr.csrf")


async def test_logout_cookie_deletion_matches_explicit_secure_setting(
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_AUTH_COOKIE_SECURE", "true")
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://localhost"
    ) as secure_client:
        signin = await _sign_in_for_cookie_test(app, secure_client, seed, sessionmaker_)
        logout = await secure_client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": signin.cookies["plexmgr.csrf"]},
        )
    assert logout.status_code == 204
    headers = logout.headers.get_list("set-cookie")
    assert "Secure" in _cookie_header(headers, "plexmgr.session")
    assert "Secure" in _cookie_header(headers, "plexmgr.csrf")
    assert "Max-Age=0" in _cookie_header(headers, "plexmgr.session")
    assert "Max-Age=0" in _cookie_header(headers, "plexmgr.csrf")


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
def test_sign_in_throttle_evicts_stale_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now)
    auth_module._throttle_sign_in(_throttle_request("198.51.100.1"))  # pyright: ignore[reportPrivateUsage]

    now += auth_module._SIGN_IN_WINDOW_SECONDS + 1  # pyright: ignore[reportPrivateUsage]
    auth_module._throttle_sign_in(_throttle_request("198.51.100.2"))  # pyright: ignore[reportPrivateUsage]

    assert "198.51.100.1" not in auth_module._sign_in_attempts  # pyright: ignore[reportPrivateUsage]
    assert "198.51.100.2" in auth_module._sign_in_attempts  # pyright: ignore[reportPrivateUsage]


def test_sign_in_throttle_preserves_active_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now)
    request = _throttle_request("198.51.100.1")
    for _ in range(10):
        auth_module._throttle_sign_in(request)  # pyright: ignore[reportPrivateUsage]

    now += auth_module._SIGN_IN_WINDOW_SECONDS - 1  # pyright: ignore[reportPrivateUsage]
    auth_module._throttle_sign_in(_throttle_request("198.51.100.2"))  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(AppError) as error:
        auth_module._throttle_sign_in(request)  # pyright: ignore[reportPrivateUsage]

    assert error.value.code == "sign_in_throttled"
    assert "198.51.100.1" in auth_module._sign_in_attempts  # pyright: ignore[reportPrivateUsage]


def test_sign_in_throttle_allows_new_window_after_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now)
    request = _throttle_request("198.51.100.1")
    for _ in range(10):
        auth_module._throttle_sign_in(request)  # pyright: ignore[reportPrivateUsage]

    now += auth_module._SIGN_IN_WINDOW_SECONDS + 1  # pyright: ignore[reportPrivateUsage]
    auth_module._throttle_sign_in(request)  # pyright: ignore[reportPrivateUsage]
    assert auth_module._sign_in_attempts["198.51.100.1"] == [now]  # pyright: ignore[reportPrivateUsage]


def test_sign_in_throttle_does_not_evict_live_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now)
    for index in range(100):
        auth_module._throttle_sign_in(_throttle_request(f"198.51.100.{index}"))  # pyright: ignore[reportPrivateUsage]

    assert len(auth_module._sign_in_attempts) == 100  # pyright: ignore[reportPrivateUsage]


def test_sign_in_throttle_eviction_scan_runs_at_most_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: now)
    auth_module._throttle_sign_in(_throttle_request("198.51.100.1"))  # pyright: ignore[reportPrivateUsage]
    assert auth_module._last_stale_key_eviction == 100.0  # pyright: ignore[reportPrivateUsage]

    now += auth_module._SIGN_IN_WINDOW_SECONDS - 1  # pyright: ignore[reportPrivateUsage]
    auth_module._throttle_sign_in(_throttle_request("198.51.100.2"))  # pyright: ignore[reportPrivateUsage]
    # Inside the cadence: the full-dict scan did not rerun.
    assert auth_module._last_stale_key_eviction == 100.0  # pyright: ignore[reportPrivateUsage]

    now += auth_module._SIGN_IN_WINDOW_SECONDS - 1  # pyright: ignore[reportPrivateUsage]
    auth_module._throttle_sign_in(_throttle_request("198.51.100.3"))  # pyright: ignore[reportPrivateUsage]
    assert auth_module._last_stale_key_eviction == now  # pyright: ignore[reportPrivateUsage]
    # The rescan reclaimed the key that aged out while the cadence held it back.
    assert "198.51.100.1" not in auth_module._sign_in_attempts  # pyright: ignore[reportPrivateUsage]
    assert "198.51.100.2" in auth_module._sign_in_attempts  # pyright: ignore[reportPrivateUsage]


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


async def test_sign_in_throttle_default_ignores_x_forwarded_for(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """``trusted_proxy_hops`` defaults to 0: a caller-supplied X-Forwarded-For must
    NOT carve out extra budget beyond the direct-peer throttle."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for i in range(10):
        ok = await client.post(
            "/api/v1/auth/plex",
            json={"auth_token": _TOKEN},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
        assert ok.status_code == 200
    throttled = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )

    assert throttled.status_code == 429


async def test_sign_in_throttle_trusted_hop_keys_on_forwarded_client(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``trusted_proxy_hops=1``, each distinct forwarded client gets its own
    budget -- fixing the global-cap risk of keying on ``request.client.host`` (the
    reverse proxy's own address) alone."""
    monkeypatch.setenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for _ in range(10):
        ok = await client.post(
            "/api/v1/auth/plex",
            json={"auth_token": _TOKEN},
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
        assert ok.status_code == 200
    throttled = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": "203.0.113.5"},
    )
    assert throttled.status_code == 429

    # A DIFFERENT forwarded client is unaffected -- its own budget, not the proxy's.
    other = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert other.status_code == 200

    # No X-Forwarded-For header at all still falls back to the direct peer rather
    # than raising -- a fresh budget, since nothing above hit that key.
    absent = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert absent.status_code == 200


async def test_sign_in_throttle_trusted_hop_falls_back_on_short_header(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A header shorter than the configured hop count cannot be trusted -- fall back
    to the direct peer rather than indexing into an attacker-shaped header."""
    monkeypatch.setenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", "2")
    get_settings.cache_clear()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for i in range(10):
        ok = await client.post(
            "/api/v1/auth/plex",
            json={"auth_token": _TOKEN},
            # Only ONE entry while hops=2 -- too short to trust, so every request
            # still falls back to (and shares) the direct-peer key.
            headers={"X-Forwarded-For": f"198.51.100.{i}"},
        )
        assert ok.status_code == 200
    throttled = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": "198.51.100.250"},
    )

    assert throttled.status_code == 429


async def test_sign_in_throttle_ignores_blank_entries_outside_trusted_suffix(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank/malformed entry in the CLIENT-CONTROLLED portion of the header (to
    the left of the trusted suffix) must not collapse every forwarded client onto
    the shared direct-peer key -- only the trusted suffix's own well-formedness
    gates the fallback. Otherwise an attacker could send a distinct blank-entry
    header on every request and force everyone behind the proxy to share one
    budget, recreating the exact global-cap lockout this throttle key exists to
    prevent."""
    monkeypatch.setenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for _ in range(10):
        ok = await client.post(
            "/api/v1/auth/plex",
            json={"auth_token": _TOKEN},
            # A blank leading (client-controlled) entry ahead of the proxy's own
            # trusted-suffix entry -- must still key on the trusted "203.0.113.5".
            headers={"X-Forwarded-For": ", 203.0.113.5"},
        )
        assert ok.status_code == 200
    throttled = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": ", 203.0.113.5"},
    )
    assert throttled.status_code == 429

    # A DIFFERENT trusted client, still preceded by a blank entry, has its own
    # budget rather than colliding on a shared fallback key.
    other = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": ", 203.0.113.10"},
    )
    assert other.status_code == 200


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


# --------------------------------------------------------------------------- #
# First-sign-in races: two concurrent racers on a clean DB must both succeed
# --------------------------------------------------------------------------- #
async def test_concurrent_client_id_create_race_loser_reuses_winner_id(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two simultaneous first-ever POST /auth/plex on a clean DB both observe no
    ``plex_oauth_client_identifier`` and both mint a candidate; ``settings.key``
    is UNIQUE, so only one can persist. The loser must complete its sign-in under
    the WINNER's device identity — never a 500, never a second identifier, and
    (the store-retry x create-once composition hazard) never an OVERWRITE that
    rotates the winner's committed identifier out from under it. This pins the
    BEHAVIOR, not the recovery mechanism (which lives in
    ``SettingsStore.set_if_absent``): the loser's initial row lookup is stubbed
    to MISS once — as it genuinely would mid-race, before the winner's commit
    became visible — so its INSERT collides on the real unique index."""
    await seed(initialized=False)
    # The WINNER's identifier is committed (its request got there first).
    await _store_setting(sessionmaker_, "plex_oauth_client_identifier", "winner-client-id")

    real_row = SettingsStore._row  # pyright: ignore[reportPrivateUsage]
    missed = {"n": 0}

    async def racing_row(self: SettingsStore, key: str) -> Setting | None:
        # The loser's FIRST lookup ran before the winner committed: miss once.
        if key == "plex_oauth_client_identifier" and missed["n"] == 0:
            missed["n"] = 1
            return None
        return await real_row(self, key)

    monkeypatch.setattr(SettingsStore, "_row", racing_row)

    # Capture the device identifier every plex.tv call actually carried.
    used_identifiers: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        used_identifiers.append(request.headers.get("X-Plex-Client-Identifier"))
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_OWNER_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_owned_server()])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200  # the loser signs in, never a 500
    assert response.json()["user"]["is_admin"] is True
    # Every plex.tv call ran under the WINNER's identifier — the loser adopted it.
    assert used_identifiers and set(used_identifiers) == {"winner-client-id"}
    async with sessionmaker_() as db:
        stored = await SettingsStore(db).get("plex_oauth_client_identifier")
    assert stored == "winner-client-id"  # the winner's identifier survived intact


async def test_concurrent_same_account_first_sign_in_yields_one_user_two_sessions(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent FIRST-TIME post-init sign-ins for the SAME Plex account both
    pass the no-row lookup and both INSERT; ``users.plex_id`` is UNIQUE, so the
    loser's flush raises ``IntegrityError``. The loser must recover onto the
    winner's row and still mint its session: both browsers end up signed in, ONE
    user row exists. Simulated like the request-dedup race tests: the winner's
    sign-in commits first, then the loser's first lookup is forced to MISS (as it
    would mid-race) so its INSERT genuinely collides on the unique index."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    winner = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert winner.status_code == 200

    real_find = auth_module.find_user_by_plex_id
    missed = {"n": 0}

    async def racing_find(session: AsyncSession, plex_id: int) -> User | None:
        # The loser looked BEFORE the winner committed: miss once, then real.
        if missed["n"] == 0:
            missed["n"] = 1
            return None
        return await real_find(session, plex_id)

    monkeypatch.setattr(auth_module, "find_user_by_plex_id", racing_find)
    client.cookies.clear()  # the loser is a separate browser, not the winner's session

    loser = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert loser.status_code == 200  # never a 500 — the same account merely raced itself
    assert loser.cookies.get("plexmgr.session")
    assert missed["n"] == 1  # the race path (miss -> collide -> recover) actually ran
    async with sessionmaker_() as db:
        users = (await db.execute(select(User).where(User.plex_id == 42))).scalars().all()
        assert len(users) == 1  # ONE shared user row — the loser adopted the winner's
        assert users[0].permissions == 1  # the recovery re-applied the admin decision
        session_rows = (
            (await db.execute(select(AuthSession).where(AuthSession.user_id == users[0].id)))
            .scalars()
            .all()
        )
    assert len(session_rows) == 2  # BOTH racers hold a live session


# --------------------------------------------------------------------------- #
# Recovery-key exchange: POST /auth/api-key trades X-Api-Key for the SAME cookie
# (CodeQL #263 — the key never needs JS-readable storage)
# --------------------------------------------------------------------------- #
async def test_api_key_exchange_mints_session_cookie(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A valid recovery key is exchanged for the same HTTP-only session cookie the
    Plex flow issues — an admin recovery session with NO owning user."""
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["auth_method"] == "api_key"
    assert body["is_admin"] is True
    assert body["user"] is None  # no Plex identity backs the recovery session
    assert response.cookies.get("plexmgr.session")
    assert response.cookies.get("plexmgr.csrf")

    # The minted row is a genuine recovery session: user_id NULL, not revoked.
    async with sessionmaker_() as db:
        rows = (await db.execute(select(AuthSession))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id is None
    assert rows[0].revoked_at is None


async def test_api_key_exchange_cookie_authenticates_later_requests(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """After the exchange the browser needs NO X-Api-Key header: the cookie in the
    jar authenticates the admin-only settings read on its own."""
    await seed(initialized=True, app_api_key=_API_KEY)

    exchange = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})
    assert exchange.status_code == 200

    # No X-Api-Key header — the session cookie alone carries the request.
    settings = await client.get("/api/v1/settings")
    assert settings.status_code == 200

    me = await client.get("/api/v1/auth/me")
    assert me.json()["auth_method"] == "api_key"
    assert me.json()["is_admin"] is True


async def test_api_key_exchange_session_enforces_csrf_on_unsafe_methods(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """The recovery session is a COOKIE credential, so unsafe methods carry the
    double-submit CSRF check exactly like a Plex session — a missing token is a
    403, the echoed CSRF cookie succeeds (logout is the handy unsafe endpoint)."""
    await seed(initialized=True, app_api_key=_API_KEY)

    exchange = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})
    csrf_cookie = exchange.cookies["plexmgr.csrf"]

    # The jar carries the session + CSRF cookies but no X-CSRF-Token header.
    blocked = await client.post("/api/v1/auth/logout")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "csrf_token_required"

    ok = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_cookie})
    assert ok.status_code == 204


async def test_api_key_exchange_wrong_key_rejected_no_cookie(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": "nope"})

    assert response.status_code == 401
    # A DISTINCT code from the expired-session 401 (issue #293 finding 2): the SPA
    # branches on it so a mistyped recovery key does not trip the global "session
    # expired -> bounce to Plex login" handler.
    assert response.json()["detail"] == "recovery_key_rejected"
    assert response.cookies.get("plexmgr.session") is None


async def test_api_key_exchange_missing_key_rejected(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post("/api/v1/auth/api-key")

    assert response.status_code == 401
    assert response.json()["detail"] == "recovery_key_rejected"


async def test_api_key_exchange_does_not_accept_plex_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A non-owner Plex session must NOT be able to mint an ADMIN recovery session
    by calling the exchange with no key: the endpoint validates the recovery key
    specifically, never falling back to whatever cookie is already in the jar.

    This is the RUNTIME half of a two-test pair (issue #380 finding 1, PR #398
    review): it pins observed behavior end-to-end, including any auth performed
    INSIDE the endpoint body (a hand-rolled ``authenticate_request(...)`` call or
    a direct ``request.cookies`` read would slip past the dependency-graph shape
    test below, whose view ends at the declared dependency surface). Its known
    limitation is the converse: on today's implementation the cookie is inert by
    construction, so this test's outcome is indistinguishable from
    ``test_api_key_exchange_missing_key_rejected`` -- it only gains teeth the
    moment someone wires cookie auth in. The shape test covers that gap from the
    structural side; together they close both regression paths."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    # A shared (non-owner) account signs in: it holds a limited (non-admin) session.
    await _use_transport(app, _plex_tv_transport(user=_SECOND_USER, resources=[_shared_server()]))
    signin = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert signin.json()["is_admin"] is False

    # With that non-admin session cookie in the jar but NO recovery key header, the
    # exchange must refuse rather than hand out an admin recovery session.
    response = await client.post("/api/v1/auth/api-key")
    assert response.status_code == 401
    assert response.json()["detail"] == "recovery_key_rejected"


async def test_api_key_exchange_dependency_shape_forbids_cookie_auth() -> None:
    """A signed-in caller's Plex session cookie must NEVER be able to mint an ADMIN
    recovery session by calling the exchange with no key (issue #380 finding 1).

    The STRUCTURAL half of the pair with
    ``test_api_key_exchange_does_not_accept_plex_session`` above. On today's
    implementation the exchange's ``provided`` parameter is sourced exclusively
    via ``Depends(api_key_header)`` -- an ``APIKeyHeader`` security scheme
    FastAPI resolves ONLY from ``request.headers``, never ``request.cookies`` --
    so a cookie in the jar is inert BY CONSTRUCTION and the behavioral test
    alone cannot distinguish "cookie refused" from "cookie never consulted".

    This pins that construction directly: nothing that can resolve an
    ``AuthContext`` from a session cookie (``authenticate_request`` /
    ``require_api_key`` / ``require_admin``) is reachable anywhere in this
    route's FastAPI dependant graph. It catches the regression the endpoint's
    docstring warns against -- wiring in ``require_api_key`` (as every
    cookie-or-key protected route does) so a signed-in NON-admin could mint an
    ADMIN recovery session -- even in variants that keep a cookie-less request's
    401/``recovery_key_rejected`` response unchanged. Its own blind spot -- auth
    performed inside the endpoint BODY, invisible to the dependant graph -- is
    exactly what the runtime test above exists to catch.
    """
    (route,) = [
        r
        for r in auth_module.router.routes
        if isinstance(r, APIRoute)
        and r.path == "/api/v1/auth/api-key"
        and r.methods is not None
        and "POST" in r.methods
    ]

    def collect_calls(dependant: object, seen: set[object]) -> set[object]:
        # `Dependant` from fastapi.dependencies.models; typed loosely here since the
        # attribute isn't part of FastAPI's public type surface. Walks the
        # SUB-dependency list only -- ``dependant`` itself is the endpoint function,
        # not one of its dependencies.
        for sub in getattr(dependant, "dependencies", []):
            call = getattr(sub, "call", None)
            if call is not None:
                seen.add(call)
            collect_calls(sub, seen)
        return seen

    calls = collect_calls(route.dependant, set())
    # The ONLY two things this route depends on: the DB session and the
    # header-sourced api-key scheme. No cookie-reading function is anywhere in it.
    assert calls == {get_session, auth_module.api_key_header}
    assert auth_module.authenticate_request not in calls
    assert auth_module.require_api_key not in calls
    assert auth_module.require_admin not in calls


async def test_api_key_exchange_throttled_after_limit(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """The exchange shares the sign-in throttle: another session-minting write on
    the unauthenticated surface, braked the same way."""
    await seed(initialized=True, app_api_key=_API_KEY)

    for _ in range(10):
        ok = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})
        assert ok.status_code == 200
    throttled = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})

    assert throttled.status_code == 429
    assert throttled.json()["detail"] == "sign_in_throttled"


async def test_openapi_advertises_both_cookie_and_apikey_auth(app: FastAPI) -> None:
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["APIKeyCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "plexmgr.session",
    }
    assert schemes["CSRFHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-CSRF-Token",
    }
    # A protected route advertises EITHER credential — a LIST of requirement
    # objects is OpenAPI's OR; a single object would mean both are required.
    logout_security = schema["paths"]["/api/v1/auth/logout"]["post"]["security"]
    assert {"APIKeyHeader": []} in logout_security
    assert {"APIKeyCookie": [], "CSRFHeader": []} in logout_security

    # Safe cookie-auth operations do not need CSRF.
    queue_security = schema["paths"]["/api/v1/queue"]["get"]["security"]
    assert {"APIKeyCookie": []} in queue_security


async def test_openapi_declares_api_key_header_on_recovery_exchange(app: FastAPI) -> None:
    """The recovery-key exchange sources ``X-Api-Key`` via the shared
    ``APIKeyHeader`` dependency (issue #293 finding 5), so the endpoint advertises
    the requirement in OpenAPI — a raw ``Request.headers.get`` left the contract
    silent and generated clients would omit the key and hit an undocumented 401.

    It is HEADER-ONLY (issue #293 P2): the endpoint deliberately refuses the session
    cookie (honouring it would let a non-admin mint an ADMIN recovery session), so the
    app-wide cookie-security rewrite MUST leave this operation alone. The published
    contract advertises ONLY ``APIKeyHeader`` — never the ``APIKeyCookie``/``CSRFHeader``
    OR that every other unsafe operation gets.
    """
    schema = app.openapi()
    security = schema["paths"]["/api/v1/auth/api-key"]["post"]["security"]
    assert security == [{"APIKeyHeader": []}]
    flattened = {scheme for requirement in security for scheme in requirement}
    assert "APIKeyCookie" not in flattened
    assert "CSRFHeader" not in flattened


# --------------------------------------------------------------------------- #
# Token rotation redaction (issue #374, ADR-0026): replacing a user's stored
# Plex token erases the OLD value from durable log history, the drain queue,
# and the live ring inside the same locked transactional boundary every other
# secret mutation uses.
# --------------------------------------------------------------------------- #
_OLD_PLEX_TOKEN = "old-plex-token-being-rotated"  # noqa: S105 - fixture credential
_NEW_PLEX_TOKEN = "new-plex-token-after-rotation"  # noqa: S105 - fixture credential


class _ObservableLock(asyncio.Lock):
    """Event-observable lock used to prove the real shared-lock ordering."""

    def __init__(self) -> None:
        super().__init__()
        self.acquire_count = 0
        self.second_acquire_started = asyncio.Event()
        self.releases: asyncio.Queue[None] = asyncio.Queue()

    async def acquire(self) -> Literal[True]:
        self.acquire_count += 1
        if self.acquire_count == 2:
            self.second_acquire_started.set()
        return await super().acquire()

    def release(self) -> None:
        super().release()
        self.releases.put_nowait(None)


class _StopDrainLoop(Exception):
    """End one exercised drain tick without a real-time sleep."""


async def _wait_for_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=5.0)


async def _run_one_drain(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    entered_drain: asyncio.Event,
    release_drain: asyncio.Event,
) -> None:
    from plex_manager.web import app as app_module

    real_drain_once = app_module.log_capture_service.drain_once

    async def paused_drain(*args: object, **kwargs: object) -> int:
        entered_drain.set()
        await _wait_for_event(release_drain)
        return await real_drain_once(*args, **kwargs)  # type: ignore[arg-type]

    async def stop_after_tick(seconds: float) -> None:
        # ``app_module.asyncio`` IS the global asyncio module, so this patch
        # replaces ``asyncio.sleep`` everywhere for the test's duration. Let
        # zero-delay cooperative yield checkpoints (the batched log rewrite
        # yields between keyset batches) pass through; only the drain loop's
        # real interval sleep stops the loop.
        if seconds == 0:
            return
        raise _StopDrainLoop

    monkeypatch.setattr(app_module.log_capture_service, "drain_once", paused_drain)
    monkeypatch.setattr(app_module.asyncio, "sleep", stop_after_tick)
    await app_module._log_drain_loop(app)  # pyright: ignore[reportPrivateUsage]


async def _seed_user_with_token(
    sessionmaker_: SessionMaker,
    *,
    plex_id: int,
    username: str,
    token: str,
    permissions: int = 1,
    email: str | None = None,
    avatar_url: str | None = None,
) -> int:
    async with sessionmaker_() as session:
        user = User(
            plex_id=plex_id,
            username=username,
            permissions=permissions,
            encrypted_plex_token=token,
            email=email,
            avatar_url=avatar_url,
        )
        session.add(user)
        await session.commit()
        return user.id


async def _seed_rotation_fixture(
    seed: SeedFn, sessionmaker_: SessionMaker, *, old_token: str = _OLD_PLEX_TOKEN
) -> None:
    """Post-init install with owner 42 already signed in under ``old_token``."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _seed_user_with_token(sessionmaker_, plex_id=42, username="plex-owner", token=old_token)


async def _insert_log_event(sessionmaker_: SessionMaker, secret: str) -> None:
    async with sessionmaker_() as session:
        session.add(
            LogEvent(
                level="INFO",
                logger="test",
                message=f"durable {secret}",
                context_json={secret: {"nested": [secret]}},
            )
        )
        await session.commit()


def _live_handler_with(*secrets_in_records: str) -> log_capture_service.LogCaptureHandler:
    """A capture handler whose queue and ring each hold one record per secret."""
    handler = log_capture_service.LogCaptureHandler()
    for secret in secrets_in_records:
        record = log_capture_service.CapturedLogRecord(
            created_at=datetime.now(UTC),
            level="INFO",
            logger="test",
            message=f"live {secret}",
            context={secret: [secret]},
        )
        handler.queue.put_nowait(record)
        handler.ring_buffer.append(record)
    return handler


def _drain_queue(
    handler: log_capture_service.LogCaptureHandler,
) -> list[log_capture_service.CapturedLogRecord]:
    records: list[log_capture_service.CapturedLogRecord] = []
    while True:
        try:
            records.append(handler.queue.get_nowait())
        except asyncio.QueueEmpty:
            return records


async def test_sign_in_token_rotation_rewrites_durable_and_live_logs(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """Replacing a stored user token erases the old value from all four surfaces."""
    await _seed_rotation_fixture(seed, sessionmaker_)
    await _insert_log_event(sessionmaker_, _OLD_PLEX_TOKEN)
    handler = _live_handler_with(_OLD_PLEX_TOKEN)
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})

    assert response.status_code == 200
    assert response.cookies.get("plexmgr.session")
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        row = (await db.execute(select(LogEvent))).scalars().one()
    assert user.encrypted_plex_token == _NEW_PLEX_TOKEN
    assert _OLD_PLEX_TOKEN not in row.message
    assert _OLD_PLEX_TOKEN not in json.dumps(row.context_json)
    queued = _drain_queue(handler)
    assert queued
    for record in (*queued, *handler.snapshot_tail(10)):
        assert _OLD_PLEX_TOKEN not in record.message
        assert _OLD_PLEX_TOKEN not in json.dumps(record.context)
    assert _NEW_PLEX_TOKEN in handler.secret_values
    assert _OLD_PLEX_TOKEN not in handler.secret_values


async def test_sign_in_identical_token_is_not_a_rotation(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-sign-in delivering the same token takes no lock and rewrites nothing."""
    await _seed_rotation_fixture(seed, sessionmaker_, old_token=_TOKEN)
    await _insert_log_event(sessionmaker_, _TOKEN)
    handler = _live_handler_with(_TOKEN)
    handler.secret_values = frozenset({_TOKEN})
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    called = False

    async def must_not_rewrite(*_args: object, **_kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", must_not_rewrite)
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.cookies.get("plexmgr.session")
    assert called is False
    assert lock.acquire_count == 0
    assert handler.secret_values == frozenset({_TOKEN})
    async with sessionmaker_() as db:
        row = (await db.execute(select(LogEvent))).scalars().one()
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
    assert row.message == f"durable {_TOKEN}"
    assert user.encrypted_plex_token == _TOKEN


async def test_first_ever_sign_in_token_is_initial_configuration_not_rotation(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first token (no previous value) is initial configuration, not a
    rotation (ADR-0026): no rewrite, no rotation lock, historical rows are left
    alone, and the drain loop's per-tick ``secret_values()`` refresh is what
    picks the new value up — exactly the pre-#374 contract."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _insert_log_event(sessionmaker_, "unrelated-historical-content")
    handler = log_capture_service.LogCaptureHandler()
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    called = False

    async def must_not_rewrite(*_args: object, **_kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", must_not_rewrite)
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})

    assert response.status_code == 200
    assert response.cookies.get("plexmgr.session")
    assert called is False
    assert lock.acquire_count == 0
    async with sessionmaker_() as db:
        row = (await db.execute(select(LogEvent))).scalars().one()
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
    assert row.message == "durable unrelated-historical-content"
    assert user.encrypted_plex_token == _NEW_PLEX_TOKEN
    # The new token reaches the capture snapshot on the next drain-tick refresh
    # (the standing pre-#374 mechanism), not via the rotation boundary.
    async with sessionmaker_() as db:
        assert _NEW_PLEX_TOKEN in await SettingsStore(db).secret_values()


@pytest.mark.parametrize("failure", ["rewrite", "commit"])
async def test_sign_in_rotation_failure_fails_closed_and_restores_exact_state(
    failure: Literal["rewrite", "commit"],
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rotation rolls back token, session mint, and rewrite together:
    the old token is still the valid credential, rows are unchanged, the handler
    snapshot/queue/ring are exactly the prior state, and no cookie is set."""
    await _seed_rotation_fixture(seed, sessionmaker_)
    await _insert_log_event(sessionmaker_, _OLD_PLEX_TOKEN)
    handler = _live_handler_with(_OLD_PLEX_TOKEN)
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    before_queue = tuple(_drain_queue(handler))
    for record in before_queue:
        handler.queue.put_nowait(record)
    before_ring = tuple(handler.ring_buffer)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    if failure == "rewrite":

        async def failing_rewrite(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("rewrite failed")

        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", failing_rewrite)
    else:
        real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]
        real_commit = AsyncSession.commit
        rewrite_finished = False

        async def mark_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
            nonlocal rewrite_finished
            result = await real_rewrite(session, values)
            rewrite_finished = True
            return result

        async def failing_once(self: AsyncSession) -> None:
            if rewrite_finished:
                raise RuntimeError("commit failed")
            await real_commit(self)

        monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", mark_rewrite)
        monkeypatch.setattr(AsyncSession, "commit", failing_once)

    with pytest.raises(RuntimeError):
        await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})

    assert handler.secret_values == frozenset({_OLD_PLEX_TOKEN})
    assert tuple(_drain_queue(handler)) == before_queue
    assert tuple(handler.ring_buffer) == before_ring
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        row = (await db.execute(select(LogEvent))).scalars().one()
        sessions = (await db.execute(select(AuthSession))).scalars().all()
    assert user.encrypted_plex_token == _OLD_PLEX_TOKEN
    assert row.message == f"durable {_OLD_PLEX_TOKEN}"
    assert row.context_json == {_OLD_PLEX_TOKEN: {"nested": [_OLD_PLEX_TOKEN]}}
    assert sessions == []


async def test_cancelled_sign_in_rotation_releases_lock_and_restores_snapshot(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mid-request cancellation restores the snapshot and leaves the shared
    boundary usable (the shielded-rollback cancel-handler pattern)."""
    await _seed_rotation_fixture(seed, sessionmaker_)
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]

    async def blocked_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        entered.set()
        await _wait_for_event(release)
        return await real_rewrite(session, values)

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", blocked_rewrite)
    cancelled = asyncio.create_task(
        client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    )
    await _wait_for_event(entered)
    cancelled.cancel()
    await assert_task_raises(cancelled, asyncio.CancelledError)
    await asyncio.wait_for(lock.releases.get(), timeout=5.0)

    assert handler.secret_values == frozenset({_OLD_PLEX_TOKEN})
    assert lock.locked() is False
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
    assert user.encrypted_plex_token == _OLD_PLEX_TOKEN
    # The boundary is still usable: a following rotation succeeds end-to-end.
    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", real_rewrite)
    retry = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    assert retry.status_code == 200
    assert _NEW_PLEX_TOKEN in handler.secret_values


async def test_cancel_during_pre_lock_rollback_completes_and_leaves_boundary_usable(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #399 rounds 3-4: the boundary's PRE-LOCK rollback (the
    property-(a) cleanup that releases the flushed writes before contending
    for the lock) runs TO COMPLETION across cancellation. A cancellation
    delivered while it is awaiting must not interrupt the DB op mid-flight (a
    half-cancelled aiosqlite rollback closes the connection and poisons the
    shared boundary) -- and, round 4, must not end the request EARLY either:
    a request that unwinds while the rollback is still running lets
    ``get_session``'s dependency scope close the session under the live op (a
    close/rollback race on the connection). The request ends cancelled only
    AFTER the rollback settles; nothing was staged or widened, the lock was
    never contended, and the connection stays healthy: a retry succeeds
    end-to-end."""
    await _seed_rotation_fixture(seed, sessionmaker_)
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    real_rollback = AsyncSession.rollback
    entered = asyncio.Event()
    release = asyncio.Event()
    rollback_completed = asyncio.Event()
    calls = {"n": 0}

    async def paused_first_rollback(self: AsyncSession) -> None:
        # The FIRST rollback this request performs IS the boundary's pre-lock
        # cleanup (nothing on the rotation path rolls back before it).
        calls["n"] += 1
        if calls["n"] == 1:
            entered.set()
            await _wait_for_event(release)
            await real_rollback(self)
            rollback_completed.set()
            return
        await real_rollback(self)

    monkeypatch.setattr(AsyncSession, "rollback", paused_first_rollback)

    task = asyncio.create_task(
        client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    )
    await _wait_for_event(entered)
    task.cancel()
    # The request must NOT end while the rollback is still running
    # (codex #399 round 4): ending early is exactly the close/rollback race
    # ``_rollback_to_completion`` exists to prevent. The op is still pending,
    # not dead -- the cancellation is remembered, not delivered into it.
    await asyncio.sleep(0.05)
    assert not task.done()
    assert not rollback_completed.is_set()
    release.set()
    await _wait_for_event(rollback_completed)  # it RAN TO COMPLETION...
    # ...and only THEN did the request end, still honoring the cancellation.
    await assert_task_raises(task, asyncio.CancelledError)

    assert lock.acquire_count == 0  # cancelled before the lock was ever contended
    assert handler.secret_values == frozenset({_OLD_PLEX_TOKEN})  # never widened
    assert handler.retiring_values == frozenset()
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
    assert user.encrypted_plex_token == _OLD_PLEX_TOKEN
    # The connection was not poisoned: a full retry rotation succeeds.
    monkeypatch.setattr(AsyncSession, "rollback", real_rollback)
    retry = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    assert retry.status_code == 200
    assert _NEW_PLEX_TOKEN in handler.secret_values


async def test_rotation_sign_in_recomputes_access_before_the_rewrite_flush(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex #399 round 4 (finding 5): the in-lock access recompute can wait on
    a live ``/identity`` probe for its full in-boundary timeout when no machine
    identifier is stored. It therefore runs as the boundary's ``pre_rewrite``
    hook -- BEFORE ``_rewrite_before_secret_replacement`` stages/flushes any
    writes -- because probing AFTER the flush would hold SQLite's writer lock
    for the whole probe, surfacing ``database is locked`` to concurrent writers
    (``db.py`` configures the same 5s busy timeout) while this sign-in is only
    trying to fail closed. Pin the order: recompute first, rewrite second."""
    await _seed_rotation_fixture(seed, sessionmaker_)
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    order: list[str] = []
    real_access = auth_module._post_init_access  # pyright: ignore[reportPrivateUsage]
    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]

    async def spy_access(*args: object, **kwargs: object) -> bool:
        order.append("access")
        return await real_access(*args, **kwargs)  # type: ignore[arg-type]

    async def spy_rewrite(*args: object, **kwargs: object) -> int:
        order.append("rewrite")
        return await real_rewrite(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_module, "_post_init_access", spy_access)
    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", spy_rewrite)

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    assert response.status_code == 200
    # First "access" is the ordinary pre-lock decision; the second is the
    # IN-LOCK recompute (facet 4), which must precede the rewrite so no write
    # transaction is open while a probe could still be blocking.
    assert order == ["access", "access", "rewrite"]


def _token_routed_transport(
    accounts: dict[str, tuple[dict[str, object], list[dict[str, object]]]],
) -> httpx.MockTransport:
    """A plex.tv transport that routes by the submitted ``X-Plex-Token``."""

    async def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers.get("X-Plex-Token")
        assert token in accounts, f"unexpected token {token!r}"
        user, resources = accounts[token]
        if request.url.path == "/api/v2/user":
            return httpx.Response(200, json=user)
        if request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=resources)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def test_concurrent_sign_ins_for_two_users_serialize_and_keep_each_other_masked(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two users rotating concurrently serialize on the shared boundary; each
    rotation masks only its own retiring token, and the OTHER user's current
    token stays in the capture snapshot at every observed instant."""
    a_old, a_new = "user-a-old-token", "user-a-new-token"
    b_old, b_new = "user-b-old-token", "user-b-new-token"
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _seed_user_with_token(sessionmaker_, plex_id=42, username="plex-owner", token=a_old)
    await _seed_user_with_token(
        sessionmaker_, plex_id=99, username="second-account", token=b_old, permissions=0
    )
    await _insert_log_event(sessionmaker_, a_old)
    await _insert_log_event(sessionmaker_, b_old)
    handler = _live_handler_with(a_old, b_old)
    handler.secret_values = frozenset({a_old, b_old})
    app.state.log_handler = handler
    await _use_transport(
        app,
        _token_routed_transport(
            {
                a_new: (_OWNER_USER, [_owned_server()]),
                b_new: (_SECOND_USER, [_shared_server()]),
            }
        ),
    )
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)
    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]
    gates: list[tuple[asyncio.Event, asyncio.Event]] = [
        (asyncio.Event(), asyncio.Event()),
        (asyncio.Event(), asyncio.Event()),
    ]
    rewrites_started = 0

    async def gated_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        nonlocal rewrites_started
        entered, release = gates[rewrites_started]
        rewrites_started += 1
        entered.set()
        await _wait_for_event(release)
        return await real_rewrite(session, values)

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", gated_rewrite)

    sign_in_a = asyncio.create_task(client.post("/api/v1/auth/plex", json={"auth_token": a_new}))
    await _wait_for_event(gates[0][0])
    # A is mid-rotation (inside the lock): its widened snapshot must cover its
    # own old+new values AND user B's still-current token.
    for value in (a_old, a_new, b_old):
        assert value in handler.secret_values
    sign_in_b = asyncio.create_task(client.post("/api/v1/auth/plex", json={"auth_token": b_new}))
    await _wait_for_event(lock.second_acquire_started)
    assert not sign_in_b.done()  # serialized behind A on the shared boundary
    gates[0][1].set()
    response_a = await asyncio.wait_for(sign_in_a, timeout=5.0)
    assert response_a.status_code == 200
    await _wait_for_event(gates[1][0])
    # Between the two rotations B's current token is STILL masked and A's new
    # token stayed masked across the handover. B's transition set was read
    # FRESH under the lock (after A committed), so A's retired token is
    # already gone from the snapshot even mid-B-rotation.
    assert b_old in handler.secret_values
    assert a_new in handler.secret_values
    assert a_old not in handler.secret_values
    gates[1][1].set()
    response_b = await asyncio.wait_for(sign_in_b, timeout=5.0)
    assert response_b.status_code == 200

    async with sessionmaker_() as db:
        rows = (await db.execute(select(LogEvent))).scalars().all()
        user_a = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        user_b = (await db.execute(select(User).where(User.plex_id == 99))).scalars().one()
    rendered_rows = " ".join(f"{row.message} {json.dumps(row.context_json)}" for row in rows)
    for retired in (a_old, b_old):
        assert retired not in rendered_rows
        assert retired not in handler.secret_values
    for record in (*_drain_queue(handler), *handler.snapshot_tail(10)):
        assert a_old not in record.message and b_old not in record.message
        assert a_old not in json.dumps(record.context)
        assert b_old not in json.dumps(record.context)
    assert user_a.encrypted_plex_token == a_new
    assert user_b.encrypted_plex_token == b_new
    assert {a_new, b_new}.issubset(handler.secret_values)


async def test_rotating_sign_in_reapplies_every_user_field_and_demotes(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """Divergence guard for the rotation branch's re-staged writes (issue #374):
    the seeded row differs from the sign-in-computed value on EVERY mutable
    field (stale username/email/avatar, admin permissions against a now
    shared-only account, old token), the token rotates in the SAME sign-in,
    and the committed row must reflect ALL the new values — a re-apply dropped
    from the boundary's fresh transaction fails loudly here instead of being
    masked by ``session.refresh()`` restoring a coincidentally-equal value.
    The demotion stream-close must still fire post-commit."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    user_id = await _seed_user_with_token(
        sessionmaker_,
        plex_id=42,
        username="stale-name",
        token=_OLD_PLEX_TOKEN,
        permissions=1,
        email="stale@example.test",
        avatar_url="https://stale.example/avatar",
    )
    fresh_account: dict[str, object] = {
        "id": 42,
        "uuid": "owner-uuid",
        "username": "fresh-name",
        "title": "fresh-name",
        "email": "fresh@example.test",
        "thumb": "https://plex.tv/users/owner-uuid/avatar?c=2",
    }
    # Only SHARED (not owned) access to the configured server now: admin -> non-admin.
    await _use_transport(app, _plex_tv_transport(user=fresh_account, resources=[_shared_server()]))
    subscription = get_event_hub(app).subscribe(auth_method="plex_session", user_id=user_id)

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is False
    # The demotion committed inside the rotation boundary still closes the
    # user's realtime streams afterwards (issue #183 ordering preserved).
    assert subscription.closed is True
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
    assert user.username == "fresh-name"
    assert user.permissions == 0
    assert user.email == "fresh@example.test"
    assert user.avatar_url == "https://plex.tv/users/owner-uuid/avatar?c=2"
    assert user.encrypted_plex_token == _NEW_PLEX_TOKEN
    assert user.last_login is not None


async def test_short_retired_token_is_erased_despite_read_floor(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A retiring token shorter than the read path's 8-character value floor is
    still erased from durable rows, the queue, and the ring: the rotation
    rewrite uses the exact, floorless retired-value pass (ADR-0026 P2 fix),
    which the sign-in path inherits by sharing the boundary."""
    short_old = "tok5!"  # 5 chars -- below redact_known_secrets' floor
    await _seed_rotation_fixture(seed, sessionmaker_, old_token=short_old)
    await _insert_log_event(sessionmaker_, short_old)
    handler = _live_handler_with(short_old)
    handler.secret_values = frozenset({short_old})
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})

    assert response.status_code == 200
    async with sessionmaker_() as db:
        row = (await db.execute(select(LogEvent))).scalars().one()
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
    assert user.encrypted_plex_token == _NEW_PLEX_TOKEN
    assert short_old not in row.message
    assert short_old not in json.dumps(row.context_json)
    for record in (*_drain_queue(handler), *handler.snapshot_tail(10)):
        assert short_old not in record.message
        assert short_old not in json.dumps(record.context)
    assert short_old not in handler.secret_values


async def test_sign_in_rotation_waits_for_in_flight_durable_log_read(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable /logs response cannot span the sign-in rotation's before/after
    secret sets: the read holds the shared lock through rendering and the
    rotating sign-in queues behind it."""
    await _seed_rotation_fixture(seed, sessionmaker_)
    await _insert_log_event(sessionmaker_, _OLD_PLEX_TOKEN)
    app.state.log_handler = log_capture_service.LogCaptureHandler()
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    entered = asyncio.Event()
    release = asyncio.Event()
    real_list_events = ops_router.SqlLogEventRepository.list_events

    async def paused_list_events(self: object, *args: object, **kwargs: object) -> object:
        page = await real_list_events(self, *args, **kwargs)  # type: ignore[arg-type]
        entered.set()
        await _wait_for_event(release)
        return page

    monkeypatch.setattr(ops_router.SqlLogEventRepository, "list_events", paused_list_events)
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    read_task = asyncio.create_task(client.get("/api/v1/ops/logs", headers={"X-Api-Key": _API_KEY}))
    await _wait_for_event(entered)
    sign_in_task = asyncio.create_task(
        client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    )
    await _wait_for_event(lock.second_acquire_started)
    assert not sign_in_task.done()
    release.set()
    read_response = await asyncio.wait_for(read_task, timeout=5.0)
    sign_in_response = await asyncio.wait_for(sign_in_task, timeout=5.0)

    assert read_response.status_code == 200
    assert sign_in_response.status_code == 200
    assert _OLD_PLEX_TOKEN not in read_response.text
    async with sessionmaker_() as db:
        row = (await db.execute(select(LogEvent))).scalars().one()
    assert _OLD_PLEX_TOKEN not in row.message


async def test_sign_in_rotation_waits_for_drain_and_retired_row_is_rewritten(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain committed before the sign-in rotation is still rewritten before
    the rotation returns: the drain holds the shared lock, the sign-in queues
    behind it, and the freshly-drained row containing the retiring token is
    covered by the durable rewrite."""

    await _seed_rotation_fixture(seed, sessionmaker_)
    handler = log_capture_service.LogCaptureHandler()
    handler.queue.put_nowait(
        log_capture_service.CapturedLogRecord(
            created_at=datetime.now(UTC),
            level="INFO",
            logger="test",
            message=f"drained {_OLD_PLEX_TOKEN}",
            context={"retired": _OLD_PLEX_TOKEN},
        )
    )
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)
    entered_drain = asyncio.Event()
    release_drain = asyncio.Event()
    drain = asyncio.create_task(_run_one_drain(app, monkeypatch, entered_drain, release_drain))
    await _wait_for_event(entered_drain)

    sign_in_task = asyncio.create_task(
        client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    )
    await _wait_for_event(lock.second_acquire_started)
    assert not sign_in_task.done()
    release_drain.set()
    await assert_task_raises(drain, _StopDrainLoop)
    response = await asyncio.wait_for(sign_in_task, timeout=5.0)

    assert response.status_code == 200
    async with sessionmaker_() as db:
        row = (await db.execute(select(LogEvent))).scalars().one()
    assert _OLD_PLEX_TOKEN not in row.message
    assert _OLD_PLEX_TOKEN not in json.dumps(row.context_json)


# --------------------------------------------------------------------------- #
# Boundary-race family (issue #389): the rotation boundary is entered clean,
# re-reads its state under the lock, and shields commit + cleanup as one unit.
# --------------------------------------------------------------------------- #
async def test_cancel_during_commit_still_runs_the_completion_sweep(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facet 1: a cancellation delivered once the rotation commit is durable must
    NOT skip the post-commit sweep. The commit + completion run as one shielded
    unit, so the retired token is erased from the durable rows, the queue, and
    the ring, and ``secret_values`` narrows -- even though the request ends
    cancelled and never sets a cookie."""
    await _seed_rotation_fixture(seed, sessionmaker_)
    await _insert_log_event(sessionmaker_, _OLD_PLEX_TOKEN)
    handler = _live_handler_with(_OLD_PLEX_TOKEN)
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]
    real_commit = AsyncSession.commit
    rewrite_finished = False
    committed = asyncio.Event()
    release = asyncio.Event()

    async def mark_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        nonlocal rewrite_finished
        result = await real_rewrite(session, values)
        rewrite_finished = True
        return result

    async def paused_commit(self: AsyncSession) -> None:
        await real_commit(self)
        if rewrite_finished:
            # The boundary's commit is now DURABLE. Pause so the test can cancel
            # the request while the shielded unit is mid-flight; the sweep still
            # runs once ``release`` fires.
            committed.set()
            await _wait_for_event(release)

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", mark_rewrite)
    monkeypatch.setattr(AsyncSession, "commit", paused_commit)

    task = asyncio.create_task(
        client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    )
    await _wait_for_event(committed)
    task.cancel()
    # Deliver the cancellation to the request's shielded await while the commit
    # unit is still paused, so the request genuinely ends cancelled.
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await assert_task_raises(task, asyncio.CancelledError)

    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        row = (await db.execute(select(LogEvent))).scalars().one()
    assert user.encrypted_plex_token == _NEW_PLEX_TOKEN  # commit landed durably
    assert _OLD_PLEX_TOKEN not in row.message  # durable rewrite ran
    assert _OLD_PLEX_TOKEN not in json.dumps(row.context_json)
    for record in (*_drain_queue(handler), *handler.snapshot_tail(10)):
        assert _OLD_PLEX_TOKEN not in record.message  # queue/ring swept
        assert _OLD_PLEX_TOKEN not in json.dumps(record.context)
    assert _OLD_PLEX_TOKEN not in handler.secret_values  # snapshot narrowed
    assert _NEW_PLEX_TOKEN in handler.secret_values


async def test_cancel_plus_commit_failure_restores_snapshot_and_surfaces_the_failure(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Codex #399 round 1: a cancellation remembered while the commit unit is in
    flight, followed by the unit FAILING, must not fall through to the bare
    re-raised ``CancelledError``. The boundary inspects the unit's outcome
    before honoring the cancellation: the rollback + ``abort_secret_rotation``
    failure path runs (snapshot restored, retiring set cleared — the widened
    in-memory redaction state never outlives the rolled-back rotation), the
    real failure is surfaced through the log pipeline with its traceback (its
    retrieval here is also what prevents an 'exception was never retrieved'
    warning), and the request still ends CANCELLED — cancellation wins the
    re-raise (the server's task machinery expects a cancelled task to end
    cancelled), with the failure chained as its ``__cause__``."""
    await _seed_rotation_fixture(seed, sessionmaker_)
    await _insert_log_event(sessionmaker_, _OLD_PLEX_TOKEN)
    handler = _live_handler_with(_OLD_PLEX_TOKEN)
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    before_queue = tuple(_drain_queue(handler))
    for record in before_queue:
        handler.queue.put_nowait(record)
    before_ring = tuple(handler.ring_buffer)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]
    real_commit = AsyncSession.commit
    rewrite_finished = False
    entered_commit = asyncio.Event()
    release_commit = asyncio.Event()

    async def mark_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        nonlocal rewrite_finished
        result = await real_rewrite(session, values)
        rewrite_finished = True
        return result

    async def failing_paused_commit(self: AsyncSession) -> None:
        if not rewrite_finished:
            await real_commit(self)
            return
        # The boundary's commit unit is now in flight. Pause so the test can
        # deliver (and have the loop REMEMBER) a cancellation first, then FAIL.
        entered_commit.set()
        await _wait_for_event(release_commit)
        raise RuntimeError("rotation commit failed")

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", mark_rewrite)
    monkeypatch.setattr(AsyncSession, "commit", failing_paused_commit)

    task = asyncio.create_task(
        client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})
    )
    await _wait_for_event(entered_commit)
    task.cancel()
    # Let the cancellation reach the boundary's shield await and be remembered
    # (pending_cancel) while the unit is still paused -- the exact interleaving
    # the finding describes.
    for _ in range(5):
        await asyncio.sleep(0)
    with caplog.at_level(logging.ERROR, logger=settings_router.__name__):
        release_commit.set()
        await assert_task_raises(task, asyncio.CancelledError)
    await asyncio.wait_for(lock.releases.get(), timeout=5.0)

    # The real failure was surfaced (state, not silence), traceback attached.
    failure_records = [
        record
        for record in caplog.records
        if "secret rotation commit failed" in record.getMessage()
    ]
    assert len(failure_records) == 1
    exc_info = failure_records[0].exc_info
    assert exc_info is not None and isinstance(exc_info[1], RuntimeError)
    # The failure path ran: snapshot restored, retiring set cleared, live
    # surfaces byte-identical, lock released and reusable.
    assert handler.secret_values == frozenset({_OLD_PLEX_TOKEN})
    assert handler.retiring_values == frozenset()
    assert tuple(_drain_queue(handler)) == before_queue
    assert tuple(handler.ring_buffer) == before_ring
    assert lock.locked() is False
    # Nothing durable landed: token, sessions, and history are untouched.
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        sessions = (await db.execute(select(AuthSession))).scalars().all()
        row = (await db.execute(select(LogEvent))).scalars().one()
    assert user.encrypted_plex_token == _OLD_PLEX_TOKEN
    assert sessions == []
    assert row.message == f"durable {_OLD_PLEX_TOKEN}"


async def test_concurrent_same_account_rotations_retire_the_current_stored_token(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facet 2: two concurrent sign-ins for the SAME account with different
    replacement tokens both read the same pre-lock ``old_token``. The loser must
    retire whatever token is ACTUALLY stored under the lock (the winner's freshly
    committed value) -- re-read there, not the stale pre-lock read -- or the
    winner's token is left uncovered by the historical rewrite."""
    old = "same-account-old-token"
    a_new = "same-account-token-a-longenough"
    b_new = "same-account-token-b-longenough"
    await _seed_rotation_fixture(seed, sessionmaker_, old_token=old)
    await _insert_log_event(sessionmaker_, old)
    handler = _live_handler_with(old)
    handler.secret_values = frozenset({old})
    app.state.log_handler = handler
    await _use_transport(
        app,
        _token_routed_transport(
            {
                a_new: (_OWNER_USER, [_owned_server()]),
                b_new: (_OWNER_USER, [_owned_server()]),
            }
        ),
    )
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)
    real_rewrite = settings_router._rewrite_before_secret_replacement  # pyright: ignore[reportPrivateUsage]
    gates: list[tuple[asyncio.Event, asyncio.Event]] = [
        (asyncio.Event(), asyncio.Event()),
        (asyncio.Event(), asyncio.Event()),
    ]
    rewrite_values: list[frozenset[str]] = []

    async def gated_rewrite(session: AsyncSession, values: frozenset[str]) -> int:
        entered, release = gates[len(rewrite_values)]
        rewrite_values.append(values)
        entered.set()
        await _wait_for_event(release)
        return await real_rewrite(session, values)

    monkeypatch.setattr(settings_router, "_rewrite_before_secret_replacement", gated_rewrite)

    sign_in_a = asyncio.create_task(client.post("/api/v1/auth/plex", json={"auth_token": a_new}))
    await _wait_for_event(gates[0][0])
    # B reaches _upsert_user and reads old_token=old (A has not committed) before
    # it queues behind A on the shared boundary.
    sign_in_b = asyncio.create_task(client.post("/api/v1/auth/plex", json={"auth_token": b_new}))
    await _wait_for_event(lock.second_acquire_started)
    assert not sign_in_b.done()
    gates[0][1].set()
    response_a = await asyncio.wait_for(sign_in_a, timeout=5.0)
    assert response_a.status_code == 200
    # A committed old -> a_new. Seed the live surfaces with an a_new-bearing
    # record (in-memory only; the single-connection test DB precludes a durable
    # concurrent write). If B retired the stale ``old``, this survives.
    a_record = log_capture_service.CapturedLogRecord(
        created_at=datetime.now(UTC),
        level="INFO",
        logger="test",
        message=f"live {a_new}",
        context={a_new: [a_new]},
    )
    handler.queue.put_nowait(a_record)
    handler.ring_buffer.append(a_record)
    await _wait_for_event(gates[1][0])
    gates[1][1].set()
    response_b = await asyncio.wait_for(sign_in_b, timeout=5.0)
    assert response_b.status_code == 200

    # A retired ``old``; B re-read and retired the CURRENT stored value a_new.
    assert rewrite_values == [frozenset({old}), frozenset({a_new})]
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        row = (await db.execute(select(LogEvent))).scalars().one()
    assert user.encrypted_plex_token == b_new  # last writer wins
    assert old not in row.message  # A's rewrite erased the original value
    for record in (*_drain_queue(handler), *handler.snapshot_tail(10)):
        assert old not in record.message and a_new not in record.message
        assert old not in json.dumps(record.context)
        assert a_new not in json.dumps(record.context)
    assert b_new in handler.secret_values
    assert old not in handler.secret_values
    assert a_new not in handler.secret_values


async def test_rotation_boundary_releases_write_txn_before_awaiting_lock(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facet 3: the sign-in mints + flushes a client-identifier row before the
    boundary; that open write transaction must be rolled back BEFORE the boundary
    contends for ``secret_rotation_lock``, so a request never holds a SQLite
    writer while waiting on the lock (which would deadlock against a drain that
    holds the lock and needs to write)."""
    await _seed_rotation_fixture(seed, sessionmaker_)  # no client-id stored -> minted here
    handler = log_capture_service.LogCaptureHandler()
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    real_rollback = AsyncSession.rollback
    observed_session: list[AsyncSession] = []
    in_txn_before: list[bool] = []

    async def recording_rollback(self: AsyncSession) -> None:
        if not observed_session:
            observed_session.append(self)
            in_txn_before.append(self.in_transaction())
        await real_rollback(self)

    class ProbedLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.in_txn_at_acquire: bool | None = None

        async def acquire(self) -> Literal[True]:
            if observed_session and self.in_txn_at_acquire is None:
                self.in_txn_at_acquire = observed_session[0].in_transaction()
            return await super().acquire()

    lock = ProbedLock()
    monkeypatch.setattr(AsyncSession, "rollback", recording_rollback)
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})

    assert response.status_code == 200
    # There WAS an open write transaction (the flushed client-id insert + user
    # update) when the boundary took its pre-lock rollback...
    assert in_txn_before == [True]
    # ...and by the time the boundary acquired the lock, that transaction was
    # already released -- no writer is held while waiting.
    assert lock.in_txn_at_acquire is False


async def test_rotation_boundary_revalidates_access_against_repointed_server(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facet 4 (security): a token-rotation sign-in recomputes its access
    decision INSIDE the boundary. If a concurrent Plex repoint changes
    ``plex_machine_identifier`` while the sign-in waits on the lock, an account
    with access only to the OLD server is refused against the NEW one instead of
    minting a session against a server it cannot reach. Simulated (the
    single-connection test DB precludes a real concurrent write) by returning the
    OLD machine id for the pre-lock access read and the NEW id for the read the
    boundary recomputes -- exactly what a committed repoint would present."""
    new_machine = "repointed-machine-id"
    await _seed_rotation_fixture(seed, sessionmaker_)  # machine_id=_MACHINE_ID, user 42 old token
    await _insert_log_event(sessionmaker_, _OLD_PLEX_TOKEN)
    handler = _live_handler_with(_OLD_PLEX_TOKEN)
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler
    # The account owns ONLY the original server; it has no resource for the repoint.
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    real_get = SettingsStore.get
    machine_reads = 0

    async def racing_get(self: SettingsStore, key: str) -> str | None:
        nonlocal machine_reads
        if key == PLEX_MACHINE_ID_SETTING:
            machine_reads += 1
            # 1st read = pre-lock decision (original server); 2nd = the boundary's
            # recompute (repointed server).
            return _MACHINE_ID if machine_reads == 1 else new_machine
        return await real_get(self, key)

    monkeypatch.setattr(SettingsStore, "get", racing_get)

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN})

    assert response.status_code == 403
    assert response.json()["detail"] == "server_access_denied"
    assert machine_reads == 2  # the access check ran a SECOND time, under the lock
    # Fails CLOSED: token not rotated, no session minted, history + snapshot intact.
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        sessions = (await db.execute(select(AuthSession))).scalars().all()
        row = (await db.execute(select(LogEvent))).scalars().one()
    assert user.encrypted_plex_token == _OLD_PLEX_TOKEN
    assert sessions == []
    assert _OLD_PLEX_TOKEN in row.message  # rewrite rolled back -> unchanged
    assert handler.secret_values == frozenset({_OLD_PLEX_TOKEN})


async def test_hung_identity_probe_cannot_hold_the_rotation_lock_beyond_the_bound(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Facet 4's in-boundary access recompute is TIME-BOUNDED: on the fallback
    path (no stored machine id) it probes the Plex server's ``/identity`` while
    holding the process-global rotation lock, and a hung server must not pin
    that lock for the HTTP client's full 30s timeout (stalling every rotation
    and the log drain). The bound expires, the sign-in fails CLOSED with the
    distinct retryable envelope, the boundary rolls back, and the lock is
    released -- never a silent fallback to the stale pre-lock decision."""
    await seed(initialized=True, app_api_key=_API_KEY)
    # No plex_machine_identifier stored -> both access checks take the
    # /identity fallback; the boundary's recompute is the SECOND probe.
    await _store_setting(sessionmaker_, "plex_url", "http://plex.local:32400")
    await _store_setting(sessionmaker_, "plex_token", "service-token")
    await _seed_user_with_token(
        sessionmaker_, plex_id=42, username="plex-owner", token=_OLD_PLEX_TOKEN
    )
    await _insert_log_event(sessionmaker_, _OLD_PLEX_TOKEN)
    handler = _live_handler_with(_OLD_PLEX_TOKEN)
    handler.secret_values = frozenset({_OLD_PLEX_TOKEN})
    app.state.log_handler = handler

    identity_calls = 0

    async def routing_handler(request: httpx.Request) -> httpx.Response:
        nonlocal identity_calls
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_OWNER_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_owned_server()])
        if request.url.path == "/identity":
            identity_calls += 1
            if identity_calls == 1:  # the PRE-lock decision resolves normally
                return httpx.Response(
                    200, json={"MediaContainer": {"machineIdentifier": _MACHINE_ID}}
                )
            # The IN-LOCK recompute's probe hangs until cancelled by the bound.
            await asyncio.Event().wait()
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    await _use_transport(app, httpx.MockTransport(routing_handler))
    lock = _ObservableLock()
    monkeypatch.setattr(deps.secret_rotation_lock, "value", lock)
    # Shrink the bound so the test proves the mechanism without a real wait.
    monkeypatch.setattr(auth_module, "_IN_BOUNDARY_ACCESS_RECHECK_TIMEOUT_SECONDS", 0.05)

    response = await asyncio.wait_for(
        client.post("/api/v1/auth/plex", json={"auth_token": _NEW_PLEX_TOKEN}), timeout=5.0
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "server_identity_recheck_timeout"
    assert identity_calls == 2  # the recompute genuinely re-probed under the lock
    assert lock.locked() is False  # the bound released the shared boundary
    # Fails CLOSED: token not rotated, no session minted, history + snapshot intact.
    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        sessions = (await db.execute(select(AuthSession))).scalars().all()
        row = (await db.execute(select(LogEvent))).scalars().one()
    assert user.encrypted_plex_token == _OLD_PLEX_TOKEN
    assert sessions == []
    assert _OLD_PLEX_TOKEN in row.message
    assert handler.secret_values == frozenset({_OLD_PLEX_TOKEN})
