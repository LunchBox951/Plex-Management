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
from plex_manager.services.update_coordination_service import (
    MaintenanceDrainingError,
    MaintenanceLeaseLostError,
    UpdateCoordinationService,
)
from plex_manager.web.deps import load_system_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.requests import Request
    from starlette.responses import Response

__all__ = [
    "SETUP_ALLOWLIST_PATHS",
    "SETUP_ALLOWLIST_PREFIXES",
    "CriticalMutationMiddleware",
    "SetupGuardMiddleware",
]

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MAINTENANCE_EXCLUDED_PREFIXES = (
    "/api/v1/auth",
    "/api/v1/setup",
    # Recovery-key rotation/revocation is an auth-domain write, not media or
    # updater-critical work. Keeping it available also preserves its own
    # concurrency/CAS boundary instead of serializing outside that lock.
    "/api/v1/settings/app-key",
    "/api/v1/updates",
    "/api/v1/internal/updates",
)

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


class CriticalMutationMiddleware(BaseHTTPMiddleware):
    """Lease state-changing API work so an updater drain cannot race it.

    The request-creation endpoint is deliberately excluded: requests remain
    accepted during the short maintenance drain, while background auto-grab is
    leased separately and therefore leaves the critical handoff queued.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if (
            request.method in _SAFE_METHODS
            or not path.startswith("/api/v1/")
            or (request.method == "POST" and path.rstrip("/") == "/api/v1/requests")
            or any(path.startswith(prefix) for prefix in _MAINTENANCE_EXCLUDED_PREFIXES)
        ):
            return await call_next(request)

        coordinator = getattr(request.app.state, "update_coordinator", None)
        if not isinstance(coordinator, UpdateCoordinationService):
            maker_obj = getattr(request.app.state, "sessionmaker", None)
            maker = (
                cast("async_sessionmaker[AsyncSession]", maker_obj)
                if isinstance(maker_obj, async_sessionmaker)
                else get_sessionmaker()
            )
            coordinator = UpdateCoordinationService(maker)
            try:
                await coordinator.initialize()
            except Exception:
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": "maintenance_coordinator_unavailable",
                        "message": "A safe mutation lease could not be established.",
                    },
                )
            request.app.state.update_coordinator = coordinator
        try:
            async with coordinator.critical_operation(f"http_{request.method.lower()}"):
                return await call_next(request)
        except MaintenanceDrainingError:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "maintenance_in_progress",
                    "message": "Container update maintenance is draining critical work.",
                    "hint": "Try again after the update finishes.",
                },
            )
        except MaintenanceLeaseLostError:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "maintenance_lease_lost",
                    "message": "The mutation finished with uncertain maintenance ownership.",
                    "hint": "Refresh the affected resource before retrying.",
                },
            )
