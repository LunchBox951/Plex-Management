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
from typing import Any, Literal, cast

import httpx
from fastapi import APIRouter, FastAPI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from plex_manager import __version__
from plex_manager.adapters.encryption import prepare_encryption
from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.adapters.prowlarr import IndexerError, IndexerRateLimitError
from plex_manager.adapters.qbittorrent import (
    QbittorrentAuthError,
    QbittorrentError,
    QbittorrentSourceError,
)
from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError
from plex_manager.config import get_settings
from plex_manager.db import get_sessionmaker
from plex_manager.domain.disk_usage import used_percent
from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.services import (
    auto_grab_service,
    eviction_service,
    import_service,
    log_capture_service,
    queue_service,
    retention_telemetry_service,
)
from plex_manager.services.health_service import (
    AutograbStatus,
    ReconcileStatus,
    read_disk_usage,
)
from plex_manager.web.deps import (
    CSRF_HEADER_NAME,
    EVICTION_INTERVAL_MINUTES_DEFAULT,
    SESSION_COOKIE_NAME,
    ServiceNotConfiguredError,
    ensure_system_settings,
    get_anime_movie_root_optional,
    get_anime_tv_root_optional,
    get_auto_grab_enabled,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_downloads_host_root,
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
    get_prowlarr,
    get_qbittorrent,
    get_quality_profile,
    get_tv_root_optional,
)
from plex_manager.web.errors import install_error_handlers
from plex_manager.web.middleware import SetupGuardMiddleware
from plex_manager.web.routers import auth as auth_router
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

# How often the auto-grab worker scans for due requests/seasons and searches
# Prowlarr (ADR-0013). Slower than the 15s reconcile tick on purpose: the tick
# itself is cheap (a due-scope query bounded by a per-scope backoff ladder), but a
# tick that DOES find work runs real Prowlarr searches, so a 60s base cadence plus
# the per-cycle search cap keeps the single Prowlarr from being hammered. A
# constant for the beta, mirroring ``_RECONCILE_INTERVAL_SECONDS``.
_AUTOGRAB_INTERVAL_SECONDS = 60.0


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


def _get_autograb_status(app: FastAPI) -> AutograbStatus:
    """Return ``app.state.autograb_status``, lazily creating it if absent.

    The exact mirror of :func:`_get_reconcile_status`: ``lifespan`` creates this
    once up front, but ``_autograb_once`` is also called directly against a bare
    ``FastAPI()`` in tests (bypassing ``lifespan`` -- see
    ``tests/web/test_autograb_loop.py``), so this stays defensive.
    """
    status = getattr(app.state, "autograb_status", None)
    if not isinstance(status, AutograbStatus):
        status = AutograbStatus()
        app.state.autograb_status = status
    return status


def _get_autograb_cooldowns(app: FastAPI) -> auto_grab_service.CooldownRegistry:
    """Return ``app.state.autograb_cooldowns``, lazily creating it if absent.

    The in-process grab-pipeline cooldown registry (ADR-0013 round-3 #2): scopes
    whose grab keeps raising ``GrabError``, mapped to their escalating
    retry-not-before. Owned HERE, not in the service, so it survives across ticks; a
    process restart clears it, exactly like ``AutograbStatus``. ``lifespan`` creates
    it once, but ``_autograb_once`` is also driven against a bare ``FastAPI()`` in
    tests (bypassing ``lifespan``), so this stays defensive like
    :func:`_get_autograb_status`.
    """
    cooldowns: auto_grab_service.CooldownRegistry | None = getattr(
        app.state, "autograb_cooldowns", None
    )
    if cooldowns is None:
        cooldowns = {}
        app.state.autograb_cooldowns = cooldowns
    return cooldowns


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
                    anime_movie_root = await get_anime_movie_root_optional(session)
                    anime_tv_root = await get_anime_tv_root_optional(session)
                    await import_service.run_import_cycle(
                        fs=get_filesystem(),
                        library=library,
                        qbt=qbt,
                        parser=get_parser(),
                        profile=get_quality_profile(),
                        session=session,
                        movies_root=movies_root,
                        tv_root=tv_root,
                        anime_movie_root=anime_movie_root,
                        anime_tv_root=anime_tv_root,
                    )
            except QbittorrentError as exc:
                await session.rollback()
                _logger.warning(
                    "qBittorrent reconcile/import skipped this cycle (%s); "
                    "running availability pass anyway",
                    type(exc).__name__,
                )
                # A remove=no operator residual needs NO client I/O, so an OUTAGE
                # must not strand it for the outage's whole duration any more than
                # an unconfigured client may (the branch below) -- run the same
                # narrow DB-only heal on the rolled-back session; rows that need a
                # removal keep waiting for the client to recover (counted + logged
                # inside the heal, never silently dropped).
                await queue_service.heal_failed_pending_without_client(session)
        else:
            # DB-only strand heal (queue_service module docstring, "Operator
            # provenance"): with qBittorrent UNCONFIGURED the reconcile cycle above
            # never runs, so a remove=no operator residual (mark_failed with
            # remove_torrent=False -- which by the operator's own choice needs NO
            # client I/O) would otherwise sit at failed_pending forever on exactly
            # the installs that path exists for. Rows needing a removal still wait
            # for the client (logged inside the heal, never silently dropped).
            await queue_service.heal_failed_pending_without_client(session)

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


