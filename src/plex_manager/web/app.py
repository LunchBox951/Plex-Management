"""FastAPI application factory.

Wires the alpha web layer: the setup wizard and settings routers, the setup-guard
middleware, and a lifespan that prepares persistence + encryption. ``/health``
stays unauthenticated and outside the setup guard.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from plex_manager import __version__
from plex_manager.adapters.encryption import prepare_encryption
from plex_manager.adapters.prowlarr import IndexerRateLimitError
from plex_manager.adapters.qbittorrent import QbittorrentAuthError
from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError
from plex_manager.db import get_sessionmaker
from plex_manager.web.deps import ServiceNotConfiguredError, ensure_system_settings
from plex_manager.web.middleware import SetupGuardMiddleware
from plex_manager.web.routers import blocklist as blocklist_router
from plex_manager.web.routers import discovery as discovery_router
from plex_manager.web.routers import quality_profile as quality_profile_router
from plex_manager.web.routers import queue as queue_router
from plex_manager.web.routers import requests as requests_router
from plex_manager.web.routers import search_preview as search_preview_router
from plex_manager.web.routers import settings as settings_router
from plex_manager.web.routers import setup as setup_router

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe used by the container healthcheck and monitoring."""
    return {"status": "ok"}


async def _service_not_configured_handler(request: Request, exc: Exception) -> Response:
    """Render :class:`ServiceNotConfiguredError` as an honest 409 (no crash)."""
    service = exc.service if isinstance(exc, ServiceNotConfiguredError) else "unknown"
    return JSONResponse(
        status_code=409,
        content={"detail": "service_not_configured", "service": service},
    )


# Typed, actionable adapter errors -> honest HTTP states (status, detail). The
# adapters deliberately raise these instead of swallowing the failure; mapping
# them here keeps the reason visible at the boundary (honesty over silence) so
# the UI can offer 'retry later' / 're-check credentials' instead of an opaque
# 500. The error TYPES guarantee no secret is in the message, but we return a
# fixed detail string (never ``str(exc)``) so nothing can leak by accident.
_ADAPTER_ERROR_RESPONSES: dict[type[Exception], tuple[int, str]] = {
    IndexerRateLimitError: (503, "indexer_rate_limited"),
    TmdbAuthError: (502, "tmdb_auth_failed"),
    TmdbApiError: (502, "tmdb_unavailable"),
    QbittorrentAuthError: (502, "qbittorrent_auth_failed"),
}


async def _adapter_error_handler(request: Request, exc: Exception) -> Response:
    """Render a typed adapter error as its mapped honest status + detail."""
    status_code, detail = _ADAPTER_ERROR_RESPONSES.get(type(exc), (502, "upstream_error"))
    return JSONResponse(status_code=status_code, content={"detail": detail})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Prepare persistence + encryption, then share an HTTP client for adapters.

    Ensures a ``system_settings`` row exists (uninitialized on a fresh install)
    and then calls :func:`prepare_encryption` with that flag — so a lost key on an
    already-initialized install aborts startup with a clear error instead of
    silently serving undecryptable data.
    """
    maker = get_sessionmaker()
    app.state.sessionmaker = maker
    async with maker() as session:
        system = await ensure_system_settings(session)
        initialized = system.initialized
        await session.commit()
    prepare_encryption(initialized=initialized)

    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(title="Plex Manager", version=__version__, lifespan=lifespan)
    app.add_middleware(SetupGuardMiddleware)
    app.add_exception_handler(ServiceNotConfiguredError, _service_not_configured_handler)
    for adapter_error in _ADAPTER_ERROR_RESPONSES:
        app.add_exception_handler(adapter_error, _adapter_error_handler)
    app.include_router(router)
    app.include_router(setup_router.router)
    app.include_router(settings_router.router)
    app.include_router(discovery_router.router)
    app.include_router(requests_router.router)
    app.include_router(search_preview_router.router)
    app.include_router(queue_router.router)
    app.include_router(blocklist_router.router)
    app.include_router(quality_profile_router.router)
    return app


app = create_app()
