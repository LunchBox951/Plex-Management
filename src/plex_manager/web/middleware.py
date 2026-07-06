"""SetupGuardMiddleware — block the API until first-run setup completes.

Until ``SystemSettings.initialized`` is True, only a small allowlist of paths is
reachable: health, the setup sub-API, the AUTH sub-API (``/api/v1/auth``), the
docs, and ``/``. Sign-in must work before init — it IS the first setup step: a
fresh install is claimed by the first Plex server owner to sign in, so
``/api/v1/auth`` cannot be gated behind the very init it establishes. Every other
path gets an API-appropriate ``409`` with ``{"detail": "setup_required",
"setup_path": "/setup"}`` — a machine-readable guard for the SPA, not a browser
redirect.

The initialized flag is read once per request from the app's sessionmaker
(``request.app.state.sessionmaker``, set by the lifespan; falls back to the
process-wide one). The allowlist short-circuits before any DB query, so ``/health``
stays dependency-free and unauthenticated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from plex_manager.db import get_sessionmaker
from plex_manager.web.deps import load_system_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.requests import Request
    from starlette.responses import Response

__all__ = ["SETUP_ALLOWLIST_PATHS", "SETUP_ALLOWLIST_PREFIXES", "SetupGuardMiddleware"]

# Exact paths always reachable pre-init.
SETUP_ALLOWLIST_PATHS: frozenset[str] = frozenset(
    {"/", "/health", "/docs", "/redoc", "/openapi.json"}
)
# Path prefixes always reachable pre-init: the whole setup sub-API AND the auth
# sub-API. Sign-in must work before init — it IS the first setup step (the first
# Plex server owner to sign in claims the install), so it can't sit behind the
# 409 the way the rest of ``/api/`` does.
SETUP_ALLOWLIST_PREFIXES: tuple[str, ...] = ("/api/v1/setup", "/api/v1/auth")


def _is_allowed(path: str) -> bool:
    """Whether ``path`` is reachable before the install is initialized."""
    if path in SETUP_ALLOWLIST_PATHS:
        return True
    if any(path.startswith(prefix) for prefix in SETUP_ALLOWLIST_PREFIXES):
        return True
    # The SPA shell, its hashed assets, and client-side routes (e.g. ``/setup``,
    # ``/queue``) carry no secrets and must render before first-run setup so the
    # wizard is reachable. Only the protected API is gated pre-init: everything
    # under ``/api/`` except the setup and auth sub-APIs (allowed above) still
    # gets the 409.
    return not path.startswith("/api/")


class SetupGuardMiddleware(BaseHTTPMiddleware):
    """Return 409 ``setup_required`` for protected paths until initialized."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if _is_allowed(request.url.path) or await self._is_initialized(request):
            return await call_next(request)
        return JSONResponse(
            status_code=409,
            content={"detail": "setup_required", "setup_path": "/setup"},
        )

    @staticmethod
    async def _is_initialized(request: Request) -> bool:
        """Read the install-state flag once for this request."""
        maker_obj = getattr(request.app.state, "sessionmaker", None)
        maker: async_sessionmaker[AsyncSession]
        if isinstance(maker_obj, async_sessionmaker):
            maker = cast("async_sessionmaker[AsyncSession]", maker_obj)
        else:
            maker = get_sessionmaker()
        async with maker() as session:
            system = await load_system_settings(session)
            return system is not None and system.initialized