async def _autograb_once(app: FastAPI) -> None:
    """One auto-grab pass with a fresh session (ADR-0013), the beta automation spine.

    Best-effort and honest:

    * The ``auto_grab_enabled`` master switch is re-read EVERY tick (like the
      eviction settings), so a web toggle takes effect on the very next cycle with
      no restart -- north-star #1's "turn this bot off with a button". A disabled
      or not-yet-configured (Prowlarr/qBittorrent absent) worker is a CLEAN no-op
      (``mark_ok``), never an error: nothing is wrong, there is just nothing to do.
    * Otherwise it delegates to :func:`auto_grab_service.run_grab_cycle`, which
      reuses the SAME ``decision_service.preview`` + ``grab_service.grab`` brains
      as the manual Grab button.

    Stamps ``app.state.autograb_status`` (ADR-0013's health signal): ``mark_ok``
    only if the whole body completes cleanly. A search that RAISES (Prowlarr down /
    rate-limited) propagates out of ``run_grab_cycle`` and is recorded by
    ``_autograb_loop``'s ``except`` -- the operator sees a failing auto-grab loop
    (WHY nothing is being grabbed), not requests silently stuck at ``pending``. An
    operational GRAB failure (``GrabError``) does not propagate -- ``run_grab_cycle``
    surfaces it on the result; it is recorded here via ``mark_error`` (TYPE only)
    and the cycle is NOT marked clean, so a live untracked torrent is never hidden
    behind a falsely-parked scope.
    """
    status = _get_autograb_status(app)
    status.mark_run_started()
    sessionmaker = app.state.sessionmaker
    client = app.state.http_client
    async with sessionmaker() as session:
        if not await get_auto_grab_enabled(session):
            status.mark_ok()
            return
        # Prowlarr + qBittorrent are both required to search and grab; if either is
        # unconfigured the worker is a clean no-op until setup completes (honest,
        # not an error -- exactly the reconcile loop's posture for a missing qBt).
        try:
            prowlarr = await get_prowlarr(session, client)
            qbt = await get_qbittorrent(session, client)
        except ServiceNotConfiguredError:
            status.mark_ok()
            return
        result = await auto_grab_service.run_grab_cycle(
            session,
            prowlarr=prowlarr,
            parser=get_parser(),
            profile=get_quality_profile(),
            qbt=qbt,
            cooldowns=_get_autograb_cooldowns(app),
            save_path=get_downloads_host_root(),
        )
    # Surface how many scopes are CURRENTLY in a grab-pipeline cooldown (ADR-0013
    # round-3 #2), independent of the ok/error verdict below: a non-zero count is the
    # operator's honest signal that the grab pipeline (not the search) is failing --
    # eager scopes that keep hitting ``GrabError`` are being cooled so they don't
    # starve the search budget, rather than silently never reaching ``downloading``.
    status.cooled_down_scopes = result.cooled_down
    # An operational GRAB failure (``GrabError`` -- qBittorrent accepted the torrent
    # but no info-hash could be derived, leaving a live untracked torrent) does NOT
    # propagate the way a raised search does: ``run_grab_cycle`` catches it, leaves
    # the scope untouched, continues the rest of the cycle, and surfaces it on the
    # result. Record it on the health signal (TYPE only) exactly like a raised
    # search, and do NOT mark the cycle clean -- the operator sees a failing loop,
    # not a request silently parked while an orphan torrent consumes disk.
    if result.last_grab_error is not None:
        status.mark_error(result.last_grab_error)
        return
    status.mark_ok()


