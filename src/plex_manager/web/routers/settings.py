"""Settings endpoints — authenticated by Plex session or ``X-Api-Key``.

``GET`` returns a redacted view (secrets masked to ``"***"``). ``PUT`` upserts
the provided config, encrypting secret values at rest. Only fields present in the
request body are written; absent fields are left unchanged.

The ``/app-key`` endpoints manage the app's own ``X-Api-Key``
(``SystemSettings.app_api_key``) -- an OPT-IN recovery/automation credential.
Setup mints nothing, so the key starts absent: ``GET /app-key/status`` reports
whether one exists (without revealing it), ``POST /app-key/rotate`` GENERATES the
first key (when none exists) and ROTATES thereafter, ``GET /app-key`` reveals the
current key for a new device, and ``DELETE /app-key`` revokes it (issue #28's
OAuth-deferral analysis).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Final

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_409_CONFLICT

from plex_manager.adapters.plex.oauth import PlexTvClient
from plex_manager.adapters.service_url import same_service_base
from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.models import AuthSession, SystemSettings, User
from plex_manager.ports.library import LibraryPort
from plex_manager.repositories.log_events import SqlLogEventRepository
from plex_manager.services import path_visibility
from plex_manager.services.health_service import SubsystemHealth, TtlCache
from plex_manager.services.log_capture_service import (
    LogCaptureHandler,
    redact_log_context,
    redact_log_message,
)
from plex_manager.services.update_policy import (
    UPDATE_POLICY_SETTING_KEYS,
    resolve_update_policy,
)
from plex_manager.web.deps import (
    API_KEY_HEADER_NAME,
    AUTO_GRAB_ENABLED_DEFAULT,
    AUTO_GRAB_INTERVAL_SECONDS_DEFAULT,
    AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT,
    AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT,
    AUTOMATIC_UPDATE_WEEKDAYS_DEFAULT,
    AUTOMATIC_UPDATE_WINDOW_END_DEFAULT,
    AUTOMATIC_UPDATE_WINDOW_START_DEFAULT,
    AUTOMATIC_UPDATES_ENABLED_DEFAULT,
    DISK_PRESSURE_TARGET_PERCENT_DEFAULT,
    DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT,
    EVICTION_ENABLED_DEFAULT,
    EVICTION_GRACE_DAYS_DEFAULT,
    EVICTION_INTERVAL_MINUTES_DEFAULT,
    EVICTION_PROACTIVE_ENABLED_DEFAULT,
    LOG_MAX_ROWS_DEFAULT,
    LOG_RETENTION_DAYS_DEFAULT,
    PLEX_MACHINE_ID_SETTING,
    SECRET_MASK,
    SECRET_SETTING_KEYS,
    SESSION_COOKIE_NAME,
    WATCHLIST_SYNC_ENABLED_DEFAULT,
    WATCHLIST_SYNC_INTERVAL_MINUTES_DEFAULT,
    AuthContext,
    AuthMethod,
    SettingsStore,
    api_key_matches,
    app_key_rotate_lock,
    ensure_system_settings,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_health_cache,
    get_http_client,
    get_library,
    hash_session_token,
    load_system_settings,
    require_admin,
    resolve_auto_grab_interval_seconds,
    resolve_auto_grab_max_searches_per_cycle,
    resolve_bool_setting,
    resolve_disk_pressure_percents,
    resolve_eviction_grace_days,
    resolve_eviction_interval_minutes,
    resolve_log_max_rows,
    resolve_log_retention_days,
    resolve_watchlist_sync_interval_minutes,
    secret_rotation_lock,
)
from plex_manager.web.errors import AppError
from plex_manager.web.events import close_realtime_streams, publish_realtime
from plex_manager.web.schemas import (
    AppApiKeyResponse,
    AppApiKeyStatusResponse,
    ErrorEnvelope,
    PlexLibraryOption,
    SettingsResponse,
    SettingsUpdate,
)
from plex_manager.web.setup_validation import (
    assert_admin_owns_server,
    assert_plex_token_authorized,
    library_options,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/settings",
    tags=["settings"],
    dependencies=[Depends(require_admin)],
)

# 32 bytes -> secrets.token_urlsafe(32) yields a 43-char URL-safe token. Setup
# mints no key any more; BOTH the first generate (from no key) and every later
# rotate mint with this length, so a key is indistinguishable in shape/strength
# regardless of when it was issued.
_API_KEY_BYTES = 32

# Mirrors ``web.routers.auth._CLIENT_ID_SETTING`` / ``web.routers.setup`` — the
# plex.tv device identifier the sign-in flow persists. Read (never re-created)
# here so the repoint verification probe uses the SAME device identity as every
# other plex.tv/Plex-server call; the fallback only matters on a DB that never
# saw a sign-in.
_CLIENT_ID_SETTING = "plex_oauth_client_identifier"
_FALLBACK_CLIENT_IDENTIFIER = "plex-manager"

_PUT_SETTINGS_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {
        "model": ErrorEnvelope,
        "description": "The signed-in admin does not own the replacement Plex server",
    },
    409: {
        "model": ErrorEnvelope,
        "description": "The signed-in admin has no Plex account on file to verify ownership",
    },
    # This status code has THREE distinct response shapes, so all three are
    # documented (mirroring queue.py's ``_GRAB_ERROR_RESPONSES`` /
    # search_preview.py's anyOf, which document the same body-validation-vs-app-error
    # collision): FastAPI's own request-body validation (``HTTPValidationError``),
    # ``_validate_disk_pressure_pair``'s plain ``HTTPException`` (``ErrorDetail``),
    # and application-validation ``AppError`` values (``plex_token_invalid`` or
    # ``credential_reentry_required``, both ``ErrorEnvelope``). Declaring only
    # ``ErrorEnvelope`` here would silently overwrite FastAPI's auto-generated
    # validation-error entry instead of adding to it.
    422: {
        "description": (
            "Request validation failed, the disk-pressure pair would invert, "
            "a changed service destination requires credential re-entry, or the "
            "replacement Plex server rejected the effective Plex token"
        ),
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                        {"$ref": "#/components/schemas/ErrorDetail"},
                        {"$ref": "#/components/schemas/ErrorEnvelope"},
                    ]
                }
            }
        },
    },
    502: {
        "model": ErrorEnvelope,
        "description": "The replacement Plex server did not answer the /identity probe",
    },
}

# Serialises app-key rotation so the compare-and-swap in ``rotate_app_key_endpoint``
# is a genuine atomic read-modify-write, not check-then-act. Two rotations racing
# with the SAME old key would otherwise both re-read the old value and both pass
# the compare BEFORE either commits, so the second write silently clobbers the
# first's freshly minted key (leaving the first client showing a dead key). Under
# this lock the loser's re-read happens only AFTER the winner has committed, so it
# observes the new key and honestly 409s.
#
# Correctness relies on this being a SINGLE-PROCESS deployment: uvicorn runs one
# worker and the in-app reconcile / eviction / log-drain lifespan loops (web/app.py)
# already assume the same single process, so an in-process ``asyncio.Lock`` — not a
# DB row lock or advisory lock — is the right, matching tool. A multi-worker
# deployment would need a DB-level guard instead (a conditional UPDATE is not an
# option here: the key column is EncryptedStr/Fernet, whose ciphertext is
# non-deterministic, so a ``WHERE app_api_key = <ciphertext>`` predicate can never
# match).
#
# The lock itself lives in ``deps`` (``app_key_rotate_lock``) so the recovery-key
# EXCHANGE endpoint in the ``auth`` router can serialize against the SAME instance
# (issue #293): an exchange must not mint a fresh recovery session from a key that a
# concurrent rotate/revoke is retiring. This module-local name is a readability alias.
_rotate_lock = app_key_rotate_lock

# A settings update validates an EFFECTIVE destination/credential pair and then
# writes its individual rows in one transaction.  Without serializing that whole
# read-check-write sequence, two otherwise-valid partial PUTs can interleave: a
# secret-only rotation can finish its checks against the old URL, a second request
# can commit a new URL plus a dummy secret, and the first can then overwrite only
# the secret -- silently pairing the fresh credential with the new destination.
# Keep validation, live Plex probes, persistence, cache invalidation, and the
# redacted response under one lock so every request reasons about the pair left by
# its predecessor.
#
# Like ``_rotate_lock`` above, this is deliberately a SINGLE-PROCESS guard. The
# supported uvicorn deployment runs one worker and the in-app background loops
# already rely on that model. A future multi-worker deployment must replace this
# with a database-level version/CAS or advisory lock spanning the same critical
# section; an in-process lock alone would not coordinate separate workers.
_settings_update_lock = asyncio.Lock()


def _log_handler(request: Request) -> LogCaptureHandler:
    handler = getattr(request.app.state, "log_handler", None)
    if not isinstance(handler, LogCaptureHandler):
        handler = LogCaptureHandler(loop=asyncio.get_running_loop())
        request.app.state.log_handler = handler
    return handler


async def _rewrite_before_secret_replacement(
    session: AsyncSession, old_values: frozenset[str]
) -> int:
    if not old_values:
        return 0
    return await SqlLogEventRepository(session).rewrite_redactable_fields(
        lambda message: redact_log_message(message, old_values),
        lambda context: redact_log_context(context, old_values),
    )


@asynccontextmanager
async def _secret_rotation(
    session: AsyncSession,
    request: Request,
    transition_values: frozenset[str],
    old_values: frozenset[str],
) -> AsyncGenerator[None]:
    handler = _log_handler(request)
    async with secret_rotation_lock:
        previous_values = handler.begin_secret_rotation(transition_values)
        try:
            await _rewrite_before_secret_replacement(session, old_values)
            yield
            current_values = await SettingsStore(session).secret_values()
            await session.commit()
        except asyncio.CancelledError:
            # Restore the in-memory snapshot first (synchronous, always runs),
            # then SHIELD the rollback so its DB op completes instead of being
            # cancelled mid-flight -- a half-cancelled aiosqlite rollback closes
            # the connection and poisons the shared boundary for the drain loop.
            handler.abort_secret_rotation(previous_values)
            await asyncio.shield(session.rollback())
            raise
        except Exception:
            await session.rollback()
            handler.abort_secret_rotation(previous_values)
            raise
        handler.complete_secret_rotation(previous_values | old_values, current_values)


def _observed_app_key(request: Request, auth: AuthContext, system: SystemSettings) -> str | None:
    """The key value THIS request proved it held at auth time — the CAS baseline.

    A HEADER-authenticated ``X-Api-Key`` caller proved the exact header value, so
    that is its baseline. Every OTHER admin observed only the stored value their
    request session loaded at auth time: a Plex-session admin (no key at all), AND
    — the issue #293 finding 3 fix — a cookie-based recovery/break-glass admin.

    A recovery session reports ``AuthMethod.api_key`` (it carries the recovery
    key's admin authority) yet authenticated by the httpOnly COOKIE, not a header.
    The pre-fix CAS sourced the baseline from the (absent) header for that admin,
    compared the stored key against ``None``, and ALWAYS 409'd — a break-glass
    admin could never rotate or revoke the very key they signed in with from the
    browser (a north-star-#1, never-require-a-terminal violation).

    ``auth.via_api_key_header`` — set at the single place each auth path constructs
    its context — is the reliable discriminator. Sniffing the header's mere PRESENCE
    here is NOT (issue #293 round 2): a client or proxy can send a stale/empty
    ``X-Api-Key`` alongside a valid recovery cookie; ``authenticate_request`` rejects
    the header and falls back to the cookie (still reporting ``api_key``), so
    presence-sniffing would adopt the REJECTED value as the baseline and 409 a
    legitimately cookie-authenticated admin.
    """
    if auth.method is AuthMethod.api_key and auth.via_api_key_header:
        return request.headers.get(API_KEY_HEADER_NAME)
    return system.app_api_key


def _acting_recovery_session_hash(request: Request, auth: AuthContext) -> str | None:
    """Token hash of the acting recovery-cookie session, or ``None`` if not one.

    Returns a hash ONLY when the caller authenticated as a break-glass recovery
    session: ``api_key`` admin authority that was NOT proven by the ``X-Api-Key``
    header (``auth.via_api_key_header`` is ``False``) — i.e. the httpOnly session
    cookie authenticated. A header-authenticated caller and a Plex-session admin both
    yield ``None``. Used to EXEMPT that one session from the rotate bulk-revoke (see
    below). Keyed off the AUTHENTICATED credential source, not the header's presence
    (issue #293 round 2): a stale ``X-Api-Key`` riding alongside the valid recovery
    cookie must not cost the actor their exemption.
    """
    if auth.method is not AuthMethod.api_key or auth.via_api_key_header:
        return None
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return hash_session_token(token) if token else None


async def _revoke_recovery_sessions(
    session: AsyncSession, *, exempt_token_hash: str | None = None
) -> None:
    """Revoke live recovery-cookie sessions (``AuthSession.user_id IS NULL``).

    When the underlying app key is rotated or revoked, a still-open recovery
    session minted by exchanging that key (``POST /auth/api-key``) must lose its
    authority too (issue #293 finding 4) — otherwise a break-glass cookie keeps
    admin access after the key it was born from is gone, contradicting the
    revoke/rotate semantics a direct ``X-Api-Key`` caller already gets (its next
    request 401s immediately). Staged in the caller's transaction; the caller's
    own ``commit`` persists it alongside the key change.

    ``exempt_token_hash`` spares exactly ONE session from the bulk revoke — the
    session of the admin PERFORMING a rotation, when that admin is themselves signed
    in via a recovery cookie (issue #293 P1). Rotation hands the actor the new
    plaintext key exactly once in the HTTP response; revoking their OWN cookie in the
    same commit would 401 their post-rotate refetch and realtime reconnect before the
    SPA renders the key, potentially unmounting Settings and hiding it. An operator
    with no Plex sign-in would then hold NEITHER the old nor the new key. The actor
    legitimately performed the rotation and the response IS their copy of the new key,
    so their session survives; every OTHER recovery session is still revoked (its
    authority ends with the key it was minted from). REVOKE passes no exemption — with
    the key destroyed there is no new key to hand back, so the actor's break-glass
    session is retired along with all the others.
    """
    stmt = update(AuthSession).where(
        AuthSession.user_id.is_(None), AuthSession.revoked_at.is_(None)
    )
    if exempt_token_hash is not None:
        stmt = stmt.where(AuthSession.token_hash != exempt_token_hash)
    await session.execute(stmt.values(revoked_at=datetime.now(UTC)))


# The default each boolean key degrades to on an unrecognized stored value --
# threaded into ``resolve_bool_setting`` so its WARNING names the actual
# fallback the matching runtime getter uses. The parity-guard test derives its
# bool key set from this mapping so the sanitizer's loop and the guard can
# never name different key sets.
_BOOL_SETTING_DEFAULTS: dict[str, bool] = {
    "eviction_enabled": EVICTION_ENABLED_DEFAULT,
    "eviction_proactive_enabled": EVICTION_PROACTIVE_ENABLED_DEFAULT,
    "auto_grab_enabled": AUTO_GRAB_ENABLED_DEFAULT,
    "automatic_updates_enabled": AUTOMATIC_UPDATES_ENABLED_DEFAULT,
    "automatic_update_idle_only": AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT,
    "watchlist_sync_enabled": WATCHLIST_SYNC_ENABLED_DEFAULT,
}

_UPDATE_POLICY_FIELDS: Final[frozenset[str]] = frozenset(UPDATE_POLICY_SETTING_KEYS)
_UPDATE_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def _parse_update_time(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if _UPDATE_TIME_RE.fullmatch(normalized) is not None else None


def _present_effective(
    raw: str | None, effective: float | int, default: float | int, honored: bool
) -> str | None:
    """The value ``GET /settings`` presents for one resolved typed setting.

    The display contract (PR #142 round 3): the page must show exactly what the
    runtime getter is using --

    * raw value HONORED -> echo the raw stored string verbatim (display
      fidelity: ``"88.5"`` stays ``88.5``).
    * degraded to the DEFAULT -> ``None`` (unset), which the page renders as
      the default -- the same value the getter returns, so null is truthful.
    * degraded to anything ELSE (an upper-bound CLAMP, or the disk-pressure
      pair rule) -> the EFFECTIVE value itself. Nulling here would make the
      page claim the default (e.g. a 30-day grace) while the runtime runs the
      clamped MAX (3650 days) -- the exact page-vs-runtime lie this exists to
      prevent. The clamped value is always within ``SettingsUpdate``'s bounds,
      so re-saving the displayed form persists it verbatim (self-healing).
    """
    if raw is not None and honored:
        return raw
    if effective == default:
        return None
    return str(effective)


def _sanitize_typed_settings(raw: dict[str, str | None]) -> dict[str, object | None]:
    """Present every stored typed value as the EFFECTIVE value the runtime uses.

    Each typed key is resolved through the SAME ``web.deps`` resolver its
    runtime getter uses (``resolve_eviction_grace_days``,
    ``resolve_disk_pressure_percents``, ...), then displayed per
    :func:`_present_effective` -- raw when honored, the effective value when
    clamped/pair-adjusted, ``None`` when the default applies. One resolution
    path means the page and the eviction/auto-grab/log-retention loops can
    never disagree about a corrupt stored value (issue #92, north star #3).

    This also keeps ``GET`` from ever 500ing or echoing a value ``PUT``
    rejects: unparsable and non-finite raws (which would crash Starlette's
    ``json.dumps(..., allow_nan=False)`` at serialization time) resolve to the
    default (``None`` here), and every clamped value lies within
    ``SettingsUpdate``'s own bounds, so the displayed form always re-saves
    cleanly.

    The disk-pressure pair is resolved TOGETHER (never per-side): a half-corrupt
    pair degrades to a workable ``target <= threshold`` pair (see
    ``resolve_disk_pressure_percents``), and whatever the pair rule substitutes
    is displayed -- including for a side that is UNSET in storage (e.g. an unset
    target whose effective value is pulled down to a low stored threshold), so
    the page never implies a default the sweep is not actually using.

    Boolean raws are normalized to their stripped form when honored: the
    resolver accepts a whitespace-padded ``" false "`` (the pre-#142 parser's
    contract), but ``SettingsResponse``'s own bool coercion would reject the
    padded literal at ``model_validate`` -- the stripped token is the same
    recognized value, minus the crash.
    """
    out: dict[str, object | None] = dict(raw)

    pair = resolve_disk_pressure_percents(
        raw.get("disk_pressure_threshold_percent"), raw.get("disk_pressure_target_percent")
    )
    out["disk_pressure_threshold_percent"] = _present_effective(
        raw.get("disk_pressure_threshold_percent"),
        pair.threshold,
        DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT,
        pair.threshold_honored,
    )
    out["disk_pressure_target_percent"] = _present_effective(
        raw.get("disk_pressure_target_percent"),
        pair.target,
        DISK_PRESSURE_TARGET_PERCENT_DEFAULT,
        pair.target_honored,
    )

    interval, interval_honored = resolve_eviction_interval_minutes(
        raw.get("eviction_interval_minutes")
    )
    out["eviction_interval_minutes"] = _present_effective(
        raw.get("eviction_interval_minutes"),
        interval,
        EVICTION_INTERVAL_MINUTES_DEFAULT,
        interval_honored,
    )

    watchlist_interval, watchlist_interval_honored = resolve_watchlist_sync_interval_minutes(
        raw.get("watchlist_sync_interval_minutes")
    )
    out["watchlist_sync_interval_minutes"] = _present_effective(
        raw.get("watchlist_sync_interval_minutes"),
        watchlist_interval,
        WATCHLIST_SYNC_INTERVAL_MINUTES_DEFAULT,
        watchlist_interval_honored,
    )

    grace, grace_honored = resolve_eviction_grace_days(raw.get("eviction_grace_days"))
    out["eviction_grace_days"] = _present_effective(
        raw.get("eviction_grace_days"), grace, EVICTION_GRACE_DAYS_DEFAULT, grace_honored
    )

    retention, retention_honored = resolve_log_retention_days(raw.get("log_retention_days"))
    out["log_retention_days"] = _present_effective(
        raw.get("log_retention_days"), retention, LOG_RETENTION_DAYS_DEFAULT, retention_honored
    )

    max_rows, max_rows_honored = resolve_log_max_rows(raw.get("log_max_rows"))
    out["log_max_rows"] = _present_effective(
        raw.get("log_max_rows"), max_rows, LOG_MAX_ROWS_DEFAULT, max_rows_honored
    )

    autograb_interval, autograb_interval_honored = resolve_auto_grab_interval_seconds(
        raw.get("auto_grab_interval_seconds")
    )
    out["auto_grab_interval_seconds"] = _present_effective(
        raw.get("auto_grab_interval_seconds"),
        autograb_interval,
        AUTO_GRAB_INTERVAL_SECONDS_DEFAULT,
        autograb_interval_honored,
    )

    autograb_max_searches, autograb_max_searches_honored = resolve_auto_grab_max_searches_per_cycle(
        raw.get("auto_grab_max_searches_per_cycle")
    )
    out["auto_grab_max_searches_per_cycle"] = _present_effective(
        raw.get("auto_grab_max_searches_per_cycle"),
        autograb_max_searches,
        AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT,
        autograb_max_searches_honored,
    )

    for key, default in _BOOL_SETTING_DEFAULTS.items():
        value = raw.get(key)
        if value is None:
            continue
        _effective, honored = resolve_bool_setting(key, value, default)
        # Honored -> the stripped token (see the docstring); unrecognized -> the
        # default applies at runtime, so unset (None) is the truthful display.
        out[key] = value.strip() if honored else None

    resolved_policy = resolve_update_policy(raw)
    schedule = resolved_policy.policy.schedule
    honored = resolved_policy.honored_fields
    stored_update_policy = any(raw.get(key) is not None for key in UPDATE_POLICY_SETTING_KEYS)
    out["automatic_update_timezone"] = schedule.timezone_name if stored_update_policy else None
    out["automatic_update_weekdays"] = (
        [day for day in AUTOMATIC_UPDATE_WEEKDAYS_DEFAULT if day in schedule.weekdays]
        if "automatic_update_weekdays" in honored
        else None
    )
    out["automatic_update_window_start"] = (
        schedule.window_start.strftime("%H:%M")
        if "automatic_update_window_start" in honored
        else None
    )
    out["automatic_update_window_end"] = (
        schedule.window_end.strftime("%H:%M") if "automatic_update_window_end" in honored else None
    )
    return out


async def _redacted(store: SettingsStore) -> SettingsResponse:
    return SettingsResponse.model_validate(_sanitize_typed_settings(await store.redacted()))


# Which ``SettingsUpdate`` fields feed each subsystem's health probe (issue #93):
# ``PUT /settings`` clears ONLY the affected subsystem's cached ``GET /health``
# result after a successful save, so an operator who just fixed (or broke) a
# credential sees a fresh probe on the very next poll instead of up to
# ``SUBSYSTEM_PROBE_TTL_SECONDS`` of stale ``ok``/``down``/``not_configured``
# state left over from before the edit. Deliberately a static field->subsystem
# map, not an event-driven invalidation framework -- see
# ``put_settings_endpoint`` for exactly where/when it fires.
_SUBSYSTEM_CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "plex": ("plex_url", "plex_token"),
    "prowlarr": ("prowlarr_url", "prowlarr_api_key"),
    "qbittorrent": ("qbittorrent_url", "qbittorrent_username", "qbittorrent_password"),
    "tmdb": ("tmdb_api_key",),
}


# The library-root fields a PUT can write; mirrors ``routers.setup._ROOT_FIELDS``.
_ROOT_FIELDS: Final[tuple[str, ...]] = (
    "movies_root",
    "tv_root",
    "anime_movie_root",
    "anime_tv_root",
)


async def _resolve_root_writes(body: SettingsUpdate) -> dict[str, str]:
    """isdir-or-remap each root field THIS PUT actually sets to a non-empty value.

    A field absent/``None`` (leave unchanged) or ``""`` (explicit clear --
    ``SettingsUpdate._blank_root_clears_to_unset`` already maps whitespace to
    ``""``) is skipped entirely: no probe, and the normal write loop persists it
    untouched. Raises the same ``library_root_unreachable`` 422 as
    ``routers.setup._resolve_submitted_roots`` when a non-empty root doesn't
    resolve here (issue #132) -- checked BEFORE the write loop, so nothing is
    committed on a rejected root. Resolution goes through the shared
    :func:`~plex_manager.services.path_visibility.remap_library_root`, so a library
    root only ever resolves under the LIBRARY mounts (never ``/downloads``) and a
    whole-media-root library maps to the mount root itself.
    """
    resolved: dict[str, str] = {}
    for field in _ROOT_FIELDS:
        if field not in body.model_fields_set:
            continue
        value = getattr(body, field)
        if value is None or value == "":
            continue
        visible = await asyncio.to_thread(path_visibility.remap_library_root, value)
        if visible is None:
            raise AppError(
                status_code=422,
                code="library_root_unreachable",
                message=f'The library folder "{value}" isn\'t visible to Plex Manager.',
                hint="Pick a folder inside a mounted volume (usually under /media), or fix "
                "the container's volume mounts, then try again.",
                diagnostics={"root": value, "field": field},
            )
        resolved[field] = visible
    return resolved


def _to_stored_string(field: str, value: object) -> str:
    """Render an incoming ``SettingsUpdate`` field value as the plain-text string
    :meth:`SettingsStore.set` persists (``settings.value`` has no typed columns).

    Booleans render lowercase (``"true"``/``"false"``) to match this codebase's
    own convention for the setting (see ``web.deps._get_bool_setting`` and the
    eviction tests that seed ``store.set("eviction_enabled", "true")`` directly)
    rather than Python's capitalized ``str(True)`` -- both round-trip correctly
    through ``web.deps``'s case-insensitive bool parse, but the lowercase form is
    the one actually written elsewhere, so a raw DB read stays consistent
    regardless of which path wrote the value.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if field == "automatic_update_weekdays" and isinstance(value, list):
        return json.dumps(value, separators=(",", ":"))
    return value if isinstance(value, str) else str(value)


@router.get("")
async def get_settings_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SettingsResponse:
    """Return the redacted service config (secrets shown as ``"***"``)."""
    return await _redacted(SettingsStore(session))


@router.get("/plex-libraries")
async def plex_libraries_endpoint(
    library: Annotated[LibraryPort, Depends(get_library)],
) -> list[PlexLibraryOption]:
    """Library folders (movie AND tv) Plex reports, for the Settings
    ``movies_root`` / ``tv_root`` pickers -- each option is tagged by
    ``section_type`` so the frontend can filter to the picker it's rendering.

    Uses the stored Plex creds (no re-typing the token); 409 if Plex is unconfigured.
    """
    # probe_writable=True (the default): authenticated, and the Plex creds are the
    # operator's own stored config — so the real writability signal is legitimate
    # here (unlike the pre-init validate/plex step, which must NOT probe).
    #
    # use_cache=False: this is an infrequent, human-driven read (once per
    # Settings page load, no polling) so it must reflect Plex as it is RIGHT
    # NOW, not a snapshot cached for up to 300s -- otherwise a movie library the
    # operator just added in Plex stays invisible in the movies_root picker for
    # up to 5 minutes (issue #15). Same use_cache=False treatment already given
    # to validate_plex (setup wizard + health dashboard) for the identical
    # reason. The warmed fast paths (is_available/scan/watch_state) are
    # untouched and stay on the cached default.
    return library_options(
        await library.list_sections(use_cache=False),
        probe_writable=True,
        suggest_mounts=path_visibility.KNOWN_LIBRARY_MOUNTS,
    )


def _set_no_store_headers(response: Response) -> None:
    """Forbid any cache from persisting a plaintext-key response (issue #208).

    A caching reverse proxy keyed only on method/URI could otherwise replay one
    authenticated caller's response to a different requester. ``no-store``
    (never write) plus ``private`` (a shared cache must not store it even if it
    ignored ``no-store``) is the modern directive; ``Pragma: no-cache`` is
    carried alongside for HTTP/1.0 intermediaries that predate ``Cache-Control``.
    """
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


@router.get("/app-key")
async def reveal_app_key_endpoint(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AppApiKeyResponse:
    """Return the current app ``X-Api-Key`` in plaintext.

    Authenticated: the caller already proved they have a valid Plex session or
    app key, so this is not an anonymous disclosure -- it is the break-glass
    recovery path for a NEW device/browser that needs to be paired without
    re-running setup.

    Setup mints no key, so a fresh install has none to reveal: that is an honest
    ``app_key_not_set`` envelope (404) whose hint points the operator at the
    Generate control, never a bare/opaque failure (north star #3).

    The success response never touches an intermediate cache (issue #208): the
    plaintext key is a recovery credential, not cacheable content.
    """
    system = await load_system_settings(session)
    if system is None or system.app_api_key is None:
        raise AppError(
            status_code=404,
            code="app_key_not_set",
            message="No recovery key exists.",
            hint="Generate one below.",
        )
    _set_no_store_headers(response)
    return AppApiKeyResponse(app_api_key=system.app_api_key)


@router.get("/app-key/status")
async def app_key_status_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AppApiKeyStatusResponse:
    """Whether a recovery key currently exists, WITHOUT revealing it.

    Lets Settings → Access render Generate (no key yet) vs Rotate/Revoke (a key is
    present) without invoking the break-glass reveal. Admin-gated like every route
    on this router; the plaintext key is never part of this response.
    """
    system = await load_system_settings(session)
    exists = system is not None and system.app_api_key is not None
    return AppApiKeyStatusResponse(exists=exists)


@router.post("/app-key/rotate")
async def rotate_app_key_endpoint(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> AppApiKeyResponse:
    """Mint an app ``X-Api-Key`` -- GENERATE the first one, or ROTATE an existing key.

    This is the sole mint path now that setup is keyless: when no key exists
    (``app_api_key IS NULL``) it GENERATES the first key (the CAS below has nothing
    to compare against, so it simply mints); when a key exists it ROTATES,
    invalidating the old one. Both run under ``_rotate_lock`` and return the
    plaintext exactly once. Like the plaintext GET, the response is never
    cache-eligible (issue #208).

    Every OTHER device/browser with the OLD key saved (localStorage) is
    immediately locked out -- there is exactly one live key at a time, matching
    ``require_api_key``'s single-key comparison. The frontend persists the
    returned key immediately for future access-key recovery, but normal browser
    auth continues to use the Plex session cookie.

    Compare-and-swap against concurrent rotations: two rotate requests can both
    pass authentication against the OLD stored key (each request loads it before
    either commits) regardless of HOW they authenticated — two api-key callers,
    two Plex-SESSION admins, or a mix. Without a guard the write that commits
    second would silently overwrite the first's freshly minted key, so the client
    that fired the first request would be left displaying an already-dead key.
    The re-read/compare/mint/commit is run under the module-level ``_rotate_lock``
    so it is a true atomic read-modify-write rather than check-then-act: the
    compare and the write cannot interleave with another rotation, so the loser's
    re-read runs only AFTER the winner has committed. Inside THIS request's own
    transaction we re-read the stored key and require it to still equal the key
    this request OBSERVED — the presented ``X-Api-Key`` header for api-key auth,
    else (session auth, which carries no key header) the stored value as loaded
    at auth time. If it has already changed, the race happened and we answer 409
    (``app_key_changed``) rather than clobber the winner. The CAS is deliberately
    UNCONDITIONAL on auth method: gating it to api-key callers would let two
    session-authenticated admins re-create the exact dead-key race it exists to
    prevent. The check is skipped only under ``dev_auth_bypass`` (there is no
    authenticated key to compare against), exactly like ``require_api_key``
    itself.

    The CAS also closes the revoke null-hole: a stored key that has become NULL
    is the genuine first-key GENERATE only when this request ALSO observed null.
    If this request observed a NON-null key that a concurrent REVOKE cleared
    mid-flight, minting again would silently resurrect the revoked key, so it
    409s too — a null stored value is not a blanket "nothing to compare, just
    mint".
    """
    system = await ensure_system_settings(session)
    async with _rotate_lock:
        if not get_settings().dev_auth_bypass:
            # The key this request observed BEFORE the fresh read below: api-key
            # callers presented it in the header; session callers observed the value
            # their request session loaded at auth time (``authenticate_request``
            # pulled this row into the identity map, and ``ensure_system_settings``
            # returned that same cached instance — a concurrent commit does not
            # update it).
            observed = _observed_app_key(request, auth, system)
            # Force a fresh read (in the same transaction as the write below, and
            # under _rotate_lock so no other rotation can commit between this read
            # and our own commit) so the CAS reflects any rotation that committed
            # while this request was in flight.
            await session.refresh(system)
            if system.app_api_key is None:
                # No key is stored right now. Two very different reasons land here:
                #  * Genuine first-key GENERATE: this request ALSO observed no key
                #    (``observed`` is None) -- nothing ever existed to change, so
                #    simply MINT (fall through).
                #  * Revoked-key null-hole: this request OBSERVED a non-null key
                #    that a concurrent REVOKE cleared while we were in flight.
                #    Minting now would silently resurrect a just-revoked key, so
                #    honour the same 409 the rotate-vs-rotate CAS gives rather than
                #    defeat the revoke. (The old ``is not None`` guard skipped the
                #    CAS entirely here and re-minted -- the reported null-hole.)
                if observed is not None:
                    raise HTTPException(status_code=409, detail="app_key_changed")
            elif not api_key_matches(observed, system.app_api_key):
                # A key IS stored but it is no longer the one this request observed:
                # a concurrent generate/rotate already minted it, so 409 rather than
                # clobber the winner.
                raise HTTPException(status_code=409, detail="app_key_changed")
        new_key = secrets.token_urlsafe(_API_KEY_BYTES)
        old_key = system.app_api_key
        retired_values = frozenset({old_key}) if old_key is not None else frozenset[str]()
        handler = _log_handler(request)
        async with secret_rotation_lock:
            previous_values = handler.begin_secret_rotation(
                (await SettingsStore(session).secret_values()) | frozenset({new_key})
            )
            try:
                if old_key is not None:
                    await _rewrite_before_secret_replacement(session, frozenset({old_key}))
                system.app_api_key = new_key
                # Invalidate every recovery-cookie session born from the OLD key,
                # except the acting recovery-cookie admin.
                await _revoke_recovery_sessions(
                    session, exempt_token_hash=_acting_recovery_session_hash(request, auth)
                )
                current_values = await SettingsStore(session).secret_values()
                await session.commit()
            except asyncio.CancelledError:
                # Restore the snapshot first, then shield the rollback so a
                # half-cancelled aiosqlite rollback cannot close and poison the
                # connection shared with the drain loop.
                handler.abort_secret_rotation(previous_values)
                await asyncio.shield(session.rollback())
                raise
            except Exception:
                await session.rollback()
                handler.abort_secret_rotation(previous_values)
                raise
            handler.complete_secret_rotation(
                previous_values | retired_values,
                current_values,
            )
    close_realtime_streams(
        request.app,
        reason="app_key_rotated",
        auth_method=AuthMethod.api_key.value,
    )
    publish_realtime(request.app, ("access",), reason="app_key_rotated")
    _set_no_store_headers(response)
    return AppApiKeyResponse(app_api_key=new_key)


@router.delete("/app-key", status_code=204)
async def revoke_app_key_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> None:
    """Revoke the app ``X-Api-Key``: clear the stored key so none authenticates.

    Every device holding the old key is locked out at once (``X-Api-Key`` auth
    401s until a new key is generated); browser Plex-session auth is unaffected.
    Idempotent -- revoking a keyless install is a no-op 204, since the end state is
    the same either way.

    Compare-and-swap, mirroring :func:`rotate_app_key_endpoint`: an EARLIER draft
    loaded ``system`` then wrote ``None`` unconditionally, which lost the update
    when a rotate committed a fresh key in between — a stale revoke (authenticated
    against a now-superseded key) would wipe a key the operator had just rotated
    to. Under ``_rotate_lock`` we re-read the stored key and, if it is non-null
    and no longer the value THIS request observed (the presented ``X-Api-Key``
    header for api-key auth, else the session-loaded value at auth time), 409
    ``app_key_changed`` rather than clobber that rotation. A currently-null stored
    key stays the idempotent 204 no-op (nothing to lose). The check is skipped
    only under ``dev_auth_bypass`` (no authenticated key to compare against),
    exactly like the rotate CAS and ``require_api_key`` itself.
    """
    system = await ensure_system_settings(session)
    async with _rotate_lock:
        if not get_settings().dev_auth_bypass:
            # The key this request observed BEFORE the fresh read below — same
            # sourcing as the rotate CAS: api-key callers presented it in the
            # header; session callers observed the value their request session
            # loaded at auth time (a concurrent commit does not update that cached
            # instance).
            observed = _observed_app_key(request, auth, system)
            # Force a fresh read in this transaction, under _rotate_lock, so the CAS
            # reflects any rotation that committed while this revoke was in flight.
            await session.refresh(system)
            if system.app_api_key is not None and not api_key_matches(observed, system.app_api_key):
                raise HTTPException(status_code=409, detail="app_key_changed")
        old_key = system.app_api_key
        if old_key is None:
            system.app_api_key = None
            await _revoke_recovery_sessions(session)
            await session.commit()
        else:
            handler = _log_handler(request)
            async with secret_rotation_lock:
                previous_values = handler.begin_secret_rotation(
                    await SettingsStore(session).secret_values()
                )
                try:
                    await _rewrite_before_secret_replacement(session, frozenset({old_key}))
                    system.app_api_key = None
                    await _revoke_recovery_sessions(session)
                    current_values = await SettingsStore(session).secret_values()
                    await session.commit()
                except asyncio.CancelledError:
                    # Restore the snapshot first, then shield the rollback so a
                    # half-cancelled aiosqlite rollback cannot close and poison
                    # the connection shared with the drain loop.
                    handler.abort_secret_rotation(previous_values)
                    await asyncio.shield(session.rollback())
                    raise
                except Exception:
                    await session.rollback()
                    handler.abort_secret_rotation(previous_values)
                    raise
                handler.complete_secret_rotation(
                    previous_values | frozenset({old_key}), current_values
                )
    close_realtime_streams(
        request.app,
        reason="app_key_revoked",
        auth_method=AuthMethod.api_key.value,
    )
    publish_realtime(request.app, ("access",), reason="app_key_revoked")


async def _verify_plex_repoint(
    body: SettingsUpdate,
    session: AsyncSession,
    store: SettingsStore,
    client: httpx.AsyncClient,
    context: AuthContext,
) -> tuple[bool, str | None]:
    """Detect a Plex repoint and, when verifiable, derive the NEW server's identity.

    Returns ``(identity_changed, derived_machine_id)``:

    * ``(False, None)`` — this PUT does not change the effective ``plex_url`` /
      ``plex_token`` pair (absent/``null`` fields, the masked-secret ``"***"``
      round-trip, and a same-value re-PUT are all NON-changes). No probe is ever
      issued — an ordinary settings save must not pay a live Plex round-trip.
    * ``(True, machine_id)`` — the identity changes, the EFFECTIVE (post-PUT)
      url+token pair is complete, and the full verification ladder passed. The
      effective value of each half is this PUT's submitted value when it
      carries one, else the currently-stored value.  A masked/omitted token may
      reuse the stored value only when the submitted URL normalizes to the exact
      same configured base; any destination change requires explicit re-entry.
    * ``(True, None)`` — the identity changes but the effective pair is
      INCOMPLETE (a half-configured install, or an explicit clear-to-``""``).
      There is nothing to probe; the caller keeps the settings write but treats
      the repoint as UNVERIFIED (stale-id drop only, no session revocation —
      see :func:`put_settings_endpoint`).

    The verification ladder, all BEFORE anything is written:

    1. ``/identity`` derive (:meth:`PlexTvClient.fetch_server_identity`) — a
       transport failure is the same honest 502 envelope
       (``server_unreachable_from_backend`` / ``server_identity_failed``)
       ``/setup/complete`` and ``/setup/validate/plex`` use, so a
       typo'd-but-parseable url can never commit a broken identity (let alone
       revoke the sessions that could fix it).
    2. AUTHENTICATED check (:func:`assert_plex_token_authorized`): ``/identity``
       is unauthenticated, so step 1 alone would bless a reachable server paired
       with a wrong/revoked token — a committed-but-unusable identity plus a
       fleet-wide sign-out. A rejected token is the 422 ``plex_token_invalid``
       envelope, nothing committed.
    3. OWNERSHIP, for Plex-SESSION callers only
       (:func:`assert_admin_owns_server`, 403 ``server_not_owned``): the caller
       has a Plex account with a stored OAuth token, so the derived id is
       asserted against THEIR OWN plex.tv resources — without this, a session
       admin repointing to a valid but NON-owned server would commit + revoke
       everyone, and their next sign-in resolves NON-admin against the new id: a
       keyless install locked out of Settings (the ADR-0005 violation again). A
       session admin whose row somehow lost its OAuth token cannot be
       ownership-checked and FAILS CLOSED (409 ``plex_account_required`` — sign
       in with Plex again, then retry), never a skipped check. API-KEY (and
       dev-bypass) callers have no Plex account to assert with, so their bar is
       steps 1-2 only — see :func:`put_settings_endpoint`'s honest asymmetry
       note.
    """
    submitted_url = (
        body.plex_url if "plex_url" in body.model_fields_set and body.plex_url is not None else None
    )
    submitted_token = (
        body.plex_token
        if "plex_token" in body.model_fields_set
        and body.plex_token is not None
        and body.plex_token != SECRET_MASK
        else None
    )
    if submitted_url is None and submitted_token is None:
        return False, None
    stored_url = await store.get("plex_url")
    stored_token = await store.get("plex_token")
    identity_changed = (submitted_url is not None and submitted_url != stored_url) or (
        submitted_token is not None and submitted_token != stored_token
    )
    if not identity_changed:
        return False, None
    # EFFECTIVE post-PUT halves: the submitted value when this PUT carries one
    # (including an explicit ``""`` clear), else what is currently stored.
    effective_url = submitted_url if submitted_url is not None else stored_url
    effective_token = submitted_token if submitted_token is not None else stored_token
    if not effective_url or not effective_token:
        return True, None
    # Never forward the STORED token to a newly submitted origin.  A URL-only
    # repoint used to send the encrypted-at-rest credential to that host during
    # /identity and /library/sections verification *before* ownership was known.
    # Even a path-prefix change on the same origin can route to another backend,
    # so anything other than the exact canonical base requires the operator to
    # explicitly re-enter the token they are authorizing us to send.
    if (
        submitted_url is not None
        and submitted_token is None
        and stored_token
        and (not stored_url or not same_service_base(submitted_url, stored_url))
    ):
        raise AppError(
            status_code=422,
            code="credential_reentry_required",
            message="Changing the Plex server address requires the Plex token again.",
            hint=(
                "Re-enter the Plex token so a stored credential is never sent to a new destination."
            ),
        )
    plex_tv = PlexTvClient(
        client,
        client_identifier=await store.get(_CLIENT_ID_SETTING) or _FALLBACK_CLIENT_IDENTIFIER,
    )
    machine_identifier = await plex_tv.fetch_server_identity(effective_url, effective_token)
    # /identity is unauthenticated: prove the EFFECTIVE token is actually
    # accepted by the replacement server before anything is committed.
    await assert_plex_token_authorized(client, effective_url, effective_token)
    if context.method is AuthMethod.plex_session and context.user_id is not None:
        # Session callers get the wizard's ownership bar (ladder step 3 above).
        user = await session.get(User, context.user_id)
        admin_oauth_token = user.encrypted_plex_token if user is not None else None
        if not admin_oauth_token:
            raise AppError(
                status_code=HTTP_409_CONFLICT,
                code="plex_account_required",
                message="Repointing Plex needs your Plex sign-in on file.",
                hint="Sign out, sign back in with Plex, then retry the change.",
            )
        resources = await plex_tv.fetch_resources(admin_oauth_token)
        assert_admin_owns_server(resources, machine_identifier)
    return True, machine_identifier


async def _reject_changed_base_stored_credential_reuse(
    body: SettingsUpdate, store: SettingsStore
) -> None:
    """Require explicit secret re-entry when a configured service base changes.

    Prowlarr and qBittorrent are not probed during a settings write, so a URL-only
    change would otherwise be committed and the next health/search/reconcile call
    would silently send their encrypted stored credential to the new destination.  A
    different reverse-proxy prefix on the same origin can route to another
    backend, so only the exact canonical base may reuse a stored credential.
    """
    protected: tuple[tuple[str, str, str, bool], ...] = (
        ("prowlarr_url", "prowlarr_api_key", "Prowlarr API key", False),
        ("qbittorrent_url", "qbittorrent_password", "qBittorrent password", True),
    )
    for url_field, secret_field, secret_label, empty_secret_is_configured in protected:
        submitted_url = (
            getattr(body, url_field)
            if url_field in body.model_fields_set and getattr(body, url_field) is not None
            else None
        )
        if not isinstance(submitted_url, str) or not submitted_url:
            continue
        stored_url = await store.get(url_field)
        if stored_url and same_service_base(submitted_url, stored_url):
            continue
        submitted_secret = (
            getattr(body, secret_field)
            if secret_field in body.model_fields_set
            and getattr(body, secret_field) not in (None, SECRET_MASK)
            else None
        )
        stored_secret = await store.get(secret_field)
        # qBittorrent accepts an intentionally empty password; Prowlarr treats
        # an empty API key as unconfigured and therefore sends no credential.
        stored_credential_exists = (
            stored_secret is not None if empty_secret_is_configured else bool(stored_secret)
        )
        if stored_credential_exists and submitted_secret is None:
            raise AppError(
                status_code=422,
                code="credential_reentry_required",
                message=f"Changing the service address requires the {secret_label} again.",
                hint=(
                    f"Re-enter the {secret_label} so a stored credential is never sent "
                    "to a new destination."
                ),
            )


@router.put("", responses=_PUT_SETTINGS_RESPONSES)
async def put_settings_endpoint(
    body: SettingsUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    context: Annotated[AuthContext, Depends(require_admin)],
    health_cache: Annotated[TtlCache[SubsystemHealth], Depends(get_health_cache)],
) -> SettingsResponse:
    """Upsert the provided config and return the redacted result.

    A secret field whose incoming value is the redaction mask (``"***"``) is a
    no-op: GET returns ``"***"`` for a configured secret, so a FE that round-trips
    the whole object back (e.g. after editing only ``plex_url``) must not clobber
    the real credential with the mask — a silent secret-wipe that would only
    surface later as an auth failure to the downstream service.

    The disk-pressure pair is cross-checked against the EFFECTIVE (post-update)
    values, not just what this one request happens to carry: ``SettingsUpdate``'s
    own ``model_validator`` only catches a target above the threshold when BOTH
    are submitted together, but ``PUT`` is a PARTIAL update — sending just one
    side against an already-stored (and now-inverted) other side would otherwise
    silently leave the whole threshold-to-target band unable to relieve pressure
    (see :func:`~plex_manager.web.routers.settings._validate_disk_pressure_pair`).
    Checked, and rejected with the SAME 422 shape, BEFORE anything is written.

    Repointing Plex is VERIFIED before it is committed. The
    ``plex_machine_identifier`` snapshot (:data:`PLEX_MACHINE_ID_SETTING`) is the
    id post-init sign-in trusts to admit users, so when this PUT actually CHANGES
    the effective ``plex_url``/``plex_token`` the full verification ladder in
    :func:`_verify_plex_repoint` runs FIRST: the REPLACEMENT server's
    ``/identity`` derive, then an AUTHENTICATED ``list_sections`` check with the
    effective token (``/identity`` is unauthenticated, so reachability alone
    would bless a wrong/revoked token), then — for Plex-SESSION callers — the
    wizard's ownership assertion. The same code paths ``/setup/complete`` and
    ``/setup/validate/plex`` use. A masked/omitted token is resolved to the
    stored real one only when the configured base is unchanged after
    normalization; a new path, scheme, host, or port requires explicit token
    re-entry before any probe. Only a server that passes gets committed: the
    settings are written, the freshly DERIVED id replaces the cached one (better
    than clearing it — it was just derived, so sign-in never needs a per-request
    re-probe), and every active browser session is revoked. Any verification
    failure is its honest envelope (502 unreachable, 422 ``plex_token_invalid``,
    403 ``server_not_owned``) with NOTHING committed and every session intact —
    a typo'd url or wrong credential must not both break sign-in AND sign
    everyone out, which would leave a keyless install recoverable only by DB
    surgery (the exact never-locked-out violation ADR-0005 forbids).

    OWNERSHIP asymmetry — honestly documented: a Plex-SESSION admin has a Plex
    account with a stored OAuth token, so the derived id is asserted against
    their OWN plex.tv resources (403 ``server_not_owned`` otherwise) BEFORE
    committing/revoking — without it, repointing to a valid but NON-owned server
    would revoke everyone and the admin's next sign-in resolves NON-admin
    against the new id: a keyless install locked out of Settings. An
    ``X-Api-Key`` (or dev-bypass) caller has NO Plex account to assert with, so
    its bar is reachability + the authenticated-token check ONLY: an api-key
    repoint to a reachable, token-accepting but non-owned server remains
    possible. That residual is accepted and recoverable BY CONSTRUCTION — the
    api key that made the change keeps working (session revocation never touches
    api-key auth), so the same key can always repoint back. Ownership continues
    to gate who can SIGN IN (and who is admin), which the freshly derived
    machine id anchors to the NEW server (ADR-0016).

    Why sessions are revoked on a verified repoint: clearing/replacing the id
    alone only changes how FUTURE sign-ins resolve server access; an
    already-minted :class:`AuthSession` keeps authorizing against its persisted
    ``User.permissions`` for up to 30 days, so the OLD server's users (and
    admins) would silently survive the repoint. A repoint is an auth-domain
    change (ADR-0016 derives every session's authority from access to THE
    configured server), so everyone — including the admin performing the
    repoint — must re-sign-in and be re-evaluated against the NEW server. The
    self-lockout is deliberate and honest, not collateral damage: this request
    already passed auth at dependency time, so the response completes normally
    for the now-revoked session, and the admin's very next request
    re-authenticates against a server this PUT just PROVED is answering.
    Revocation stamps ``revoked_at`` on rows where it is NULL (the model's
    auditable-revoke convention) rather than deleting; API-key auth is
    untouched, so the ``X-Api-Key`` recovery path still works throughout.

    An UNVERIFIABLE identity change — the effective pair is incomplete (a
    half-configured install, or an explicit ``""`` clear) — cannot be probed:
    the write proceeds, the STALE cached id is dropped (nothing may keep
    anchoring sign-in to the old server), but sessions are NOT revoked. An
    incomplete identity cannot mint new sign-ins anyway (an honest
    ``service_not_configured``), and revoking on a half-configured install is
    the same lockout trap the probe exists to prevent; the PUT that completes
    the pair is a verified repoint and revokes then.

    Submitted library roots are likewise gated BEFORE the write loop
    (:func:`_resolve_root_writes`, issue #132): a non-empty root that isn't
    visible to this container (a HOST-namespace path, e.g. from a stale client)
    is 422 ``library_root_unreachable`` with nothing committed; one that IS
    visible only via a container-mount remap is persisted as the REMAPPED path,
    not the raw submitted one.

    After a successful commit, invalidates (issue #93) the cached ``GET /health``
    probe for every subsystem whose credential field(s) were ACTUALLY persisted
    this call (see :data:`_SUBSYSTEM_CREDENTIAL_FIELDS`) — tracked separately from
    ``body.model_fields_set`` because a field can be present-but-``None`` (leave
    unchanged) or a secret sent back as the ``"***"`` mask (also a no-op); neither
    should invalidate anything, since nothing about that subsystem's config
    actually changed. Runs strictly AFTER ``session.commit()`` so a failed save
    (a raised validation error above, a failed verification, or a DB error during
    the write loop/commit) never touches the cache — a failed write must leave any
    still-valid cached probe exactly as it was. This covers the Plex repoint path
    too, with no special-casing: a verified (or unverifiable-but-written) repoint's
    ``plex_url``/``plex_token`` land in ``written_fields`` exactly like any other
    field, so the very next ``GET /health`` re-probes the NEW server instead of
    serving a stale pre-repoint ``ok``/``down`` card.
    """
    # Security-critical read/check/write boundary: see ``_settings_update_lock``.
    # The redacted response stays inside too, so a waiting PUT cannot change rows
    # between this request's commit and the values it returns to the operator.
    async with _settings_update_lock:
        # Authentication dependencies use this same AsyncSession before the
        # endpoint acquires the lock. End that read transaction now so a request
        # that waited behind another PUT cannot retain a pre-lock database
        # snapshot while validating its effective destination/credential pair.
        # The dependencies leave only primitive AuthContext data and make no
        # writes, so rolling their read transaction back is safe.
        await session.rollback()
        await _validate_disk_pressure_pair(body, session)
        await _validate_update_window_pair(body, session)

        store = SettingsStore(session)
        await _reject_changed_base_stored_credential_reuse(body, store)
        # Verify BEFORE any write: a failed verification must leave nothing behind.
        plex_identity_changed, machine_identifier = await _verify_plex_repoint(
            body, session, store, client, context
        )
        # Likewise verify every submitted library root is visible to THIS container
        # (issue #132) before anything is written.
        resolved_roots = await _resolve_root_writes(body)

        written_fields: set[str] = set()
        submitted_secret_fields = {
            field
            for field in body.model_fields_set
            if field in SECRET_SETTING_KEYS
            and getattr(body, field) is not None
            and getattr(body, field) != SECRET_MASK
        }
        old_secret_values_list: list[str] = []
        changing_secret_fields: set[str] = set()
        for field in submitted_secret_fields:
            old_value = await store.get(field)
            if old_value != _to_stored_string(field, getattr(body, field)):
                changing_secret_fields.add(field)
                if old_value:
                    old_secret_values_list.append(old_value)
        old_secret_values = frozenset(old_secret_values_list)
        transition_values = (await store.secret_values()) | frozenset(
            _to_stored_string(field, getattr(body, field)) for field in changing_secret_fields
        )

        async def write_settings() -> None:
            for field in body.model_fields_set:
                value = getattr(body, field)
                if value is None:
                    continue
                if field in SECRET_SETTING_KEYS and value == SECRET_MASK:
                    continue
                if field in resolved_roots:
                    value = resolved_roots[field]
                await store.set(field, _to_stored_string(field, value))
                written_fields.add(field)
            if plex_identity_changed:
                if machine_identifier is None:
                    await store.delete(PLEX_MACHINE_ID_SETTING)
                else:
                    await store.set(PLEX_MACHINE_ID_SETTING, machine_identifier)
                    await session.execute(
                        update(AuthSession)
                        .where(AuthSession.revoked_at.is_(None))
                        .values(revoked_at=datetime.now(UTC))
                    )

        if changing_secret_fields:
            async with _secret_rotation(session, request, transition_values, old_secret_values):
                await write_settings()
        else:
            await write_settings()
            await session.commit()

        # A long configured interval must not postpone an enable/shorten change --
        # nor a Plex identity change's snapshot cleanup -- until the old sleep
        # expires. BOTH identity-change shapes need the immediate wake: a verified
        # repoint (new machine identifier) leaves old-server tokens STALE for the
        # new server and the watchlist worker is what clears their snapshots
        # (#296); an UNVERIFIABLE change (an explicit clear / incomplete pair,
        # which dropped the cached anchor above) leaves the install truly
        # unconfigured, and the worker's not_configured branch is what clears the
        # now-orphaned snapshot rows (#327) -- without the wake they keep
        # protecting titles from eviction until the next scheduled tick
        # (hours/days away) despite the operator explicitly walking away from
        # Plex. The worker owns this process-local event.
        if plex_identity_changed or written_fields.intersection(
            {"watchlist_sync_enabled", "watchlist_sync_interval_minutes"}
        ):
            wake_event = getattr(request.app.state, "watchlist_wake_event", None)
            if isinstance(wake_event, asyncio.Event):
                wake_event.set()

        # Same immediacy for the auto-grab worker (issue #332): a shortened
        # interval, a changed per-cycle search cap, or a re-enable must be
        # observed on the next tick, not after the OLD (up to 1h) sleep expires.
        # ``_autograb_loop`` re-reads all three fresh when woken; the worker owns
        # this process-local event.
        if written_fields.intersection(
            {
                "auto_grab_enabled",
                "auto_grab_interval_seconds",
                "auto_grab_max_searches_per_cycle",
            }
        ):
            autograb_wake_event = getattr(request.app.state, "autograb_wake_event", None)
            if isinstance(autograb_wake_event, asyncio.Event):
                autograb_wake_event.set()

        # Clear backend probe caches before publishing: a listening tab can refetch
        # immediately on the SSE event, so publishing first could race it into the
        # stale pre-update health snapshot.
        for subsystem, fields in _SUBSYSTEM_CREDENTIAL_FIELDS.items():
            if written_fields.intersection(fields):
                health_cache.invalidate(subsystem)

        if plex_identity_changed and machine_identifier is not None:
            close_realtime_streams(
                request.app,
                reason="plex_server_repointed",
                auth_method=AuthMethod.plex_session.value,
            )
        if written_fields or plex_identity_changed:
            topics = ["settings", "ops:health"]
            if written_fields.intersection(_UPDATE_POLICY_FIELDS):
                topics.append("updates")
            publish_realtime(
                request.app,
                tuple(topics),
                reason="settings_updated",
            )

        return await _redacted(store)


async def _validate_update_window_pair(body: SettingsUpdate, session: AsyncSession) -> None:
    """Validate the effective partial-update window before persisting either side."""
    fields = body.model_fields_set
    store = SettingsStore(session)
    start = (
        body.automatic_update_window_start
        if "automatic_update_window_start" in fields
        and body.automatic_update_window_start is not None
        else _parse_update_time(await store.get("automatic_update_window_start"))
        or AUTOMATIC_UPDATE_WINDOW_START_DEFAULT
    )
    end = (
        body.automatic_update_window_end
        if "automatic_update_window_end" in fields and body.automatic_update_window_end is not None
        else _parse_update_time(await store.get("automatic_update_window_end"))
        or AUTOMATIC_UPDATE_WINDOW_END_DEFAULT
    )
    if start == end:
        raise HTTPException(
            status_code=422,
            detail="automatic update window start and end must differ",
        )


async def _validate_disk_pressure_pair(body: SettingsUpdate, session: AsyncSession) -> None:
    """422 when the EFFECTIVE (threshold, target) pair would be inverted.

    "Effective" means: this request's submitted value for a side, when it
    actually supplies one (present in ``model_fields_set`` AND non-``null`` —
    mirroring ``put_settings_endpoint``'s own persist loop, which treats a
    ``null`` field as "leave unchanged", never as "clear to null"); otherwise
    whatever is CURRENTLY STORED for that side (via the same typed getters the
    eviction sweep itself reads, so this check reasons about the identical
    effective values the sweep would see after this PUT commits). Catches the
    single-field split-update ``SettingsUpdate._target_at_or_below_threshold``
    documents as its known residual: e.g. a stored target of 80 plus a PUT
    naming only ``disk_pressure_threshold_percent=70`` would otherwise leave
    ``target(80) > threshold(70)`` in effect, with nothing else ever
    cross-checking it.
    """
    fields = body.model_fields_set
    threshold = (
        body.disk_pressure_threshold_percent
        if "disk_pressure_threshold_percent" in fields
        and body.disk_pressure_threshold_percent is not None
        else await get_disk_pressure_threshold_percent(session)
    )
    target = (
        body.disk_pressure_target_percent
        if "disk_pressure_target_percent" in fields
        and body.disk_pressure_target_percent is not None
        else await get_disk_pressure_target_percent(session)
    )
    if target > threshold:
        raise HTTPException(
            status_code=422,
            detail=(
                "disk_pressure_target_percent must be <= disk_pressure_threshold_percent "
                "(a target above the trigger leaves the whole threshold-to-target band "
                "under 'pressure' with nothing to evict)"
            ),
        )
