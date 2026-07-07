"""Structured error envelope for auth/setup flows (north star #3).

``detail`` stays the stable machine code the SPA keys on; ``message``/``hint``
say what happened and what to do; ``diagnostics`` carries only non-secret
context. Raising :class:`AppError` (or the adapter's ``PlexVerifyError``)
anywhere under FastAPI renders the envelope via :func:`install_error_handlers`,
which ``web/app.py`` wires into the application.

:func:`install_error_handlers` also overrides FastAPI's DEFAULT
``RequestValidationError`` handler with :func:`_request_validation_error_handler`,
which sanitizes the 422 body in two ordered stages before it is rendered:
secret-field redaction first (a rejected credential must never be echoed back
-- see :func:`_redact_secret_fields`), then non-finite-float sanitization (a
raw ``inf``/``nan`` would crash strict-JSON rendering -- issue #92, see
:func:`_sanitize_validation_error_value`). The standard ``{"detail": [...]}``
envelope is preserved verbatim.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Final, cast

from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from plex_manager.adapters.plex.oauth import PlexVerifyError

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import Response

__all__ = ["AppError", "install_error_handlers"]

_REDACTED: Final = "***"

# Request-body fields whose value is a credential/secret. A validation error on
# one of these must NEVER echo the submitted value back in the 422 body: FastAPI's
# DEFAULT RequestValidationError handler returns each error's raw ``input``, so a
# rejected ``plex_token`` / ``prowlarr_api_key`` (e.g. one carrying a CR/LF the
# header-safety validator refused) would come straight back in the error body --
# undoing the guard and leaking the credential (north star #3: secrets never
# leak). Matched against every element of an error's ``loc`` so a nested body
# field is covered too. These names appear ONLY as secret fields in the request
# models; over-matching a same-named non-secret field would merely hide an input
# from a debug body, never expose one.
_SECRET_REQUEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "plex_token",
        "prowlarr_api_key",
        "qbittorrent_password",
        "tmdb_api_key",
        "auth_token",
        "token",
        "api_key",
        "password",
    }
)

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


def _scrub_secret_values(value: Any) -> Any:
    """Recursively mask any secret-field-keyed value nested in an echoed
    ``input``/``ctx``. A validation error's ``input`` is often the WHOLE request
    body (e.g. a ``missing`` required-field error, or a model-level validator), so
    a secret VALUE can ride along even when the error's own ``loc`` names a
    non-secret field -- this masks the secret wherever it appears while leaving
    non-secret values intact for debuggability. Non-container values pass through
    (a scalar is masked by the caller only when its ``loc`` targets a secret field).
    """
    if isinstance(value, dict):
        scrubbed: dict[Any, Any] = {}
        for key, item in cast("dict[Any, Any]", value).items():
            if isinstance(key, str) and key in _SECRET_REQUEST_FIELDS:
                scrubbed[key] = _REDACTED
            else:
                scrubbed[key] = _scrub_secret_values(item)
        return scrubbed
    if isinstance(value, (list, tuple)):
        return [_scrub_secret_values(item) for item in cast("list[Any]", value)]
    return value


def _redact_secret_fields(error: dict[str, Any]) -> dict[str, Any]:
    """Strip secret values from a validation error before it is serialized.

    Two leak vectors are closed: (1) an error whose ``loc`` targets a secret field
    (see :data:`_SECRET_REQUEST_FIELDS`) has ``input`` equal to the rejected secret
    itself -- the whole ``input`` is replaced with ``***`` and ``ctx`` (which can
    carry the raised value) is dropped; (2) an error on a NON-secret field can still
    echo the whole request body in ``input``/``ctx`` -- any secret-keyed value in
    there is recursively masked while non-secret values stay for debuggability.
    ``msg`` for the credential validators is a static, value-free string, so the
    ``{"detail": [...]}`` envelope stays intact and parseable.
    """
    loc = error.get("loc", ())
    targets_secret = any(isinstance(part, str) and part in _SECRET_REQUEST_FIELDS for part in loc)
    redacted = dict(error)
    if "input" in redacted:
        redacted["input"] = _REDACTED if targets_secret else _scrub_secret_values(redacted["input"])
    if targets_secret:
        redacted.pop("ctx", None)
    elif "ctx" in redacted:
        redacted["ctx"] = _scrub_secret_values(redacted["ctx"])
    return redacted


# Ordered pipeline of PER-ERROR sanitizers -- stage 1 of the composed
# RequestValidationError handler, applied to each raw ``exc.errors()`` dict
# BEFORE ``jsonable_encoder``. Each sanitizer is pure (dict in, dict out) and
# additive, so a future sanitizer composes by joining this tuple; the handler
# and its ``{"detail": [...]}`` envelope contract need no rewrite. (The
# non-finite-float sanitizer is deliberately NOT in this tuple: it is stage 2,
# a value-level pass over the whole ``jsonable_encoder`` output -- see
# ``_request_validation_error_handler`` for the two-stage composition.)
_VALIDATION_ERROR_SANITIZERS: Final[tuple[Callable[[dict[str, Any]], dict[str, Any]], ...]] = (
    _redact_secret_fields,
)


def _sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run every validation-error dict through the sanitizer pipeline, in order."""
    sanitized: list[dict[str, Any]] = []
    for error in errors:
        current = error
        for sanitize in _VALIDATION_ERROR_SANITIZERS:
            current = sanitize(current)
        sanitized.append(current)
    return sanitized


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
    """Render a request-body validation failure as a SANITIZED 422 -- never a 500,
    never a secret echo.

    FastAPI's DEFAULT handler for this exception echoes the REJECTED raw value
    back in each error item's ``"input"`` key (so the operator can see exactly
    what was rejected) via
    ``JSONResponse(content={"detail": jsonable_encoder(exc.errors())})`` -- the
    standard ``HTTPValidationError`` envelope (``{"detail": [...]}``) that
    ``docs/api/openapi.json`` documents and the generated frontend client is
    typed against; ``frontend/src/lib/errors.ts``'s ``extractDetail`` also keys
    on a top-level ``detail``. This override MUST preserve that envelope -- it
    only changes what goes *inside* it, in two ORDERED stages:

    1. **Secret redaction** (:func:`_sanitize_validation_errors`, per raw error
       dict): a rejected credential (e.g. a CR/LF ``plex_token`` the
       header-safety validator refused) must never come back in the body --
       the default ``input`` echo would undo the guard. Runs FIRST so no raw
       secret ever reaches a later stage; it only inserts the static ``"***"``
       mask (never a float), so it cannot re-introduce what stage 2 removes.
    2. **Non-finite-float sanitization** (:func:`_sanitize_validation_error_value`,
       over the whole ``jsonable_encoder`` output): Starlette's ``JSONResponse``
       renders with ``json.dumps(..., allow_nan=False)`` (strict RFC-8259
       JSON), which RAISES on a non-finite float -- so a rejected ``inf``/
       ``nan`` is replaced with its ``repr()`` string. This is a PRE-EXISTING
       latent bug, not one issue #92 introduced: any field already bounded
       with ``ge``/``le`` (e.g. ``disk_pressure_threshold_percent``'s
       ``ge=0, le=100``) already rejected a raw ``NaN`` (NaN comparisons are
       always ``False``) and 500'd the same way. Fixing it generically here
       closes the bug everywhere at once. Runs LAST, on the final JSON-shaped
       payload, so nothing can slip past it; it only rewrites non-finite
       floats to ``"inf"``/``"nan"``/``"-inf"`` strings, so it cannot unmask
       what stage 1 redacted.

    Both stages keep the envelope strictly-valid JSON while still telling the
    operator exactly what was rejected for every NON-secret field (honesty over
    silence: a validation failure must 422 cleanly -- never crash while
    reporting, never leak while explaining).
    """
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - registered per exact type
        raise exc
    redacted = _sanitize_validation_errors(list(exc.errors()))
    payload = _sanitize_validation_error_value(jsonable_encoder(redacted))
    return JSONResponse(status_code=422, content={"detail": payload})


def install_error_handlers(app: FastAPI) -> None:
    """Register the envelope handlers for :class:`AppError` and ``PlexVerifyError``,
    plus the ONE composed ``RequestValidationError`` override: secret-field
    redaction first, then the non-finite-safe rendering pass -- so a rejected
    credential is never echoed back and a rejected ``inf``/``nan`` never crashes
    the 422 (see :func:`_request_validation_error_handler`)."""
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(PlexVerifyError, _plex_verify_error_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_error_handler)
