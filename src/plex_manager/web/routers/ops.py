"""Ops endpoints — health/status dashboard, log viewer/export, disk-pressure
eviction preview + manual trigger (ADR-0012). AUTHENTICATED, mirrors ``queue.py``.

Three groups, matching the blueprint's three components:

* ``GET /health`` — one read answering "is every subsystem healthy, is the
  reconcile loop running, how full is the disk" (:mod:`services.health_service`).
* ``GET /logs``, ``/logs/tail``, ``/logs/export`` — the durable, filterable
  ``log_events`` store, the live all-levels ring-buffer tail, and the
  LLM-diagnosis export bundle (:mod:`services.log_capture_service`).
* ``GET /disk``, ``POST /evict`` — per-root usage + a ranked eviction-candidate
  preview, and a manual pressure-sweep trigger (:mod:`services.eviction_service`).
  ``GET /disk``'s preview is TTL-cached per root (~15s, see
  ``_get_disk_preview_cache``) exactly like ``GET /health``'s subsystem probes —
  it is polled on the same cadence and would otherwise re-run an uncached Plex
  ``watch_state`` call plus an ``os.walk`` per available title on every poll.

Every endpoint here is read-only or an idempotent operator action; none of them
ever return a secret (subsystem ``detail`` strings and log messages carry
whatever a call site already chose to log — see ``log_capture_service``'s
module docstring on why that discipline lives upstream of this router, not in
it). Both durable-store READ boundaries — ``GET /logs`` and ``GET /logs/export``
— re-apply ``logsafe.redact_secrets`` to every persisted message as a second,
independent redaction pass (issue #153): capture-time redaction only covers rows
this build wrote through ``log_capture_service``, so a pre-upgrade row or a
direct repository write is masked consistently at BOTH read boundaries, not just
on export. (``GET /logs/tail`` reads the in-memory ring buffer, which is written
only by the already-redacting capture path, so it needs no second pass.)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final, Literal, cast

import httpx
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse, PlainTextResponse, Response

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.domain.disk_usage import used_percent
from plex_manager.logsafe import redact_secrets
from plex_manager.ports.library import LibraryPort
from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.services import eviction_service, watchlist_service
from plex_manager.services.eviction_service import EvictionOutcome
from plex_manager.services.health_service import (
    SUBSYSTEM_CACHE_KEYS,
    AutograbStatus,
    HealthCredentials,
    ReconcileStatus,
    SubsystemHealth,
    TtlCache,
    collect_health_snapshot,
    read_disk_usage,
)
from plex_manager.services.log_capture_service import RING_BUFFER_MAXLEN, LogCaptureHandler
from plex_manager.web.deps import (
    SettingsStore,
    get_anime_movie_root_optional,
    get_anime_tv_root_optional,
    get_autograb_status,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_eviction_filesystem,
    get_eviction_grace_days,
    get_health_cache,
    get_http_client,
    get_library,
    get_library_optional,
    get_log_handler,
    get_movies_root_optional,
    get_reconcile_status,
    get_session,
    get_tv_root_optional,
    get_watchlist_status,
    require_admin,
)
from plex_manager.web.events import publish_realtime
from plex_manager.web.schemas import (
    AutograbStatusItem,
    DiskGaugeItem,
    DiskResponse,
    DiskRootItem,
    EvictErrorItem,
    EvictionCandidateItem,
    EvictionOutcomeItem,
    EvictResponse,
    HealthResponse,
    LiveLogRecordItem,
    LogEventItem,
    LogsResponse,
    LogsTailResponse,
    ReconcileStatusItem,
    SubsystemHealthItem,
    WatchlistStatusItem,
)

__all__ = ["router"]

_logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/ops",
    tags=["ops"],
    dependencies=[Depends(require_admin)],
)

# Internal safety bounds — NOT web-editable settings: these guard the export
# endpoint's own memory/response-size footprint, they are not user-facing
# policy (mirrors ``log_capture_service.RING_BUFFER_MAXLEN``/``QUEUE_MAXSIZE``'s
# identical precedent of "an implementation cap is a constant; a behavioural
# threshold like ``log_retention_days`` is a setting").
_MAX_EXPORT_ROWS: Final = 5000
_DEFAULT_EXPORT_WINDOW_HOURS: Final = 24
_MAX_LOG_PAGE_SIZE: Final = 500
_DEFAULT_LOG_PAGE_SIZE: Final = 100


# --------------------------------------------------------------------------- #
# Component 1 — health / status dashboard
# --------------------------------------------------------------------------- #
@router.get("/health")
async def health_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    cache: Annotated[TtlCache[SubsystemHealth], Depends(get_health_cache)],
    reconcile_status: Annotated[ReconcileStatus, Depends(get_reconcile_status)],
    autograb_status: Annotated[AutograbStatus, Depends(get_autograb_status)],
    watchlist_status: Annotated[
        watchlist_service.WatchlistWorkerStatus, Depends(get_watchlist_status)
    ],
) -> HealthResponse:
    """One read: per-subsystem reachability, disk gauges, and the reconcile +
    auto-grab loops' own health. Each upstream probe is TTL-cached (~15s) so
    polling this every few seconds never hammers an upstream or burns the TMDB
    rate limit."""
    # Generation snapshot FIRST, strictly before the credential reads below
    # (Codex round 3): the moment a credential leaves the store it can be
    # superseded by a concurrent ``PUT /settings`` (whose commit bumps these
    # generations via ``TtlCache.invalidate``), and only a snapshot that
    # PRECEDES the read can prove no invalidation happened since -- see
    # ``TtlCache``'s invariant. Taken any later, a save landing between the
    # read and the snapshot would go unnoticed and the probe's stale result
    # would be cached for another full TTL.
    generations = cache.generation_snapshot(SUBSYSTEM_CACHE_KEYS)
    store = SettingsStore(session)
    creds = HealthCredentials(
        plex_url=await store.get("plex_url"),
        plex_token=await store.get("plex_token"),
        prowlarr_url=await store.get("prowlarr_url"),
        prowlarr_api_key=await store.get("prowlarr_api_key"),
        qbittorrent_url=await store.get("qbittorrent_url"),
        qbittorrent_username=await store.get("qbittorrent_username"),
        qbittorrent_password=await store.get("qbittorrent_password"),
        tmdb_api_key=await store.get("tmdb_api_key"),
    )
    movies_root = await get_movies_root_optional(session)
    tv_root = await get_tv_root_optional(session)
    anime_movie_root = await get_anime_movie_root_optional(session)
    anime_tv_root = await get_anime_tv_root_optional(session)
    snapshot = await collect_health_snapshot(
        session=session,
        client=client,
        cache=cache,
        creds=creds,
        reconcile_status=reconcile_status,
        autograb_status=autograb_status,
        generations=generations,
        library_roots={
            "movies_root": movies_root,
            "tv_root": tv_root,
            # ADR-0015 anime library routing — surfaced here too so an anime
            # disk (a separate mount from movies_root/tv_root) shows its own
            # usage gauge on the Status dashboard instead of a silent gap.
            "anime_movie_root": anime_movie_root,
            "anime_tv_root": anime_tv_root,
        },
    )
    return HealthResponse(
        subsystems=[
            SubsystemHealthItem(
                name=s.name,
                status=s.status,
                detail=s.detail,
                checked_at=s.checked_at,
                note=s.note,
            )
            for s in snapshot.subsystems
        ],
        disks=[
            DiskGaugeItem(
                root=d.root,
                path=d.path,
                total_bytes=d.total_bytes,
                available_bytes=d.available_bytes,
                used_percent=d.used_percent,
                error=d.error,
            )
            for d in snapshot.disks
        ],
        reconcile=ReconcileStatusItem(
            last_run_at=snapshot.reconcile.last_run_at,
            last_ok_at=snapshot.reconcile.last_ok_at,
            last_error_type=snapshot.reconcile.last_error_type,
            last_error_at=snapshot.reconcile.last_error_at,
            consecutive_failures=snapshot.reconcile.consecutive_failures,
        ),
        autograb=AutograbStatusItem(
            last_run_at=snapshot.autograb.last_run_at,
            last_ok_at=snapshot.autograb.last_ok_at,
            last_error_type=snapshot.autograb.last_error_type,
            last_error_at=snapshot.autograb.last_error_at,
            consecutive_failures=snapshot.autograb.consecutive_failures,
            cooled_down_scopes=snapshot.autograb.cooled_down_scopes,
        ),
        watchlist=WatchlistStatusItem(
            state=watchlist_status.state,
            last_run_at=watchlist_status.last_run_at,
            last_ok_at=watchlist_status.last_ok_at,
            last_error_type=watchlist_status.last_error_type,
            last_error_at=watchlist_status.last_error_at,
            fetched=watchlist_status.fetched,
            created=watchlist_status.created,
            existing=watchlist_status.existing,
            failed_users=watchlist_status.failed_users,
            failed_entries=watchlist_status.failed_entries,
        ),
    )


# --------------------------------------------------------------------------- #
# Component 2 — log / console viewer
# --------------------------------------------------------------------------- #
@router.get("/logs")
async def list_logs_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    level: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    logger: Annotated[str | None, Query()] = None,
    correlation_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=_MAX_LOG_PAGE_SIZE)] = _DEFAULT_LOG_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogsResponse:
    """Paginated, filtered read of the durable ``log_events`` store, newest
    first. ``correlation_id`` matches a ``request_id``/``download_id``/
    ``tmdb_id`` carried in a record's context."""
    page = await SqlLogEventRepository(session).list_events(
        level=level,
        since=since,
        logger=logger,
        correlation_id=correlation_id,
        limit=limit,
        offset=offset,
    )
    return LogsResponse(
        total=page.total,
        events=[
            LogEventItem(
                id=r.id,
                created_at=r.created_at,
                level=r.level,
                logger=r.logger,
                # Second redaction pass on the durable read (issue #153): capture-
                # time redaction only covers rows THIS build wrote; a pre-upgrade
                # row or a direct repository write is masked here just as it is on
                # /logs/export, so both durable-read boundaries are secret-safe.
                message=redact_secrets(r.message),
                context=r.context,
            )
            for r in page.results
        ],
    )


