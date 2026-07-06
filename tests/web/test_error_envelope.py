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
from pydantic import BaseModel, Field

from plex_manager.adapters.plex.oauth import PlexVerifyError
from plex_manager.web.errors import AppError, install_error_handlers


class _ValidatedBody(BaseModel):
    """A trivial pydantic model used to trigger a real ``RequestValidationError``
    (rather than raising one directly) so the handler runs exactly as FastAPI's
    routing layer invokes it. Bounded so a non-finite ``value`` (which fails
    every comparison) is actually rejected, mirroring the real settings fields."""

    value: float = Field(le=1_000_000.0)


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    async def boom_app() -> None:
        raise AppError(
            status_code=403,
            code="server_not_owned",
            message="Your Plex account does not own the server 'Apollo'.",
            hint="Sign in with the account that owns the server, or pick a server you own.",
        )

    async def boom_verify() -> None:
        raise PlexVerifyError(
            "plex_tv_unreachable_server",
            "plex.tv did not answer from the server.",
            diagnostics={"host": "plex.tv"},
        )

    async def boom_validation(body: _ValidatedBody) -> None:
        return None

    # Register by name (not the ``@app.get`` decorator) so the handlers count as
    # referenced under strict pyright's reportUnusedFunction.
    app.add_api_route("/boom-app", boom_app)
    app.add_api_route("/boom-verify", boom_verify)
    app.add_api_route("/boom-validation", boom_validation, methods=["POST"])
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


async def test_request_validation_error_keeps_standard_detail_envelope(app: FastAPI) -> None:
    """The non-finite-safe override must still shape its body as FastAPI's
    documented ``HTTPValidationError`` (``{"detail": [...]}``), matching
    ``docs/api/openapi.json`` and what ``frontend/src/lib/errors.ts``'s
    ``extractDetail`` requires (a top-level ``detail`` key) -- a bare list here
    silently breaks field-level error rendering for every 422 in the app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/boom-validation", json={"value": "not-a-number"})
    assert res.status_code == 422
    body = res.json()
    assert isinstance(body, dict)
    assert isinstance(body["detail"], list)
    assert body["detail"][0]["loc"] == ["body", "value"]


async def test_request_validation_error_sanitizes_non_finite_echoed_input(app: FastAPI) -> None:
    """A non-finite rejected value must still round-trip inside the standard
    envelope -- as its ``repr()`` string, since ``json.dumps(..., allow_nan=False)``
    would otherwise raise while rendering the very response reporting the
    rejection (issue #92)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/boom-validation",
            content=b'{"value": Infinity}',
            headers={"Content-Type": "application/json"},
        )
    assert res.status_code == 422
    body = res.json()
    assert isinstance(body, dict)
    assert body["detail"][0]["input"] == "inf"
