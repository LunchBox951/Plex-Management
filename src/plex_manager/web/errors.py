"""Structured error envelope for auth/setup flows (north star #3).

``detail`` stays the stable machine code the SPA keys on; ``message``/``hint``
say what happened and what to do; ``diagnostics`` carries only non-secret
context. Raising :class:`AppError` (or the adapter's ``PlexVerifyError``)
anywhere under FastAPI renders the envelope via :func:`install_error_handlers`,
which ``web/app.py`` wires into the application.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

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


def install_error_handlers(app: FastAPI) -> None:
    """Register the envelope handlers for :class:`AppError` and ``PlexVerifyError``."""
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(PlexVerifyError, _plex_verify_error_handler)