@router.get("/logs/tail")
async def tail_logs_endpoint(
    handler: Annotated[LogCaptureHandler, Depends(get_log_handler)],
    limit: Annotated[int, Query(ge=1, le=RING_BUFFER_MAXLEN)] = 200,
) -> LogsTailResponse:
    """The live, in-memory, ALL-levels ring-buffer tail (newest first) — lost on
    restart, never persisted (only INFO+ reaches durable ``log_events``, see
    ``GET /logs`` above). ``dropped_count`` is the capture handler's own honest
    signal for how many INFO+ records missed durable storage since startup."""
    records = handler.snapshot_tail(limit)
    records.reverse()
    return LogsTailResponse(
        events=[
            LiveLogRecordItem(
                created_at=r.created_at,
                level=r.level,
                logger=r.logger,
                message=r.message,
                context=r.context,
            )
            for r in records
        ],
        dropped_count=handler.dropped_count,
    )


def _export_filename(fmt: Literal["text", "json"]) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    extension = "json" if fmt == "json" else "txt"
    return f"plex-manager-logs-{stamp}.{extension}"


@router.get(
    "/logs/export",
    responses={
        200: {
            "description": (
                "Either a `text/plain` line-per-event trail (default, `format=text`) "
                "or a JSON `LogsResponse` bundle (`format=json`) — the two shapes this "
                "endpoint actually serves, per the `format` query parameter."
            ),
            "content": {
                "text/plain": {"schema": {"type": "string"}},
                "application/json": {"schema": {"$ref": "#/components/schemas/LogsResponse"}},
            },
        },
    },
)
async def export_logs_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    correlation_id: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    format: Annotated[Literal["text", "json"], Query()] = "text",
) -> Response:
    """The LLM-diagnosis affordance: one coherent, downloadable/copyable trail.

    Either a single correlation id's FULL history (``correlation_id`` — a
    ``request_id``/``download_id``/``tmdb_id``), or a time window from
    ``since`` through now (``since`` omitted defaults to the last 24h) — when
    both are supplied, ``correlation_id`` wins (a specific id's whole trail is
    the more precise ask). Bounded to :data:`_MAX_EXPORT_ROWS` (an internal
    safety cap, not a policy setting); when the matching window exceeds the
    cap, the OLDEST rows are kept (the root-cause lead-up survives) and the
    newest overflow is what's dropped, with an honest trailing note. Rendered
    OLDEST-first (a coherent top-to-bottom story), unlike the newest-first
    ``GET /logs`` list. ``Content-Disposition: attachment`` so navigating
    straight to this URL downloads a file; a caller reading the body via
    ``fetch`` (the frontend's "copy to clipboard") is unaffected by the header.

    Every message is passed through :func:`~plex_manager.logsafe.
    redact_secrets` again here (issue #153) as a SECOND, independent line of
    defense on top of the capture-time pass (``log_capture_service._capture``)
    -- this is the boundary the blueprint explicitly calls out ("the log store
    never records a secret"), and a row written before this redaction pass
    existed, or by any future path that bypasses the capture pipeline, must
    still never leave this endpoint carrying one.
    """
    repo = SqlLogEventRepository(session)
    if correlation_id is not None:
        page = await repo.list_events(
            correlation_id=correlation_id, limit=_MAX_EXPORT_ROWS, oldest_first=True
        )
    else:
        window_start = (
            since
            if since is not None
            else datetime.now(UTC) - timedelta(hours=_DEFAULT_EXPORT_WINDOW_HOURS)
        )
        page = await repo.list_events(since=window_start, limit=_MAX_EXPORT_ROWS, oldest_first=True)

    events = page.results  # repo already returns oldest-first
    truncated = page.total > len(page.results)
    headers = {"Content-Disposition": f'attachment; filename="{_export_filename(format)}"'}

    if format == "json":
        body = LogsResponse(
            total=page.total,
            events=[
                LogEventItem(
                    id=e.id,
                    created_at=e.created_at,
                    level=e.level,
                    logger=e.logger,
                    message=redact_secrets(e.message),
                    context=e.context,
                )
                for e in events
            ],
        )
        return JSONResponse(content=body.model_dump(mode="json"), headers=headers)

    lines = [
        f"{e.created_at.isoformat()} {e.level:<8} {e.logger}: {redact_secrets(e.message)}"
        for e in events
    ]
    if truncated:
        dropped = page.total - len(page.results)
        lines.append(
            f"... truncated: {dropped} newer row(s) not shown "
            f"(export capped at the {_MAX_EXPORT_ROWS} oldest matching rows) ..."
        )
    return PlainTextResponse("\n".join(lines) + "\n", headers=headers)


