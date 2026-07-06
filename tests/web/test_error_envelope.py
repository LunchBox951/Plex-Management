"""The error envelope keeps ``detail`` as the stable machine code (the SPA's
existing humanizer keys on it) while adding human ``message``/``hint`` and
non-secret ``diagnostics`` -- north star #3: no failure without a stated cause.

This suite deliberately builds its OWN bare ``FastAPI()`` (never ``create_app``)
and imports only ``web.errors`` -- ``web.app`` is transitionally unimportable
until the auth router is rewritten, so collection must not touch it.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.adapters.plex.oauth import PlexVerifyError
from plex_manager.web.errors import AppError, install_error_handlers


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/boom-app")
    async def boom_app() -> None:
        raise AppError(
            status_code=403,
            code="server_not_owned",
            message="Your Plex account does not own the server 'Apollo'.",
            hint="Sign in with the account that owns the server, or pick a server you own.",
        )

    @app.get("/boom-verify")
    async def boom_verify() -> None:
        raise PlexVerifyError(
            "plex_tv_unreachable_server",
            "plex.tv did not answer from the server.",
            diagnostics={"host": "plex.tv"},
        )

    return app


async def test_app_error_envelope(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/boom-app")
    assert res.status_code == 403
    body = res.json()
    assert body["detail"] == "server_not_owned"
    assert "does not own" in body["message"]
    assert body["hint"].startswith("Sign in")
    assert "diagnostics" not in body


async def test_plex_verify_error_envelope(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/boom-verify")
    assert res.status_code == 502
    body = res.json()
    assert body["detail"] == "plex_tv_unreachable_server"
    assert body["diagnostics"] == {"host": "plex.tv"}
