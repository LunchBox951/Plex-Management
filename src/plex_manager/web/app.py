"""FastAPI application factory.

Wires the alpha web layer: the setup wizard and settings routers, the setup-guard
middleware, and a lifespan that prepares persistence + encryption. ``/health``
stays unauthenticated and outside the setup guard.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from plex_manager import __version__
from plex_manager.adapters.encryption import prepare_encryption
from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.adapters.prowlarr import IndexerError, IndexerRateLimitError
from plex_manager.adapters.qbittorrent import QbittorrentAuthError, QbittorrentError
from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError
from plex_manager.db import get_sessionmaker
from plex_manager.services import import_service, queue_service
from plex_manager.web.deps import (
    ServiceNotConfiguredError,
    ensure_system_settings,
    get_filesystem,
    get_library_optional,
    get_movies_root_optional,
    get_parser,
    get_qbittorrent,
    get_quality_profile,
)
from plex_manager.web.middleware import SetupGuardMiddleware
from plex_manager.web.routers import blocklist as blocklist_router
from plex_manager.web.routers import discovery as discovery_router
from plex_manager.web.routers import quality_profile as quality_profile_router
from plex_manager.web.routers import queue as queue_router
from plex_manager.web.routers import requests as requests_router
from plex_manager.web.routers import search_preview as search_preview_router
from plex_manager.web.routers import settings as settings_router
from plex_manager.web.routers import setup as setup_router
from plex_manager.web.spa import mount_spa

router = APIRouter()

_logger = logging.getLogger(__name__)

# How often the background reconciler reconciles the client, drains imports, and
# confirms availability. A constant for the beta (a configurable interval / a
# dedicated worker are noted follow-ups).
_RECONCILE_INTERVAL_SECONDS = 15.0


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe used by the container healthcheck and monitoring."""
    return {"status": "ok"}


async def _reconcile_once(app: FastAPI) -> None:
    """One reconcile + import + availability pass with a fresh session.

    Best-effort: if the download client isn't configured yet, the cycle is a no-op;
    import + availability only run when Plex and the Movies root are configured too.
    The reconciler is the single owner of cross-system truth (overview §5,
    north-star #5), so this — not a GET /queue poll — drives the loop, keeping the
    queue read fast and never blocking a request on a multi-GB copy.
    """
    sessionmaker = app.state.sessionmaker
    client = app.state.http_client
    async with sessionmaker() as session:
        library = await get_library_optional(session, client)

        # Download-client reconcile + import drain — needs qBittorrent (+ the Movies
        # root for the drain). Skipped when qBittorrent isn't configured.
        try:
            qbt = await get_qbittorrent(session, client)
        except ServiceNotConfiguredError:
            qbt = None
        if qbt is not None:
            # A qBittorrent outage / auth-failure must not abort the cycle before the
            # Plex-only availability pass below. reconcile_and_list ->
            # qbt.get_all_statuses() raises QbittorrentError when the client is
            # unreachable or rejects the login (QbittorrentAuthError is a subclass, so
            # it is covered too); the adapter wraps every httpx transport/status error
            # into QbittorrentError, so that one type is the whole surface. Surface it
            # (honesty over silence: the type name only, never a secret) and roll the
            # shared session back so a mid-reconcile partial write can't taint the
            # availability commit, then fall through — no request stuck in "Finalizing"
            # while qBittorrent is down.
            try:
                await queue_service.reconcile_and_list(qbt, session)
                movies_root = await get_movies_root_optional(session)
                if library is not None and movies_root:
                    await import_service.run_import_cycle(
                        fs=get_filesystem(),
                        library=library,
                        qbt=qbt,
                        parser=get_parser(),
                        profile=get_quality_profile(),
                        session=session,
                        movies_root=movies_root,
                    )
            except QbittorrentError as exc:
                await session.rollback()
                _logger.warning(
                    "qBittorrent reconcile/import skipped this cycle (%s); "
                    "running availability pass anyway",
                    type(exc).__name__,
                )

        # Availability promotion (completed -> available) needs ONLY Plex, so it runs
        # even when qBittorrent is down or the Movies root was cleared after an
        # import already triggered a scan — no request stuck in "Finalizing".
        if library is not None:
            await import_service.run_availability_cycle(library=library, session=session)


async def _reconcile_loop(app: FastAPI) -> None:
    """Run :func:`_reconcile_once` forever; one bad cycle never kills the loop."""
    while True:
        try:
            await _reconcile_once(app)
        except Exception:
            _logger.exception("reconcile loop iteration failed; continuing")
        await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)


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
#
# Subclass relationships (e.g. QbittorrentAuthError < QbittorrentError,
# IndexerRateLimitError < IndexerError) are disambiguated by Starlette, which
# resolves the handler by walking the exception's MRO and picking the most
# specific registered type. The handler itself then keys on the EXACT type, so a
# base-class outage and its auth/rate-limit subclass map to distinct details.
_ADAPTER_ERROR_RESPONSES: dict[type[Exception], tuple[int, str]] = {
    IndexerRateLimitError: (503, "indexer_rate_limited"),
    IndexerError: (503, "indexer_unavailable"),
    TmdbAuthError: (502, "tmdb_auth_failed"),
    TmdbApiError: (502, "tmdb_unavailable"),
    QbittorrentAuthError: (502, "qbittorrent_auth_failed"),
    QbittorrentError: (502, "qbittorrent_unavailable"),
    PlexAuthError: (502, "plex_auth_failed"),
    PlexLibraryError: (502, "plex_unavailable"),
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
    # The background reconciler closes the request -> grab -> import -> available
    # loop without a GET /queue poll having to do the heavy work.
    reconcile_task = asyncio.create_task(_reconcile_loop(app))
    try:
        yield
    finally:
        reconcile_task.cancel()
        # Await the cancelled task so its cleanup runs; return_exceptions=True
        # absorbs the expected CancelledError without re-raising on shutdown.
        await asyncio.gather(reconcile_task, return_exceptions=True)
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
    # Mount the built SPA LAST so its catch-all fallback has the lowest match
    # priority (no-op when the frontend hasn't been built; see spa.mount_spa).
    mount_spa(app)
    return app


app = create_app()