# --------------------------------------------------------------------------- #
# Component 3 — disk-pressure eviction: preview + manual trigger
# --------------------------------------------------------------------------- #
def _get_disk_preview_cache(request: Request) -> TtlCache[DiskRootItem]:
    """Return the process-wide, per-root disk/candidate-preview TTL cache.

    Mirrors ``web.deps.get_health_cache``'s lazy ``app.state`` init verbatim —
    same "create once, stash on ``app.state``, every subsequent request in this
    process reuses the SAME cache instance" shape, same default TTL
    (:data:`~plex_manager.services.health_service.SUBSYSTEM_PROBE_TTL_SECONDS`,
    ~15s). The Status page polls ``GET /disk`` on that same ~15s cadence;
    without this cache EVERY poll would re-run an uncached ``LibraryPort.
    watch_state()`` per available title plus an ``os.walk`` per title
    (:func:`eviction_service.preview_candidates`) — hammering Plex and the
    filesystem for a view that only needs to be fresh to within ~15s, exactly
    like the subsystem probes this mirrors.
    """
    cache = getattr(request.app.state, "disk_preview_cache", None)
    if not isinstance(cache, TtlCache):
        cache = TtlCache[DiskRootItem]()
        request.app.state.disk_preview_cache = cache
    # Same generic-narrowing cast ``get_health_cache`` uses -- this accessor is
    # the ONLY place anything ever assigns ``app.state.disk_preview_cache``.
    return cast("TtlCache[DiskRootItem]", cache)


