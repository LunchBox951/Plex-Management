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
from pydantic import BaseModel, Field, field_validator

from plex_manager.adapters.plex.oauth import PlexVerifyError
from plex_manager.web.errors import (
    AppError,
    _redact_secret_fields,  # pyright: ignore[reportPrivateUsage]
    _scrub_secret_values,  # pyright: ignore[reportPrivateUsage]
    install_error_handlers,
)


class _CredBody(BaseModel):
    """A body with a secret field (``plex_token``), a non-secret one (``label``),
    and a bounded float (``ratio``) to exercise the composed
    RequestValidationError sanitizer: secret redaction + non-finite rendering."""

    plex_token: str
    label: str
    # Bounded like the real settings percent fields, so a non-finite value (which
    # fails every comparison) is genuinely rejected -- the composition test sends
    # Infinity here alongside a bad secret.
    ratio: float | None = Field(default=None, le=100.0)

    @field_validator("plex_token")
    @classmethod
    def _reject_bad(cls, value: str) -> str:
        if "BAD" in value:
            raise ValueError("contains characters that are not valid in an HTTP header")
        return value


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

    async def take_cred(body: _CredBody) -> dict[str, str]:
        return {"ok": body.label}

    # Register by name (not the ``@app.get`` decorator) so the handlers count as
    # referenced under strict pyright's reportUnusedFunction.
    app.add_api_route("/boom-app", boom_app)
    app.add_api_route("/boom-verify", boom_verify)
    app.add_api_route("/boom-validation", boom_validation, methods=["POST"])
    app.add_api_route("/take-cred", take_cred, methods=["POST"])
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


async def test_validation_error_redacts_secret_field_input(app: FastAPI) -> None:
    """A validation error on a secret field must not echo the submitted value: the
    422 body keeps the {"detail": [...]} envelope but the secret field's ``input``
    is redacted and its ``ctx`` dropped -- north star #3."""
    submitted = "leak-SENTINEL-BAD-ZZZINJECT"
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/take-cred", json={"plex_token": submitted, "label": "x"})
    assert res.status_code == 422
    assert "SENTINEL" not in res.text
    assert "ZZZINJECT" not in res.text
    (error,) = res.json()["detail"]
    assert error["loc"] == ["body", "plex_token"]
    assert error["input"] == "***"
    assert "ctx" not in error


async def test_validation_error_keeps_non_secret_input(app: FastAPI) -> None:
    """A NON-secret field keeps its echoed ``input`` for debuggability: only secret
    fields are redacted. Here ``plex_token`` is present+valid and the missing
    non-secret ``label`` drives a normal 'field required' error that still shows its
    (null) input."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/take-cred", json={"plex_token": "fine", "shown": "visible-42"})
    assert res.status_code == 422
    detail = res.json()["detail"]
    # The 'label' field is required and missing -> a normal error whose input is the
    # whole submitted body (a non-secret loc), left intact for debugging.
    assert any(err["loc"][-1] == "label" for err in detail)
    assert "visible-42" in res.text


def test_scrub_secret_values_masks_nested_secrets_only() -> None:
    """The recursive scrubber masks secret-keyed values ANYWHERE (dicts inside
    lists inside dicts) while leaving non-secret values intact -- so a whole-body
    ``input`` echoed by a non-secret field's error never leaks a credential."""
    payload = {
        "plex_token": "SECRET1",
        "label": "keep-me",
        "nested": [{"prowlarr_api_key": "SECRET2", "url": "http://ok"}],
    }
    scrubbed = _scrub_secret_values(payload)
    assert scrubbed == {
        "plex_token": "***",
        "label": "keep-me",
        "nested": [{"prowlarr_api_key": "***", "url": "http://ok"}],
    }


def test_redact_secret_fields_masks_whole_input_for_secret_loc() -> None:
    """When the error's own ``loc`` targets a secret field, the whole ``input`` (the
    rejected secret itself) is replaced and ``ctx`` dropped."""
    error = {
        "type": "value_error",
        "loc": ["body", "plex_token"],
        "msg": "Value error, contains characters that are not valid in an HTTP header",
        "input": "SECRET-VALUE",
        "ctx": {"error": "SECRET-VALUE echoed here"},
    }
    redacted = _redact_secret_fields(error)
    assert redacted["input"] == "***"
    assert "ctx" not in redacted
    # The original error dict is not mutated (pure sanitizer).
    assert error["input"] == "SECRET-VALUE"


async def test_validation_error_composes_secret_redaction_with_non_finite(app: FastAPI) -> None:
    """The COMPOSED handler runs BOTH sanitizers on one request: a body carrying a
    header-unsafe secret AND a non-finite float yields a single 422 whose body has
    the ``repr()`` string for the rejected float, the ``***`` mask for the rejected
    secret, and no raw secret text anywhere -- proving redaction (stage 1) does not
    un-sanitize non-finites and the non-finite pass (stage 2) does not unmask
    secrets, inside the standard ``{"detail": [...]}`` envelope."""
    submitted = "leak-SENTINEL-BAD-ZZZINJECT"
    # Raw JSON body: ``Infinity`` is a python-json extension FastAPI's parser
    # accepts (json.loads allow_nan default) -- exactly how a client reaches the
    # non-finite rendering path.
    content = f'{{"plex_token": "{submitted}", "label": "x", "ratio": Infinity}}'.encode()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/take-cred", content=content, headers={"Content-Type": "application/json"}
        )
    assert res.status_code == 422
    # The raw secret appears NOWHERE in the response text.
    assert "SENTINEL" not in res.text
    assert "ZZZINJECT" not in res.text
    detail = res.json()["detail"]
    assert isinstance(detail, list)
    by_field = {err["loc"][-1]: err for err in detail}
    # Secret field: masked input, ctx dropped (stage 1).
    assert by_field["plex_token"]["input"] == "***"
    assert "ctx" not in by_field["plex_token"]
    # Non-finite float: rendered as its repr string, honestly echoed (stage 2).
    assert by_field["ratio"]["input"] == "inf"
