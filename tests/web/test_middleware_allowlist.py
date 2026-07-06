"""SetupGuardMiddleware allowlist: sign-in is reachable before first-run init.

The maintainer's acceptance criterion (AC1): a fresh install (uninitialized DB,
no setup token) boots and ``Sign in with Plex`` is the FIRST thing reachable — it
IS the first setup step, since the install is claimed by the first Plex server
owner to sign in. These tests pin that the REAL production allowlist lets
``/api/v1/auth`` through the guard pre-init while every OTHER protected API path
still gets the honest ``409 setup_required``. They deliberately do NOT patch the
allowlist, so they exercise the shipped ``SETUP_ALLOWLIST_PREFIXES``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.web.routers import auth as auth_module

SeedFn = Callable[..., Awaitable[None]]

_TOKEN = "browser-obtained-plex-token"  # noqa: S105 - fake token for the MockTransport
_MACHINE_ID = "abc123machine"

_OWNER_USER: dict[str, object] = {
    "id": 42,
    "uuid": "owner-uuid",
    "username": "plex-owner",
    "title": "plex-owner",
    "email": "owner@example.test",
}


def _owned_server() -> dict[str, object]:
    return {
        "name": "Apollo",
        "product": "Plex Media Server",
        "clientIdentifier": _MACHINE_ID,
        "provides": "server",
        "owned": True,
        "connections": [],
    }


def _owner_transport() -> httpx.MockTransport:
    """A plex.tv v2 transport that verifies the token as a server-owning account."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_OWNER_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_owned_server()])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def _use_transport(app: FastAPI, transport: httpx.MockTransport) -> None:
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=transport)


@pytest.fixture(autouse=True)
def _reset_throttle() -> None:
    """Clear the in-process sign-in throttle so a prior file's attempts never leak."""
    auth_module._reset_sign_in_throttle()


async def test_auth_sign_in_reachable_pre_init(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Pre-init, ``POST /api/v1/auth/plex`` reaches the router — not the guard's
    409. With a valid server-owning token it completes the first-owner claim (200),
    proving sign-in IS the first reachable setup step."""
    await seed(initialized=False)
    await _use_transport(app, _owner_transport())

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code != 409  # the setup guard did NOT block sign-in
    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True


async def test_auth_me_reachable_pre_init(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Pre-init, ``GET /api/v1/auth/me`` reaches the router and honestly reports
    an unauthenticated caller instead of the guard's 409."""
    await seed(initialized=False)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json()["authenticated"] is False


async def test_protected_path_still_guarded_pre_init(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Every OTHER protected API path still gets the honest 409 pre-init — only
    the setup and auth sub-APIs are allowlisted."""
    await seed(initialized=False)

    response = await client.get("/api/v1/requests")

    assert response.status_code == 409
    assert response.json()["detail"] == "setup_required"