async def _disk_root_item(
    *,
    session: AsyncSession,
    library: LibraryPort | None,
    label: str,
    media_type: Literal["movie", "tv"],
    root_path: str,
    all_roots: Sequence[str],
    grace_days: int,
    cache: TtlCache[DiskRootItem],
) -> DiskRootItem:
    """One configured root's usage gauge + its ranked eviction preview.

    TTL-cached (~15s, keyed on the role ``label`` + ``root_path`` — see the
    ``cache_key`` note below and :func:`_get_disk_preview_cache`) so a dashboard
    polling every ~15s never maps 1:1 onto a fresh Plex ``watch_state`` call per
    title plus an ``os.walk`` per title on every poll.

    An unreadable root reports ``error`` set (zeroed gauges, no candidates —
    there is nothing to preview against); this is cached too, so a persistently
    broken mount is not re-stat'd on every poll either. An unconfigured Plex
    (``library is None``) still shows the usage gauge, just with an empty
    candidate list: honest ("we can't check watch state"), never a fabricated
    preview.

    The usage read and the candidate preview are deliberately ISOLATED from each
    other: usage is read (and, on failure, reported) first, unconditionally: a
    Plex outage must never cost the operator the disk-usage gauges they need
    exactly when eviction can't run automatically. A configured-but-unreachable
    Plex (``get_library_optional`` only checks that a url/token are SET, not that
    they actually work) makes ``preview_candidates`` raise while resolving
    ``watch_state`` — caught here and downgraded to an empty candidate list
    (logged), the same honest degraded-preview posture as an unreadable root,
    rather than 500ing the WHOLE ``/ops/disk`` response over one root's preview.
    """
    # Key by the ROLE LABEL and path (#97): two roots can be configured to the
    # SAME directory -- not only movies_root vs tv_root (different media_type),
    # but also a normal vs anime root of the SAME media_type (e.g. movies_root and
    # anime_movie_root both pointing at one shared movie tree). The cached value
    # carries the role's ``label`` (``DiskRootItem.root``), so keying on
    # ``media_type:root_path`` alone would serve the FIRST role's cached item --
    # its label and all -- back for the second same-media_type role, collapsing
    # two configured roots into one duplicated row and hiding the other from the
    # Status response. The label is unique per role, so it keeps every configured
    # root's preview distinct even when two share a path (and still differs across
    # media_type, since each label maps to exactly one media_type).
    cache_key = f"{label}:{root_path}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        # shutil.disk_usage (a statvfs syscall) can stall on a hung NFS/SMB
        # mount -- offload it so that never freezes the event loop.
        usage = await asyncio.to_thread(read_disk_usage, root_path)
    except OSError as exc:
        result = DiskRootItem(
            root=label,
            path=root_path,
            total_bytes=0,
            available_bytes=0,
            used_percent=0.0,
            error=str(exc),
            candidates=[],
        )
        cache.set(cache_key, result)
        return result

    candidates: list[EvictionCandidateItem] = []
    if library is not None:
        try:
            # ``all_roots`` scopes the preview to breadcrumbs this root OWNS
            # (deepest containing root) so a nested anime root's content is
            # previewed under ITS row, never double-listed under the parent's
            # -- the same assignment the real sweep uses.
            ranked = await eviction_service.preview_candidates(
                session=session,
                library=library,
                media_type=media_type,
                root_path=root_path,
                grace_days=grace_days,
                all_roots=all_roots,
            )
        except (PlexLibraryError, PlexAuthError) as exc:
            # Plex IS configured but unreachable/rejecting the token: the disk
            # gauge above is already resolved and must still be returned --
            # only the candidate preview degrades, honestly, to empty rather
            # than taking down the whole endpoint (and every OTHER root) with
            # it. ``error`` is deliberately left unset: it drives the frontend's
            # "hide the usage gauge" branch (Status.tsx), which must stay
            # visible here -- this is a preview-only degradation, not an
            # unreadable root.
            _logger.warning(
                "eviction candidate preview skipped for %s root %s (%s)",
                media_type,
                root_path,
                type(exc).__name__,
            )
            ranked = []
        candidates = [
            EvictionCandidateItem(
                request_id=c.request_id,
                media_type=c.media_type,
                title=c.title,
                season=c.season,
                status=c.status,
                last_viewed_at=c.last_viewed_at,
                size_percent=c.size_percent,
                library_path=c.library_path,
            )
            for c in ranked
        ]

    result = DiskRootItem(
        root=label,
        path=root_path,
        total_bytes=usage.total_bytes,
        available_bytes=usage.available_bytes,
        used_percent=used_percent(usage),
        error=None,
        candidates=candidates,
    )
    cache.set(cache_key, result)
    return result


