"""Structured error envelope for auth/setup flows (north star #3).

``detail`` stays the stable machine code the SPA keys on; ``message``/``hint``
say what happened and what to do; ``diagnostics`` carries only non-secret
context. Raising :class:`AppError` (or the adapter's ``PlexVerifyError``)
anywhere under FastAPI renders the envelope via :func:`install_error_handlers`,
which ``web/app.py`` wires into the application.

:func:`install_error_handlers` also overrides FastAPI's DEFAULT
``RequestValidationError`` handler with :func:`_request_validation_error_handler`
-- see that function's docstring for why a plain 422 body is not safe to render
as-is when the rejected value was a non-finite float (issue #92).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final, cast

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from plex_manager.adapters.plex.oauth import PlexVerifyError

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import Response

__all__ = ["AppError", "install_error_handlers"]

# plex.tv / the Plex server gave an unusable answer during verification: an
# honest, retryable upstream state, never an opaque 500 (north star #3).
_PLEX_VERIFY_STATUS: Final = 502


class AppError(Exception):
    """A failure with a stable machine ``code`` plus human ``message``/``hint``.

    ``code`` is the stable identifier the SPA's humanizer keys on (kept as the
    wire ``detail``); ``message``/``hint`` are operator-facing prose; optional
    ``diagnostics`` carries only NON-secret context (host, status, ...). Raising
    it anywhere under FastAPI renders the envelope with ``status_code``.
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        hint: str | None = None,
        diagnostics: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint
        self.diagnostics = diagnostics


def _envelope(
    *,
    status_code: int,
    code: str,
    message: str,
    hint: str | None = None,
    diagnostics: dict[str, str] | None = None,
) -> JSONResponse:
    """Serialize the envelope, omitting ``hint``/``diagnostics`` when empty.

    ``detail`` (the machine code) and ``message`` are always present; a falsy
    ``hint`` or ``diagnostics`` (``None`` OR an empty dict) is dropped so the
    body never carries an empty, meaningless key.
    """
    content: dict[str, object] = {"detail": code, "message": message}
    if hint:
        content["hint"] = hint
    if diagnostics:
        content["diagnostics"] = diagnostics
    return JSONResponse(status_code=status_code, content=content)


async def _app_error_handler(request: Request, exc: Exception) -> Response:
    """Render an :class:`AppError` as its structured envelope + status."""
    if not isinstance(exc, AppError):  # pragma: no cover - registered per exact type
        raise exc
    return _envelope(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        hint=exc.hint,
        diagnostics=exc.diagnostics,
    )


async def _plex_verify_error_handler(request: Request, exc: Exception) -> Response:
    """Render a ``PlexVerifyError`` as a 502 envelope, code-agnostic.

    Generic over every verification code the adapter emits
    (``plex_tv_unreachable_server`` / ``plex_token_invalid`` /
    ``plex_tv_bad_response`` / ``server_identity_failed`` /
    ``server_unreachable_from_backend``): ``detail`` is the raised ``code`` and
    ``diagnostics`` is the adapter's non-secret context, passed straight through.
    """
    if not isinstance(exc, PlexVerifyError):  # pragma: no cover - registered per exact type
        raise exc
    return _envelope(
        status_code=_PLEX_VERIFY_STATUS,
        code=exc.code,
        message=str(exc),
        diagnostics=exc.diagnostics,
    )


def _sanitize_validation_error_value(value: object) -> object:
    """Recursively replace a non-finite ``float`` with its ``repr()`` string.

    Recurses into ``dict``/``list`` only -- the two container shapes
    ``jsonable_encoder(ValidationError.errors())`` ever produces (each error
    item's ``"input"``/``"ctx"`` keys can themselves hold nested structures,
    e.g. a rejected list-of-floats body). Every other value (str, int, bool,
    None, an already-finite float) passes through unchanged.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)  # "inf" / "nan" / "-inf" -- readable, JSON-safe
    if isinstance(value, dict):
        # ``jsonable_encoder`` output is JSON-shaped: every dict key is a str.
        typed_dict = cast("dict[str, object]", value)
        return {key: _sanitize_validation_error_value(item) for key, item in typed_dict.items()}
    if isinstance(value, list):
        typed_list = cast("list[object]", value)
        return [_sanitize_validation_error_value(item) for item in typed_list]
    return value


async def _request_validation_error_handler(request: Request, exc: Exception) -> Response:
    """Render a request-body validation failure as 422 -- never a 500.

    FastAPI's DEFAULT handler for this exception echoes the REJECTED raw value
    back in each error item's ``"input"`` key (so the operator can see exactly
    what was rejected) via
    ``JSONResponse(content={"detail": jsonable_encoder(exc.errors())})`` -- the
    standard ``HTTPValidationError`` envelope (``{"detail": [...]}``) that
    ``docs/api/openapi.json`` documents and the generated frontend client is
    typed against; ``frontend/src/lib/errors.ts``'s ``extractDetail`` also keys
    on a top-level ``detail``. This override MUST preserve that envelope -- it
    only changes what goes *inside* it. That is fine for almost every rejected
    value -- except a non-finite float
    (``inf``/``nan``): Starlette's ``JSONResponse`` renders with
    ``json.dumps(..., allow_nan=False)`` (strict RFC-8259 JSON), which RAISES
    on a non-finite float.

    This is a PRE-EXISTING latent bug, not one issue #92 introduces: any field
    already bounded with ``ge``/``le`` (e.g. ``disk_pressure_threshold_percent``'s
    ``ge=0, le=100``) already rejects a raw ``NaN`` today, because a NaN
    comparison is always ``False`` -- ``nan >= 0`` is ``False`` just like
    ``nan <= 100`` is, so it already fails that bound and 500s the SAME way,
    with or without this PR. Issue #92 just adds MORE fields where this was
    reachable (``eviction_interval_minutes``'s new ``le=`` now also rejects
    ``+Infinity``, which its old ``gt=0``-only bound let through). Fixing it
    generically here closes the bug everywhere at once rather than only for
    the three newly-bounded settings fields. Sanitizing the echoed value to
    its ``repr()`` keeps the response strictly valid JSON while still telling
    the operator exactly what was rejected (honesty over silence: this must
    422 cleanly, never crash while reporting a validation failure).
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registered per exact type
        raise exc
    errors = _sanitize_validation_error_value(jsonable_encoder(exc.errors()))
    return JSONResponse(status_code=422, content={"detail": errors})


def install_error_handlers(app: FastAPI) -> None:
    """Register the envelope handlers for :class:`AppError` and ``PlexVerifyError``,
    plus the non-finite-safe override for FastAPI's own request-validation 422."""
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(PlexVerifyError, _plex_verify_error_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_error_handler)