async def _autograb_loop(app: FastAPI) -> None:
    """Run :func:`_autograb_once` forever; one bad cycle never kills the loop.

    A raised indexer error (Prowlarr down / rate-limited) is recorded on the
    ``AutograbStatus`` health signal and the loop simply sleeps its base interval
    before retrying -- ``run_grab_cycle`` already ABORTS the rest of the cycle on
    the first raise (rather than hammering a down Prowlarr with every due scope),
    so a 60s base cadence is itself the global cycle backoff. Mirrors
    ``_reconcile_loop``'s "one bad cycle never kills the loop".
    """
    while True:
        try:
            await _autograb_once(app)
        except Exception as exc:
            _get_autograb_status(app).mark_error(exc)
            _logger.exception("auto-grab loop iteration failed; continuing")
        await asyncio.sleep(_AUTOGRAB_INTERVAL_SECONDS)


async def _log_drain_loop(app: FastAPI) -> None:
    """Sibling background task (own interval) draining the log-capture queue into
    ``log_events``, and periodically pruning past ``log_retention_days``.

    Reads ``app.state.log_handler`` (set by :func:`lifespan`) for the queue the
    synchronous :class:`~plex_manager.services.log_capture_service.
    LogCaptureHandler` feeds; a DB failure on either the drain or the prune is
    caught and logged, never left to kill the loop — a queue that cannot be
    drained this tick simply carries its backlog into the next one (bounded by
    the queue's own ``maxsize``, see the handler's docstring). ``drain_once`` is
    passed ``handler`` so a failed ``create_many`` insert's whole
    (already-dequeued) batch is added to ``handler.dropped_count`` — never
    re-queued, but always honestly counted.

    That covers a failed INSERT, but not a failed COMMIT of an otherwise
    successful insert: ``drain_once`` has already dequeued the batch from the
    in-memory queue by the time it returns, so if the commit right below THIS
    loop's call then raises (transient DB hiccup, full disk), the transaction
    rolls back and those records are just as lost — yet uncounted, since
    ``drain_once`` only ever saw ``create_many`` succeed. The drain commit is
    wrapped separately from the prune commit below so exactly that batch size
    is attributed to ``handler.dropped_count`` before the exception is
    re-raised into the ``except`` at the bottom of this loop — a prune-commit
    failure, by contrast, loses no log records (nothing new was inserted), so
    it is never counted here.
    """
    handler = app.state.log_handler
    sessionmaker = app.state.sessionmaker
    last_pruned_at = time.monotonic()
    while True:
        try:
            async with sessionmaker() as session:
                repo = SqlLogEventRepository(session)
                drained = await log_capture_service.drain_once(handler.queue, repo, handler=handler)
                try:
                    await session.commit()
                except Exception:
                    if drained:
                        handler.dropped_count += drained
                    raise
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
    """One disk-pressure eviction pass across every configured root — plus,
    for any root whose pressure gate does NOT fire this tick, a DELETE-NOTHING
    retention-telemetry sweep (:func:`~plex_manager.services.
    retention_telemetry_service.run_retention_telemetry_sweep`) logging what a
    sweep WOULD have evicted, on the SAME tick/interval (no new scheduler).

    Returns the FRESHLY-read ``eviction_interval_minutes`` (in seconds) for
    :func:`_eviction_loop` to sleep — so a web-edited interval takes effect on
    the very next tick, no restart required. Every other setting (enabled,
    thresholds, grace, proactive) is likewise re-read every tick for the same
    reason. ``eviction_enabled=False`` short-circuits BOTH the pressure-triggered
    and the proactive sweep — the master, in-app "turn this bot off" switch
    (north-star #1: a correction is always a settings toggle, never a terminal)
    — and, deliberately, the telemetry sweep too: it is part of the SAME
    eviction subsystem tick, so turning eviction off also stops its extra
    per-tick Plex watch-state polling.
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
        anime_movie_root = await get_anime_movie_root_optional(session)
        anime_tv_root = await get_anime_tv_root_optional(session)
        fs = get_eviction_filesystem(movies_root, tv_root, anime_movie_root, anime_tv_root)

        # ADR-0015: the anime roots get their own entries so both the pressure
        # sweep and the delete-nothing telemetry sweep below can actually reach
        # anime content -- eviction_service._owned_by_root assigns each
        # breadcrumb to its DEEPEST containing configured root, so an anime
        # library_path is never a candidate unless its root is listed here.
        roots: tuple[tuple[Literal["movie", "tv"], str | None], ...] = (
            ("movie", movies_root),
            ("tv", tv_root),
            ("movie", anime_movie_root),
            ("tv", anime_tv_root),
        )
        # Every configured root, threaded into each per-root sweep as the
        # ownership scope: with NESTED roots (an anime root inside movies_root),
        # a breadcrumb belongs ONLY to its most specific root's sweep -- the
        # parent's disk pressure must never evict (or telemetry-count) the child
        # mount's content (see eviction_service._owned_by_root).
        all_roots: list[str] = [r for _mt, r in roots if r]
        for media_type, root in roots:
            if not root:
                continue

            # Retention telemetry (delete-nothing, ADR-0012 follow-up): only when
            # THIS root's pressure gate would NOT fire this tick -- the real sweep
            # below is about to evict nothing anyway (below threshold_pct), so
            # this is the one case where the operator otherwise learns nothing
            # about what a cleanup policy would need. A root the sweep itself
            # cannot read (missing mount) is treated the same as "gate fired" --
            # never a false "safe to snapshot" signal -- and run_eviction_sweep
            # below independently logs/skips it exactly as before, unaffected by
            # this decision. Wrapped in its OWN try/except so a telemetry bug can
            # never prevent (or be silently masked by) the real eviction sweep
            # for this same root.
            try:
                pressure_would_fire = (
                    used_percent(await asyncio.to_thread(read_disk_usage, root)) >= threshold_pct
                )
            except OSError:
                pressure_would_fire = True
            # With proactive eviction ON, the "about to evict nothing" premise is
            # false (the proactive pass below acts on the same candidates this
            # tick) and the observer would double the Plex/FS walk right before
            # it -- the delete-nothing observer exists to DESIGN a retention
            # policy, so it stands down once one is actually enabled.
            if not pressure_would_fire and not proactive_enabled:
                try:
                    await retention_telemetry_service.run_retention_telemetry_sweep(
                        session=session,
                        library=library,
                        fs=fs,
                        media_type=media_type,
                        root_path=root,
                        all_roots=all_roots,
                        grace_days=grace_days,
                        threshold_pct=threshold_pct,
                        target_pct=target_pct,
                        # Live queue headroom, so a per-tick emission burst is
                        # paced under the durable log queue's ACTUAL free slots
                        # (not an empty-queue assumption) -- set on app.state in
                        # lifespan before this loop starts. See the sweep's
                        # ``free_slots`` param.
                        free_slots=app.state.log_handler.free_slots,
                    )
                except Exception:
                    _logger.exception(
                        "retention telemetry sweep failed for %s root %s; continuing",
                        media_type,
                        root,
                    )
                    # The telemetry sweep shares THIS tick's session with the real
                    # eviction sweep just below. A SQLAlchemy failure mid-telemetry
                    # leaves that session in a poisoned (aborted) transaction, so
                    # run_eviction_sweep's own reads/writes would then raise too --
                    # a telemetry bug silently blocking the REAL eviction, which is
                    # exactly the "telemetry can never block eviction" guarantee
                    # this subsystem promises. Roll the session back to a clean
                    # state before the eviction sweep runs. The rollback is itself
                    # guarded: if it also fails, log and continue -- run_eviction_sweep
                    # will surface any genuinely broken session honestly rather than
                    # being masked here.
                    try:
                        await session.rollback()
                    except Exception:
                        _logger.exception(
                            "rollback after a failed retention telemetry sweep for "
                            "%s root %s also failed; continuing",
                            media_type,
                            root,
                        )

            # Wrapped in its OWN try/except + guarded rollback, exactly like the
            # telemetry sweep above (and for the same reason): every root shares
            # THIS tick's single session, so a SQLAlchemy failure mid-sweep leaves
            # that session in a poisoned (aborted) transaction. Without the
            # rollback the NEXT root's sweep -- and the proactive pass just below
            # -- would then raise too, so ONE root's failure would silently skip
            # every remaining root this tick. Roll back to a clean state before
            # continuing; the rollback is itself guarded so even a broken session
            # is logged, never masked.
            try:
                evicted = await eviction_service.run_eviction_sweep(
                    session=session,
                    library=library,
                    fs=fs,
                    media_type=media_type,
                    root_path=root,
                    all_roots=all_roots,
                    threshold_pct=threshold_pct,
                    target_pct=target_pct,
                    grace_days=grace_days,
                )
            except Exception:
                _logger.exception(
                    "eviction sweep failed for %s root %s; continuing with remaining roots",
                    media_type,
                    root,
                )
                try:
                    await session.rollback()
                except Exception:
                    _logger.exception(
                        "rollback after a failed eviction sweep for %s root %s "
                        "also failed; continuing",
                        media_type,
                        root,
                    )
            else:
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
                # evicted are naturally absent here (no longer `available`). Same
                # guarded-rollback wrapper as the pressure sweep above -- a
                # proactive-pass failure on one root must not poison the shared
                # session for the next root either.
                try:
                    proactive_evicted = await eviction_service.run_eviction_sweep(
                        session=session,
                        library=library,
                        fs=fs,
                        media_type=media_type,
                        root_path=root,
                        all_roots=all_roots,
                        threshold_pct=threshold_pct,
                        target_pct=target_pct,
                        grace_days=grace_days,
                        proactive=True,
                    )
                except Exception:
                    _logger.exception(
                        "proactive eviction sweep failed for %s root %s; "
                        "continuing with remaining roots",
                        media_type,
                        root,
                    )
                    try:
                        await session.rollback()
                    except Exception:
                        _logger.exception(
                            "rollback after a failed proactive eviction sweep for "
                            "%s root %s also failed; continuing",
                            media_type,
                            root,
                        )
                else:
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
    # A well-formed grab whose HTTP source resolves to no addable torrent — the
    # client is healthy, so 422 (unprocessable), never a dishonest 502 outage.
    QbittorrentSourceError: (422, "torrent_source_unresolvable"),
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

    app.state.http_client = create_upstream_http_client()
    app.state.reconcile_status = ReconcileStatus()
    app.state.autograb_status = AutograbStatus()
    # In-process grab-pipeline cooldown registry (ADR-0013 round-3 #2), owned here so
    # it survives across auto-grab ticks; a restart clears it, like the health record.
    autograb_cooldowns: auto_grab_service.CooldownRegistry = {}
    app.state.autograb_cooldowns = autograb_cooldowns

    log_handler = log_capture_service.configure_logging(get_settings().log_level)
    app.state.log_handler = log_handler
    # The background reconciler closes the request -> grab -> import -> available
    # loop without a GET /queue poll having to do the heavy work. The auto-grab
    # worker (ADR-0013) is what turns a fresh request INTO a grab in the first
    # place; the log-drain and eviction tasks round out the set. All four are
    # SIBLINGS (own intervals, same lifecycle) -- never the same schedule.
    reconcile_task = asyncio.create_task(_reconcile_loop(app))
    autograb_task = asyncio.create_task(_autograb_loop(app))
    log_drain_task = asyncio.create_task(_log_drain_loop(app))
    eviction_task = asyncio.create_task(_eviction_loop(app))
    background_tasks = (reconcile_task, autograb_task, log_drain_task, eviction_task)
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


def _install_cookie_security_scheme(app: FastAPI) -> None:
    """Advertise the ``plexmgr.session`` cookie as a first-class auth scheme.

    Every protected route accepts EITHER the ``X-Api-Key`` header (the recovery/
    automation credential) OR the browser session cookie (the normal path — see
    ``deps.authenticate_request``), but FastAPI only emits the ``APIKeyHeader``
    scheme it can see as an explicit dependency. That leaves the exported contract
    dishonest: a client generated from it would believe cookie auth does not exist
    and that ``/auth/logout`` needs an api key. This wraps ``app.openapi`` to add the
    ``APIKeyCookie`` scheme and rewrite each api-key-secured operation's requirement
    to the honest OR. Safe methods accept either credential. Unsafe methods accept
    either the ``X-Api-Key`` header OR the browser session cookie together with the
    double-submit ``X-CSRF-Token`` header; a single security requirement object is
    OpenAPI's AND, while the list is OR.
    """
    default_openapi = app.openapi
    api_key_only: list[dict[str, list[str]]] = [{"APIKeyHeader": []}]
    api_key_or_cookie: list[dict[str, list[str]]] = [
        {"APIKeyHeader": []},
        {"APIKeyCookie": []},
    ]
    api_key_or_cookie_with_csrf: list[dict[str, list[str]]] = [
        {"APIKeyHeader": []},
        {"APIKeyCookie": [], "CSRFHeader": []},
    ]
    unsafe_methods = {"post", "put", "patch", "delete"}

    def openapi_with_cookie() -> dict[str, Any]:
        schema = default_openapi()
        components: dict[str, Any] = schema.get("components", {})
        schemes: dict[str, Any] | None = components.get("securitySchemes")
        if schemes is None:
            return schema
        schemes.setdefault(
            "APIKeyCookie",
            {
                "type": "apiKey",
                "in": "cookie",
                "name": SESSION_COOKIE_NAME,
            },
        )
        schemes.setdefault(
            "CSRFHeader",
            {
                "type": "apiKey",
                "in": "header",
                "name": CSRF_HEADER_NAME,
            },
        )
        paths: dict[str, Any] = schema.get("paths", {})
        for path_item in paths.values():
            operations: dict[str, Any] = path_item
            for method, value in operations.items():
                if not isinstance(value, dict):
                    continue
                operation = cast(dict[str, Any], value)
                if operation.get("security") == api_key_only:
                    operation["security"] = (
                        api_key_or_cookie_with_csrf
                        if method in unsafe_methods
                        else api_key_or_cookie
                    )
        return schema

    app.openapi = openapi_with_cookie


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    app = FastAPI(title="Plex Manager", version=__version__, lifespan=lifespan)
    app.add_middleware(SetupGuardMiddleware)
    app.add_exception_handler(ServiceNotConfiguredError, _service_not_configured_handler)
    for adapter_error in _ADAPTER_ERROR_RESPONSES:
        app.add_exception_handler(adapter_error, _adapter_error_handler)
    # Structured auth/setup failures (north star #3): AppError + the plex.tv
    # verification error render the code+message+hint+diagnostics envelope.
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(setup_router.router)
    app.include_router(auth_router.router)
    app.include_router(settings_router.router)
    app.include_router(discovery_router.router)
    app.include_router(requests_router.router)
    app.include_router(search_preview_router.router)
    app.include_router(queue_router.router)
    app.include_router(blocklist_router.router)
    app.include_router(quality_profile_router.router)
    app.include_router(ops_router.router)
    _install_cookie_security_scheme(app)
    # Mount the built SPA LAST so its catch-all fallback has the lowest match
    # priority (no-op when the frontend hasn't been built; see spa.mount_spa).
    mount_spa(app)
    return app


def create_upstream_http_client() -> httpx.AsyncClient:
    """Create the shared service-to-service client for configured integrations."""
    return httpx.AsyncClient(timeout=30.0, trust_env=False)


app = create_app()