@router.get("/disk")
async def disk_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    library: Annotated[LibraryPort | None, Depends(get_library_optional)],
    cache: Annotated[TtlCache[DiskRootItem], Depends(_get_disk_preview_cache)],
) -> DiskResponse:
    """Disk usage per configured library root, plus a ranked preview of what a
    pressure sweep WOULD evict from each (never evicts anything itself).

    TTL-cached per root (~15s) — see :func:`_disk_root_item` — so the Status
    page's ~15s poll never re-hammers Plex/the filesystem on every tick.
    """
    movies_root = await get_movies_root_optional(session)
    tv_root = await get_tv_root_optional(session)
    anime_movie_root = await get_anime_movie_root_optional(session)
    anime_tv_root = await get_anime_tv_root_optional(session)
    grace_days = await get_eviction_grace_days(session)

    # Ownership scope for every root's preview (see _disk_root_item): with
    # nested configured roots, each breadcrumb is listed only under its most
    # specific root's row.
    all_roots: list[str] = [r for r in (movies_root, tv_root, anime_movie_root, anime_tv_root) if r]

    roots: list[DiskRootItem] = []
    if movies_root:
        roots.append(
            await _disk_root_item(
                session=session,
                library=library,
                label="movies_root",
                media_type="movie",
                root_path=movies_root,
                all_roots=all_roots,
                grace_days=grace_days,
                cache=cache,
            )
        )
    if tv_root:
        roots.append(
            await _disk_root_item(
                session=session,
                library=library,
                label="tv_root",
                media_type="tv",
                root_path=tv_root,
                all_roots=all_roots,
                grace_days=grace_days,
                cache=cache,
            )
        )
    # ADR-0015 anime library routing — its own DiskRootItem rows, so a separate
    # anime disk is never a silent gap on the Status page. Cached (like every
    # other root) by ``f"{label}:{root_path}"`` (#97): the role label keeps an
    # anime root pointed at the SAME path as movies_root/tv_root as its OWN
    # distinct row, rather than serving back the earlier role's cached entry and
    # hiding one configured root.
    if anime_movie_root:
        roots.append(
            await _disk_root_item(
                session=session,
                library=library,
                label="anime_movie_root",
                media_type="movie",
                root_path=anime_movie_root,
                all_roots=all_roots,
                grace_days=grace_days,
                cache=cache,
            )
        )
    if anime_tv_root:
        roots.append(
            await _disk_root_item(
                session=session,
                library=library,
                label="anime_tv_root",
                media_type="tv",
                root_path=anime_tv_root,
                all_roots=all_roots,
                grace_days=grace_days,
                cache=cache,
            )
        )
    return DiskResponse(roots=roots)


