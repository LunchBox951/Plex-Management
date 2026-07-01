"""FastAPI application factory.

Wires the alpha web layer: the setup wizard and settings routers, the setup-guard
middleware, and a lifespan that prepares persistence + encryption. ``/health``
stays unauthenticated and outside the setup guard.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

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
from plex_manager.config import get_settings
from plex_manager.db import get_sessionmaker
from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.services import (
    eviction_service,
    import_service,
    log_capture_service,
    queue_service,
)
from plex_manager.services.health_service import ReconcileStatus
from plex_manager.web.deps import (
    EVICTION_INTERVAL_MINUTES_DEFAULT,
    ServiceNotConfiguredError,
    ensure_system_settings,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_eviction_enabled,
    get_eviction_filesystem,
    get_eviction_grace_days,
    get_eviction_interval_minutes,
    get_eviction_proactive_enabled,
    get_filesystem,
    get_library_optional,
    get_log_retention_days,
    get_movies_root_optional,
    get_parser,
    get_qbittorrent,
    get_quality_profile,
    get_tv_root_optional,
)
from plex_manager.web.middleware import SetupGuardMiddleware
from plex_manager.web.routers import blocklist as blocklist_router
from plex_manager.web.routers import discovery as discovery_router
from plex_manager.web.routers import ops as ops_router
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


def _get_reconcile_status(app: FastAPI) -> ReconcileStatus:
    """Return ``app.state.reconcile_status``, lazily creating it if absent.

    ``lifespan`` normally creates this once up front (alongside ``sessionmaker``/
    ``http_client``), but ``_reconcile_once`` is also called directly against a
    bare ``FastAPI()`` in tests (bypassing ``lifespan`` entirely — see
    ``tests/web/test_reconcile_loop.py``), so this stays defensive rather than
    assuming the attribute exists.
    """
    status = getattr(app.state, "reconcile_status", None)
    if not isinstance(status, ReconcileStatus):
        status = ReconcileStatus()
        app.state.reconcile_status = status
    return status


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe used by the container healthcheck and monitoring."""
    return {"status": "ok"}


async def _reconcile_once(app: FastAPI) -> None:
    """One reconcile + import + availability pass with a fresh session.

    Best-effort: if the download client isn't configured yet, the cycle is a
    no-op. The import drain runs whenever Plex is configured, REGARDLESS of
    whether either library root is set: ``movies_root`` / ``tv_root`` are each
    independently optional, and a row of that media type reaching
    ``import_download`` while its own root is unset surfaces its own honest,
    per-row ``ImportBlocked`` (never silently skipped, never gating the OTHER
    media type's import) — see ``import_service.run_import_cycle``. The
    reconciler is the single owner of cross-system truth (overview §5,
    north-star #5), so this — not a GET /queue poll — drives the loop, keeping the
    queue read fast and never blocking a request on a multi-GB copy.

    Stamps ``app.state.reconcile_status`` (ADR-0012's health dashboard signal):
    ``mark_run_started`` unconditionally at the top, ``mark_ok`` only if the WHOLE
    body below completes without raising — an exception that escapes this
    function (never the internally-handled ``QbittorrentError`` branch below,
    which the cycle tolerates and still finishes) is instead recorded by
    ``_reconcile_loop``'s own ``except``, which is the only place that actually
    sees it.
    """
    reconcile_status = _get_reconcile_status(app)
    reconcile_status.mark_run_started()
    sessionmaker = app.state.sessionmaker
    client = app.state.http_client
    async with sessionmaker() as session:
        library = await get_library_optional(session, client)

        # Download-client reconcile + import drain — needs qBittorrent. Skipped
        # when qBittorrent isn't configured.
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
                if library is not None:
                    movies_root = await get_movies_root_optional(session)
                    tv_root = await get_tv_root_optional(session)
                    await import_service.run_import_cycle(
                        fs=get_filesystem(),
                        library=library,
                        qbt=qbt,
                        parser=get_parser(),
                        profile=get_quality_profile(),
                        session=session,
                        movies_root=movies_root,
                        tv_root=tv_root,
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

    reconcile_status.mark_ok()


async def _reconcile_loop(app: FastAPI) -> None:
    """Run :func:`_reconcile_once` forever; one bad cycle never kills the loop."""
    while True:
        try:
            await _reconcile_once(app)
        except Exception as exc:
            _get_reconcile_status(app).mark_error(exc)
            _logger.exception("reconcile loop iteration failed; continuing")
        await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)