@router.post("/evict")
async def evict_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    library: Annotated[LibraryPort, Depends(get_library)],
    cache: Annotated[TtlCache[DiskRootItem], Depends(_get_disk_preview_cache)],
) -> EvictResponse:
    """Manually trigger a pressure-triggered eviction sweep across every
    configured root — the north-star #1 button: free space on demand.

    Runs REGARDLESS of the ``eviction_enabled`` master switch: that setting
    gates the AUTOMATIC periodic sweep (``web/app.py``'s ``_eviction_tick``),
    never this explicit, operator-initiated action — an operator who disabled
    the background loop must still be able to free space on demand without
    re-enabling it. Still pressure-gated (a root already below
    ``disk_pressure_threshold_percent`` evicts nothing — an empty ``evicted``
    is a normal, honest outcome) and still honours every other rule (watched,
    past grace, un-pinned, not in flight): this is the SAME sweep the periodic
    task runs, just invoked synchronously instead of on a timer. Requires Plex
    (409 ``service_not_configured`` otherwise — watch state can't be resolved
    without it); an unset root is simply skipped, not an error.

    Invalidates :func:`_get_disk_preview_cache` after the sweep: without this,
    ``GET /disk`` would keep serving the pre-eviction snapshot (stale
    candidates the operator just deleted, stale free-space gauge) for up to
    its ~15s TTL, contradicting north-star #3 for the very endpoint that IS
    the correction button.

    Each root's sweep is INDEPENDENT: one root raising (e.g. a transient
    ``PlexLibraryError`` resolving TV watch state during candidate assembly)
    must never abort a root that has not run yet, nor hide what an EARLIER
    root already deleted and committed. A bare 500 here would do exactly that
    — the operator would see "Free space failed" with no indication that,
    say, the movies root already freed real space — which is a dishonest,
    silent-partial-success state north star #2 forbids. So every root's sweep
    is individually caught; a caught failure is logged and recorded in
    ``errors`` (never swallowed), ``evicted`` still lists whatever succeeded,
    and the endpoint ALWAYS reaches ``cache.clear()`` and returns 200 —
    partial completion is a first-class, visible outcome, not a terminal one.
    """
    movies_root = await get_movies_root_optional(session)
    tv_root = await get_tv_root_optional(session)
    anime_movie_root = await get_anime_movie_root_optional(session)
    anime_tv_root = await get_anime_tv_root_optional(session)
    threshold_pct = await get_disk_pressure_threshold_percent(session)
    target_pct = await get_disk_pressure_target_percent(session)
    grace_days = await get_eviction_grace_days(session)
    fs = get_eviction_filesystem(movies_root, tv_root, anime_movie_root, anime_tv_root)

    # ADR-0015: the anime roots get their own rows so the pressure sweep can
    # actually reach anime content -- ``eviction_service._owned_by_root`` assigns
    # each breadcrumb to its DEEPEST containing configured root, so an anime
    # library_path is never a candidate unless its root is listed here, even
    # though ``fs`` above already allows deleting it.
    roots: tuple[
        tuple[
            Literal["movie", "tv"],
            Literal["movies_root", "tv_root", "anime_movie_root", "anime_tv_root"],
            str | None,
        ],
        ...,
    ] = (
        ("movie", "movies_root", movies_root),
        ("tv", "tv_root", tv_root),
        ("movie", "anime_movie_root", anime_movie_root),
        ("tv", "anime_tv_root", anime_tv_root),
    )
    # The nested-root ownership scope for every per-root sweep: with an anime
    # root nested inside movies_root/tv_root, a breadcrumb belongs ONLY to its
    # most specific root's sweep -- the parent's pressure must never evict the
    # child mount's content (see eviction_service._owned_by_root).
    all_roots: list[str] = [r for _mt, _label, r in roots if r]
    outcomes: list[EvictionOutcome] = []
    errors: list[EvictErrorItem] = []
    for media_type, root_label, root in roots:
        if not root:
            continue
        try:
            outcomes.extend(
                await eviction_service.run_eviction_sweep(
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
            )
        except Exception as exc:
            # Never re-raise: a later root's failure must not hide an earlier
            # root's already-committed evictions (see the docstring above).
            # The detail names only the exception TYPE, never ``str(exc)`` --
            # the same secret-safety discipline ``_adapter_error_handler``
            # uses, since this except is broad enough to catch more than the
            # typed adapter errors that discipline was written for.
            _logger.exception(
                "eviction sweep failed for %s root %s; continuing with remaining roots",
                media_type,
                root,
            )
            # Every root shares THIS request's single session, so a SQLAlchemy
            # failure mid-sweep leaves it in a poisoned (aborted) transaction --
            # without a rollback the NEXT root's sweep would then raise too,
            # cascading one root's failure into every remaining root (#95). Roll
            # back to a clean state before continuing; the rollback is itself
            # guarded so even a broken session is logged, never masked.
            try:
                await session.rollback()
            except Exception:
                _logger.exception(
                    "rollback after a failed eviction sweep for %s root %s also failed; continuing",
                    media_type,
                    root,
                )
            errors.append(
                EvictErrorItem(root=root_label, detail=f"sweep failed ({type(exc).__name__})")
            )

    # The sweep just deleted files and/or changed watch-derived eligibility
    # for whatever it touched — the cached preview (candidates + free-space
    # gauge) is now stale for every root, not just the ones with outcomes
    # (freed space shifts the usage gauge too). Clear it so the very next
    # GET /disk reflects this sweep instead of serving up to ~15s of
    # pre-eviction state back to the operator who just clicked the button.
    # Always reached -- even a per-root failure above never skips this, so a
    # partial sweep's freed space is still visible on the very next poll.
    cache.clear()
    if outcomes or errors:
        publish_realtime(
            request.app,
            ("requests", "discover", "ops:disk", "ops:health"),
            reason="eviction",
        )

    return EvictResponse(
        evicted=[
            EvictionOutcomeItem(
                request_id=o.request_id,
                media_type=o.media_type,
                title=o.title,
                season=o.season,
                library_path=o.library_path,
                freed_bytes=o.freed_bytes,
            )
            for o in outcomes
        ],
        errors=errors,
    )