async def _log_drain_loop(app: FastAPI) -> None:
    """Sibling background task (own interval) draining the log-capture queue into
    ``log_events``, and periodically pruning past ``log_retention_days``.

    Reads ``app.state.log_handler`` (set by :func:`lifespan`) for the queue the
    synchronous :class:`~plex_manager.services.log_capture_service.
    LogCaptureHandler` feeds; a DB failure on either the drain or the prune is
    caught and logged, never left to kill the loop — a queue that cannot be
    drained this tick simply carries its backlog into the next one (bounded by
    the queue's own ``maxsize``, see the handler's docstring). ``drain_once`` is
    passed ``handler`` so a failed insert's whole (already-dequeued) batch is
    added to ``handler.dropped_count`` — never re-queued, but always honestly
    counted — before the exception below is caught.
    """
    handler = app.state.log_handler
    sessionmaker = app.state.sessionmaker
    last_pruned_at = time.monotonic()
    while True:
        try:
            async with sessionmaker() as session:
                repo = SqlLogEventRepository(session)
                await log_capture_service.drain_once(handler.queue, repo, handler=handler)
                await session.commit()
                if (
                    time.monotonic() - last_pruned_at
                    >= log_capture_service.LOG_PRUNE_INTERVAL_SECONDS
                ):
                    retention_days = await get_log_retention_days(session)
                    pruned = await log_capture_service.prune_once(repo, retention_days)
                    await session.commit()
                    last_pruned_at = time.monotonic()
                    if pruned:
                        _logger.info(
                            "pruned %d log_events row(s) older than %d day(s)",
                            pruned,
                            retention_days,
                        )
        except Exception:
            _logger.exception("log drain/prune tick failed; continuing")
        await asyncio.sleep(log_capture_service.LOG_DRAIN_INTERVAL_SECONDS)


async def _eviction_tick(app: FastAPI) -> float:
    """One disk-pressure eviction pass across every configured root.

    Returns the FRESHLY-read ``eviction_interval_minutes`` (in seconds) for
    :func:`_eviction_loop` to sleep — so a web-edited interval takes effect on
    the very next tick, no restart required. Every other setting (enabled,
    thresholds, grace, proactive) is likewise re-read every tick for the same
    reason. ``eviction_enabled=False`` short-circuits BOTH the pressure-triggered
    and the proactive sweep — the master, in-app "turn this bot off" switch
    (north-star #1: a correction is always a settings toggle, never a terminal).
    """
    sessionmaker = app.state.sessionmaker
    client = app.state.http_client
    async with sessionmaker() as session:
        interval_minutes = await get_eviction_interval_minutes(session)
        if not await get_eviction_enabled(session):
            return interval_minutes * 60.0

        library = await get_library_optional(session, client)
        if library is None:
            # Nothing is evictable without Plex to resolve watch state from --
            # never guess, never evict blind.
            return interval_minutes * 60.0

        threshold_pct = await get_disk_pressure_threshold_percent(session)
        target_pct = await get_disk_pressure_target_percent(session)
        grace_days = await get_eviction_grace_days(session)
        proactive_enabled = await get_eviction_proactive_enabled(session)
        movies_root = await get_movies_root_optional(session)
        tv_root = await get_tv_root_optional(session)
        fs = get_eviction_filesystem(movies_root, tv_root)

        roots: tuple[tuple[Literal["movie", "tv"], str | None], ...] = (
            ("movie", movies_root),
            ("tv", tv_root),
        )
        for media_type, root in roots:
            if not root:
                continue
            evicted = await eviction_service.run_eviction_sweep(
                session=session,
                library=library,
                fs=fs,
                media_type=media_type,
                root_path=root,
                threshold_pct=threshold_pct,
                target_pct=target_pct,
                grace_days=grace_days,
            )
            if evicted:
                _logger.info(
                    "evicted %d %s title(s) from %s under disk pressure",
                    len(evicted),
                    media_type,
                    root,
                )
            if proactive_enabled:
                # A SEPARATE pass, never gated on the pressure sweep above having
                # fired: opting in means "also clear past-grace watched content
                # regardless of usage". Candidates the pressure sweep already
                # evicted are naturally absent here (no longer `available`).
                proactive_evicted = await eviction_service.run_eviction_sweep(
                    session=session,
                    library=library,
                    fs=fs,
                    media_type=media_type,
                    root_path=root,
                    threshold_pct=threshold_pct,
                    target_pct=target_pct,
                    grace_days=grace_days,
                    proactive=True,
                )
                if proactive_evicted:
                    _logger.info(
                        "proactively evicted %d %s title(s) from %s (past grace)",
                        len(proactive_evicted),
                        media_type,
                        root,
                    )
    return interval_minutes * 60.0


async def _eviction_loop(app: FastAPI) -> None:
    """Run :func:`_eviction_tick` forever, on its OWN interval — a sibling to the
    15s reconcile tick and the log-drain loop, never the same schedule (a
    pressure sweep is far more expensive per-candidate than a reconcile pass:
    it resolves fresh Plex watch state and walks the filesystem for size). One
    bad tick never kills the loop; a failed tick falls back to re-reading the
    interval setting directly so a broken tick can never wedge the loop at some
    stale sleep duration.

    That fallback read opens its OWN session (the tick's session is already
    gone/rolled back by the time we're in the ``except``) — so it can raise
    too, e.g. the same transient DB hiccup that failed the tick in the first
    place. The outer ``try`` covers BOTH the tick attempt and that fallback: if
    either one raises, the iteration falls all the way back to the hardcoded
    ``EVICTION_INTERVAL_MINUTES_DEFAULT``. Nothing here is allowed to escape
    the loop (mirroring ``_reconcile_loop``'s "one bad cycle never kills the
    loop") — automatic disk-pressure eviction staying dead until a process
    restart would be a silent, terminal-requiring failure, which north star #2
    forbids.
    """
    while True:
        try:
            try:
                sleep_seconds = await _eviction_tick(app)
            except Exception:
                _logger.exception("eviction sweep tick failed; continuing")
                async with app.state.sessionmaker() as session:
                    sleep_seconds = (await get_eviction_interval_minutes(session)) * 60.0
        except Exception:
            _logger.exception(
                "eviction loop iteration failed even in its fallback path; "
                "sleeping the default interval and continuing"
            )
            sleep_seconds = EVICTION_INTERVAL_MINUTES_DEFAULT * 60.0
        await asyncio.sleep(sleep_seconds)


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

    Also wires the operability beta (ADR-0012): ``app.state.reconcile_status``
    (the health dashboard's "is the loop running" signal, mutated by
    ``_reconcile_once``/``_reconcile_loop`` above), the log-capture handler
    (``config.log_level`` applied to the root logger here, for the first time —
    previously defined but unused), and its sibling drain/eviction background
    tasks — each on its OWN interval, never the 15s reconcile tick.
    """
    maker = get_sessionmaker()
    app.state.sessionmaker = maker
    async with maker() as session:
        system = await ensure_system_settings(session)
        initialized = system.initialized
        await session.commit()
    prepare_encryption(initialized=initialized)

    app.state.http_client = httpx.AsyncClient(timeout=30.0)
    app.state.reconcile_status = ReconcileStatus()

    log_handler = log_capture_service.configure_logging(get_settings().log_level)
    app.state.log_handler = log_handler

    # The background reconciler closes the request -> grab -> import -> available
    # loop without a GET /queue poll having to do the heavy work. The log-drain
    # and eviction tasks are its SIBLINGS (own intervals, same lifecycle).
    reconcile_task = asyncio.create_task(_reconcile_loop(app))
    log_drain_task = asyncio.create_task(_log_drain_loop(app))
    eviction_task = asyncio.create_task(_eviction_loop(app))
    background_tasks = (reconcile_task, log_drain_task, eviction_task)
    try:
        yield
    finally:
        for task in background_tasks:
            task.cancel()
        # Await every cancelled task so its cleanup runs; return_exceptions=True
        # absorbs the expected CancelledError without re-raising on shutdown.
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await app.state.http_client.aclose()
        log_capture_service.stop_logging(log_handler)


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
    app.include_router(ops_router.router)
    # Mount the built SPA LAST so its catch-all fallback has the lowest match
    # priority (no-op when the frontend hasn't been built; see spa.mount_spa).
    mount_spa(app)
    return app


app = create_app()
