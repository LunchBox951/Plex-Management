"""FastAPI dependencies: DB session, api-key auth, settings store, adapters.

Wiring rules:

* ``get_session`` reuses :func:`plex_manager.db.get_session`.
* ``require_api_key`` enforces the static ``X-Api-Key`` header against
  ``SystemSettings.app_api_key`` — which is Fernet-encrypted at rest, so the
  plaintext is never on disk; the header is constant-time-compared against the
  decrypted value. The header is sourced via ``APIKeyHeader`` so the security
  scheme appears in the OpenAPI. It is skipped when ``settings.dev_auth_bypass``
  is set. Health, setup and docs routes do NOT depend on it.
* ``require_setup_admin`` gates every setup endpoint except ``/status``: a
  ``dev_auth_bypass`` short-circuit, then an OPTIONAL pre-init hardening token
  (``PLEX_MANAGER_SETUP_TOKEN``, only enforced while uninitialized), then normal
  session-cookie-or-``X-Api-Key`` auth, then an admin check — every rejection an
  ``AppError`` envelope, never a bare detail. It is the SOLE setup gate: the legacy
  pre-init token dependencies were removed when the setup router migrated onto it.
* ``SettingsStore`` is the typed access layer over the ``settings`` table: secret
  values (Plex token, Prowlarr / TMDB api keys, qBittorrent password) go to the
  Fernet-encrypted ``encrypted_value`` column; non-secret values (urls,
  usernames) go to plaintext ``value``. The redacted view never exposes a secret.
* ``get_tmdb`` / ``get_prowlarr`` / ``get_qbittorrent`` build a configured adapter
  from the decrypted settings plus the shared ``httpx.AsyncClient``. A missing
  required setting raises :class:`ServiceNotConfiguredError` (HTTP 409), never a
  crash.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, cast

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import State

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.media_probe.ffprobe import FfprobeMediaProbe
from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.adapters.plex.library import PlexLibrary
from plex_manager.adapters.prowlarr.adapter import ProwlarrIndexer
from plex_manager.adapters.qbittorrent.adapter import QbittorrentClient
from plex_manager.adapters.tmdb.adapter import TmdbMetadata
from plex_manager.config import get_settings
from plex_manager.db import get_session, get_sessionmaker
from plex_manager.domain.quality_profile import QualityProfile, default_profile
from plex_manager.models import AuthSession, Setting, SystemSettings, User
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.filesystem import FileSystemPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.media_probe import MediaProbePort
from plex_manager.ports.metadata import MetadataPort
from plex_manager.ports.parser import ParserPort
from plex_manager.services import log_capture_service, path_visibility
from plex_manager.services.health_service import (
    AutograbStatus,
    ReconcileStatus,
    SubsystemHealth,
    TtlCache,
)
from plex_manager.services.update_policy import (
    AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT,
    AUTOMATIC_UPDATE_TIMEZONE_DEFAULT,
    AUTOMATIC_UPDATE_WEEKDAYS_DEFAULT,
    AUTOMATIC_UPDATE_WINDOW_END_DEFAULT,
    AUTOMATIC_UPDATE_WINDOW_START_DEFAULT,
    AUTOMATIC_UPDATES_ENABLED_DEFAULT,
)
from plex_manager.web.errors import AppError
from plex_manager.web.settings_bounds import (
    DISK_PRESSURE_PERCENT_MAX,
    DISK_PRESSURE_PERCENT_MIN,
    EVICTION_GRACE_DAYS_MAX,
    EVICTION_INTERVAL_MAX_MINUTES,
    LOG_MAX_ROWS_MAX,
    LOG_RETENTION_DAYS_MAX,
)

__all__ = [
    "API_KEY_HEADER_NAME",
    "AUTOMATIC_UPDATES_ENABLED_DEFAULT",
    "AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT",
    "AUTOMATIC_UPDATE_TIMEZONE_DEFAULT",
    "AUTOMATIC_UPDATE_WEEKDAYS_DEFAULT",
    "AUTOMATIC_UPDATE_WINDOW_END_DEFAULT",
    "AUTOMATIC_UPDATE_WINDOW_START_DEFAULT",
    "AUTO_GRAB_ENABLED_DEFAULT",
    "CSRF_COOKIE_NAME",
    "CSRF_HEADER_NAME",
    "DISK_PRESSURE_PERCENT_MAX",
    "DISK_PRESSURE_PERCENT_MIN",
    "DISK_PRESSURE_TARGET_PERCENT_DEFAULT",
    "DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT",
    "EVICTION_ENABLED_DEFAULT",
    "EVICTION_GRACE_DAYS_DEFAULT",
    "EVICTION_GRACE_DAYS_MAX",
    "EVICTION_INTERVAL_MAX_MINUTES",
    "EVICTION_INTERVAL_MINUTES_DEFAULT",
    "EVICTION_PROACTIVE_ENABLED_DEFAULT",
    "KNOWN_SETTING_KEYS",
    "LOG_MAX_ROWS_DEFAULT",
    "LOG_MAX_ROWS_MAX",
    "LOG_RETENTION_DAYS_DEFAULT",
    "LOG_RETENTION_DAYS_MAX",
    "PLEX_MACHINE_ID_SETTING",
    "SECRET_MASK",
    "SECRET_SETTING_KEYS",
    "SESSION_COOKIE_NAME",
    "SETUP_TOKEN_HEADER_NAME",
    "AuthContext",
    "AuthMethod",
    "DiskPressurePercents",
    "ServiceNotConfiguredError",
    "SettingsStore",
    "api_key_matches",
    "authenticate_request",
    "enforce_pre_init_setup_token",
    "ensure_system_settings",
    "get_anime_movie_root_optional",
    "get_anime_tv_root_optional",
    "get_auto_grab_enabled",
    "get_autograb_status",
    "get_disk_pressure_target_percent",
    "get_disk_pressure_threshold_percent",
    "get_downloads_host_root",
    "get_eviction_enabled",
    "get_eviction_filesystem",
    "get_eviction_grace_days",
    "get_eviction_interval_minutes",
    "get_eviction_proactive_enabled",
    "get_filesystem",
    "get_health_cache",
    "get_http_client",
    "get_library",
    "get_library_optional",
    "get_log_handler",
    "get_log_max_rows",
    "get_log_retention_days",
    "get_media_probe",
    "get_movies_root",
    "get_movies_root_optional",
    "get_parser",
    "get_prowlarr",
    "get_qbittorrent",
    "get_qbittorrent_optional",
    "get_quality_profile",
    "get_reconcile_status",
    "get_session",
    "get_tmdb",
    "get_tv_root",
    "get_tv_root_optional",
    "hash_session_token",
    "is_setup_token_required",
    "load_system_settings",
    "require_admin",
    "require_admin_short_session",
    "require_api_key",
    "require_api_key_short_session",
    "require_setup_admin",
    "resolve_bool_setting",
    "resolve_disk_pressure_percents",
    "resolve_eviction_grace_days",
    "resolve_eviction_interval_minutes",
    "resolve_log_max_rows",
    "resolve_log_retention_days",
    "resolve_qbittorrent",
]

_logger = logging.getLogger(__name__)

# The bearer-token header. Declared via ``APIKeyHeader`` (below) so FastAPI emits
# the security scheme + per-route requirement into the OpenAPI document — without
# it, generated clients would treat protected routes as unauthenticated and omit
# the key.
API_KEY_HEADER_NAME = "X-Api-Key"
SETUP_TOKEN_HEADER_NAME = "X-Setup-Token"  # noqa: S105 — header name, not a token
# The ``settings.key`` under which the configured server's Plex ``machineIdentifier``
# is stored at setup-complete. The single source of truth shared by the setup router
# (writes it) and the auth router (reads it post-init to resolve server access
# without re-probing ``/identity``). Not a wire/``SettingsResponse`` field — an
# internal identifier, so deliberately NOT in ``KNOWN_SETTING_KEYS`` (mirroring
# ``plex_oauth_client_identifier``), read/written via ``SettingsStore`` by key.
PLEX_MACHINE_ID_SETTING = "plex_machine_identifier"
SESSION_COOKIE_NAME = "plexmgr.session"
CSRF_COOKIE_NAME = "plexmgr.csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
# ``auto_error=False``: we do the rejection ourselves so the failure detail stays
# the stable ``invalid_api_key`` (and so the pre-init paths can stay open).
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


class AuthMethod(StrEnum):
    """How the current request authenticated."""

    api_key = "api_key"
    plex_session = "plex_session"
    dev_bypass = "dev_bypass"


@dataclass(frozen=True)
class AuthContext:
    """Authenticated request identity.

    ``user_*`` is populated only for Plex session auth. The legacy app API key has
    no user identity and remains a recovery/automation credential.
    """

    method: AuthMethod
    user_id: int | None = None
    plex_id: int | None = None
    username: str | None = None
    email: str | None = None
    avatar_url: str | None = None
    is_admin: bool = False
    session_expires_at: datetime | None = None


# The canonical config keys (also the ``settings.key`` values and the wire field
# names in the settings schema — one stable naming, no translation layer).
# ``movies_root`` / ``tv_root`` are the on-disk library folders the importer routes
# movies / tv into; both are non-secret config (a path), entered at setup and
# editable in Settings. ``tv_root`` is OPTIONAL everywhere ``movies_root`` is
# required nowhere new: an install may configure only one, or both.
KNOWN_SETTING_KEYS: tuple[str, ...] = (
    "plex_url",
    "plex_token",
    "prowlarr_url",
    "prowlarr_api_key",
    "qbittorrent_url",
    "qbittorrent_username",
    "qbittorrent_password",
    "tmdb_api_key",
    "movies_root",
    "tv_root",
    # Operability beta (ADR-0012) — every one of these is web-editable, plain
    # (never secret) config: disk-pressure eviction tuning + the log-retention
    # window. Read via the typed getters below, each with a safe, honest
    # fallback default when unset or unparsable (never a crash, never a silently
    # wrong threshold) -- see ``docs/design/operability-beta-plan.md``.
    "disk_pressure_threshold_percent",
    "disk_pressure_target_percent",
    "eviction_grace_days",
    "eviction_enabled",
    "eviction_proactive_enabled",
    "eviction_interval_minutes",
    "log_retention_days",
    # The row-count companion to log_retention_days (issue #152): bounds
    # log_events growth even when age-based pruning alone never trips (a
    # chatty install with a generous retention window).
    "log_max_rows",
    # Auto-grab worker (ADR-0013): the master on/off switch for the background
    # request->search->grab loop. Web-editable, plain boolean config (default ON),
    # read every tick so a web toggle takes effect on the next cycle -- the
    # north-star #1 "turn this bot off with a button, never a terminal" switch. The
    # manual Grab button stays the override regardless.
    "auto_grab_enabled",
    # Anime library routing (ADR-0015): two OPTIONAL roots, mirroring tv_root's
    # optional treatment exactly. Unset ⇒ anime imports fall back to
    # movies_root/tv_root, i.e. identical behavior to before this feature
    # existed. Read via get_anime_movie_root_optional/get_anime_tv_root_optional
    # below.
    "anime_movie_root",
    "anime_tv_root",
    # Opt-in container updates (ADR-0024). The updater's bearer credential is a
    # Compose secret and deliberately does not appear in this public store.
    "automatic_updates_enabled",
    "automatic_update_timezone",
    "automatic_update_weekdays",
    "automatic_update_window_start",
    "automatic_update_window_end",
    "automatic_update_idle_only",
)

# Keys whose values are secrets: stored encrypted, masked on read. Everything
# else is plaintext config (urls, usernames).
SECRET_SETTING_KEYS: frozenset[str] = frozenset(
    {"plex_token", "prowlarr_api_key", "qbittorrent_password", "tmdb_api_key"}
)

# Public so the settings router can recognise a redacted secret on round-trip and
# skip it (avoids clobbering a stored secret with the literal mask).
SECRET_MASK = "***"  # noqa: S105 — a redaction placeholder, not a credential


class ServiceNotConfiguredError(Exception):
    """A required adapter credential is missing — surfaced as HTTP 409.

    Honest, not a crash: the operator gets ``{"detail": "service_not_configured",
    "service": "<name>"}`` so the UI can route them back to setup.
    """

    def __init__(self, service: str) -> None:
        self.service = service
        super().__init__(f"service not configured: {service}")


# --------------------------------------------------------------------------- #
# SystemSettings helpers
# --------------------------------------------------------------------------- #
async def load_system_settings(session: AsyncSession) -> SystemSettings | None:
    """Return the single ``system_settings`` row, or ``None`` if not yet created.

    Ordered by ``id`` for determinism: the row is pinned to ``id=1`` (a CHECK
    constraint forbids any other), so this is belt-and-braces, but a bare
    ``limit(1)`` without an ``ORDER BY`` has no guaranteed row order.
    """
    result = await session.execute(select(SystemSettings).order_by(SystemSettings.id).limit(1))
    return result.scalars().first()


async def ensure_system_settings(session: AsyncSession) -> SystemSettings:
    """Return the install-state row, creating an uninitialized one if absent.

    Concurrency-safe: the row is pinned to ``id=1``. Two workers starting on an
    empty DB can both pass the ``load_system_settings`` check and both attempt the
    insert; the loser collides on the primary key (id=1) and raises
    ``IntegrityError``, which we catch, roll back, and resolve by re-reading the
    winner's row — never two rows, never a crash (honesty over silence).
    """
    row = await load_system_settings(session)
    if row is not None:
        return row
    row = SystemSettings(id=1, initialized=False)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await load_system_settings(session)
        if existing is None:  # pragma: no cover - the conflicting row must exist
            raise
        return existing
    return row


# --------------------------------------------------------------------------- #
# Settings store
# --------------------------------------------------------------------------- #
class SettingsStore:
    """Typed get/set of service config in the ``settings`` table.

    Secrets are routed to the encrypted column transparently; the caller never
    decides which column to use. The redacted view masks secrets so a GET can
    never leak them.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, key: str) -> Setting | None:
        result = await self._session.execute(select(Setting).where(Setting.key == key))
        return result.scalars().first()

    async def get(self, key: str) -> str | None:
        """Return the decrypted value for ``key``, or ``None`` if unset."""
        row = await self._row(key)
        if row is None:
            return None
        if key in SECRET_SETTING_KEYS:
            return row.encrypted_value
        return row.value

    async def set(self, key: str, value: str) -> None:
        """Upsert ``key``. Secret keys are written encrypted, plaintext otherwise.

        The secret/plaintext routing is derived from :data:`SECRET_SETTING_KEYS`,
        so a secret can never accidentally be persisted in the plaintext column.

        Concurrency-safe for the first write of a brand-new key: ``settings.key``
        is UNIQUE, so two requests racing to set the SAME never-before-seen key
        (e.g. two concurrent ``PUT /settings`` submissions from two admin tabs,
        both writing ``movies_root`` for the first time) can both pass the
        ``_row`` "not found yet" check and both attempt an INSERT. Recovery
        follows the same insert-then-recover shape as :func:`ensure_system_settings`
        -- catch the loser's ``IntegrityError``, roll back, and resolve by
        re-reading the now-existing (winner's) row -- but scoped to a SAVEPOINT
        (``begin_nested``) around just this INSERT, not a full-transaction
        rollback. ``set`` is called repeatedly inside ONE caller transaction
        (``PUT /settings`` loops over every submitted field; ``POST
        /setup/complete`` writes ~12 keys) that commits once at the end; a
        full ``session.rollback()`` here would discard every EARLIER key this
        same request already flushed successfully in this loop, silently
        dropping their writes even though the request still returns 200 --
        trading a loud, retryable 500 for silent partial data loss, which is
        exactly what "honesty over silence" forbids. The SAVEPOINT confines the
        rollback to this one failed INSERT, leaving prior flushes in the same
        transaction intact. Unlike ``ensure_system_settings`` (where either
        side's freshly-created row is equivalent, so conceding to the winner is
        enough), a ``set`` caller is asking for a SPECIFIC value; simply
        returning the winner's row would silently drop this call's write. So the
        recovered row is then updated with THIS call's value exactly like the
        ordinary update path below -- the net effect is a true upsert: the last
        write to actually commit always wins, and exactly one row exists either
        way.

        Last-write-wins is the RIGHT resolution for ordinary settings -- every
        caller is storing a preference, and the later write should win exactly
        as it would have arriving sequentially -- but it makes ``set`` unsuitable
        for CREATE-ONCE identity keys: a loser absorbing its insert conflict
        here falls through to the value assignment below and OVERWRITES the
        winner's freshly minted identity, with no way to tell the caller which
        value actually survived. Keys that must never rotate once minted (e.g.
        the plex.tv client identifier) go through :meth:`set_if_absent` instead.
        """
        is_secret = key in SECRET_SETTING_KEYS
        row = await self._row(key)
        if row is None:
            # The SAVEPOINT must be opened BEFORE the new ``Setting`` is added:
            # ``begin_nested()`` takes an internal snapshot by flushing whatever
            # is already pending, so adding the row first would let its INSERT
            # -- and a possible collision -- happen during that snapshot, before
            # any SAVEPOINT exists to roll back to.
            nested = await self._session.begin_nested()
            row = Setting(key=key, is_secret=is_secret)
            self._session.add(row)
            try:
                await self._session.flush()
            except IntegrityError:
                # Roll back via the SAVEPOINT object itself, NOT ``self._session.
                # rollback()``: a bare session-level rollback here would discard
                # the WHOLE outer transaction (every key this same request has
                # already ``set()`` earlier in the loop), not just this failed
                # INSERT -- silently dropping prior writes behind an eventual 200,
                # exactly the "honesty over silence" violation this exists to
                # avoid. Rolling back the SAVEPOINT undoes only this INSERT and
                # leaves everything flushed before it intact.
                await nested.rollback()
                existing = await self._row(key)
                if existing is None:  # pragma: no cover - the conflicting row must exist
                    raise
                row = existing
            else:
                # Release the SAVEPOINT on success so it doesn't linger open
                # for the rest of this session -- ``set()`` is called repeatedly
                # in a loop (``PUT /settings``, ``POST /setup/complete``) and each
                # call must leave the transaction exactly as it found it.
                await nested.commit()
        row.is_secret = is_secret
        if is_secret:
            row.encrypted_value = value
            row.value = None
        else:
            row.value = value
            row.encrypted_value = None
        await self._session.flush()

    async def set_if_absent(self, key: str, value: str) -> str:
        """Create ``key`` exactly once; return the value that actually persisted.

        The CREATE-ONCE counterpart to :meth:`set`, for keys that are minted
        IDENTITIES rather than preferences. :meth:`set` is an upsert whose
        concurrent-first-write recovery deliberately resolves to last-write-wins
        -- right when every caller is asking to store a preference, wrong when
        the key is an identity that must never rotate once minted. The canonical
        example is ``plex_oauth_client_identifier``, the app's persisted plex.tv
        device identity: plex.tv registers every DISTINCT
        ``X-Plex-Client-Identifier`` as a NEW device on the operator's account,
        and the auth, setup, and settings routers all present the stored value
        on their plex.tv calls, so exactly one identifier may ever persist.
        Routing such a key through :meth:`set` would let the LOSER of two racing
        first sign-ins absorb its insert conflict and then OVERWRITE the
        winner's freshly persisted identifier while returning its own, unstored
        candidate to its caller -- the two racers would proceed under DIFFERENT
        device identities and the stored one would have silently rotated
        mid-flight.

        Semantics: if a value is already persisted under ``key``, return it
        untouched and write nothing (this call's candidate is discarded).
        Otherwise INSERT this call's ``value`` inside a SAVEPOINT -- the same
        scoped recovery as :meth:`set`; see that docstring for why a
        full-session rollback is forbidden here -- and if the INSERT collides
        with a concurrent first write, roll the SAVEPOINT back, re-read, and
        return the WINNER's committed value: never overwrite it, and never
        raise for what is a benign race. The return value is therefore always
        the value that actually persisted, so every racer converges on the same
        identity. Secret keys route to the encrypted column exactly like
        :meth:`set`.
        """
        is_secret = key in SECRET_SETTING_KEYS
        row = await self._row(key)
        if row is None:
            # SAVEPOINT before add(), for the same snapshot reason as set().
            nested = await self._session.begin_nested()
            row = Setting(key=key, is_secret=is_secret)
            self._session.add(row)
            try:
                await self._session.flush()
            except IntegrityError:
                # A concurrent first write won. Scoped rollback (see set()),
                # then ADOPT the winner's row -- crucially, without falling
                # through to the value assignment below.
                await nested.rollback()
                existing = await self._row(key)
                if existing is None:  # pragma: no cover - the conflicting row must exist
                    raise
                row = existing
            else:
                await nested.commit()
        persisted = row.encrypted_value if is_secret else row.value
        if persisted is not None:
            return persisted
        # No value has ever persisted under this key: either the row was created
        # by THIS call just above, or (degenerately) a pre-existing row carries
        # nothing in the column this key's current classification reads. Writing
        # this call's value here IS the create, not a rotation.
        row.is_secret = is_secret
        if is_secret:
            row.encrypted_value = value
            row.value = None
        else:
            row.value = value
            row.encrypted_value = None
        await self._session.flush()
        return value

    async def delete(self, key: str) -> None:
        """Remove ``key`` if present; a no-op when it was never set.

        Used to invalidate a DERIVED/cached setting whose source of truth just
        changed -- e.g. the Plex ``machineIdentifier`` snapshot
        (:data:`PLEX_MACHINE_ID_SETTING`) cached at setup, which must be dropped
        when an admin repoints the app at a different server (new ``plex_url`` /
        ``plex_token``) so the next sign-in re-derives it from ``/identity``
        rather than trusting the OLD server's id. Idempotent: deleting an unset
        key does nothing (never a crash), matching :meth:`set`'s upsert symmetry.
        """
        row = await self._row(key)
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()

    async def redacted(self) -> dict[str, str | None]:
        """Return ``{key: value}`` with secret values masked to ``"***"``.

        A configured secret reports ``"***"``; an unset one reports ``None``. The
        plaintext secret is never returned.
        """
        out: dict[str, str | None] = {}
        for key in KNOWN_SETTING_KEYS:
            row = await self._row(key)
            if row is None:
                out[key] = None
            elif key in SECRET_SETTING_KEYS:
                out[key] = SECRET_MASK if row.encrypted_value is not None else None
            else:
                out[key] = row.value
        return out


# --------------------------------------------------------------------------- #
# Shared HTTP client
# --------------------------------------------------------------------------- #
def get_http_client(request: Request) -> httpx.AsyncClient:
    """Return the process-wide ``httpx.AsyncClient`` created by the app lifespan.

    Tests override this dependency with a ``MockTransport``-backed client so no
    live network is touched in the gate.
    """
    client = getattr(request.app.state, "http_client", None)
    if not isinstance(client, httpx.AsyncClient):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="http_client_unavailable",
        )
    return client


def get_health_cache(request: Request) -> TtlCache[SubsystemHealth]:
    """Return the process-wide subsystem-probe TTL cache (ADR-0012).

    Lazily created on first access and stashed on ``app.state`` so every
    subsequent request in this process reuses the SAME cache instance (the
    whole point — see ``health_service.SUBSYSTEM_PROBE_TTL_SECONDS``'s docstring
    on why a per-request cache would never actually deduplicate anything).
    Unlike ``reconcile_status``/``log_handler``, this is NOT created up front by
    ``lifespan``: nothing outside ``GET /api/v1/ops/health`` ever reads or
    mutates it, so it is a pure web-layer concern the dependency itself owns —
    mirrors ``get_http_client``'s own ``app.state`` lookup/lazy-init shape.
    """
    cache = getattr(request.app.state, "health_cache", None)
    if not isinstance(cache, TtlCache):
        cache = TtlCache[SubsystemHealth]()
        request.app.state.health_cache = cache
    # ``isinstance`` against a generic runtime class can't narrow the type
    # parameter (pyright sees ``TtlCache[Unknown]``); the cast is safe because
    # this accessor is the ONLY place anything ever assigns ``app.state.
    # health_cache``, always with this exact type.
    return cast("TtlCache[SubsystemHealth]", cache)


def get_log_handler(request: Request) -> log_capture_service.LogCaptureHandler:
    """Return the process-wide :class:`LogCaptureHandler` ``lifespan`` created
    (the ring-buffer + ``dropped_count`` source for ``GET /api/v1/ops/logs/tail``).

    Mirrors :func:`get_http_client`'s ``app.state`` lookup and honest 503 when
    absent -- a real deployment always has one (``lifespan`` sets it up before
    serving traffic), so a missing handler here means logging genuinely was
    never configured (e.g. a test driving the router without going through
    ``lifespan`` and without setting one up itself), not a value worth
    fabricating a placeholder for.
    """
    handler = getattr(request.app.state, "log_handler", None)
    if not isinstance(handler, log_capture_service.LogCaptureHandler):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="log_handler_unavailable",
        )
    return handler


def get_reconcile_status(request: Request) -> ReconcileStatus:
    """Return ``app.state.reconcile_status`` for a read-only HTTP response
    (``GET /api/v1/ops/health``'s reconcile panel).

    Lazily creates + stores a fresh, never-run :class:`ReconcileStatus` if
    absent so the health endpoint stays honest ("the loop hasn't run yet")
    rather than 503ing -- a real deployment always has one by the time it
    serves traffic (``lifespan`` sets it up front, mutated in place by
    ``_reconcile_once``/``_reconcile_loop`` in ``web/app.py``); this lazy path
    only matters for a test exercising the router without going through
    ``lifespan``.
    """
    current = getattr(request.app.state, "reconcile_status", None)
    if not isinstance(current, ReconcileStatus):
        current = ReconcileStatus()
        request.app.state.reconcile_status = current
    return current


def get_autograb_status(request: Request) -> AutograbStatus:
    """Return ``app.state.autograb_status`` for a read-only HTTP response
    (``GET /api/v1/ops/health``'s auto-grab panel, ADR-0013).

    The exact mirror of :func:`get_reconcile_status`: lazily creates + stores a
    fresh, never-run :class:`AutograbStatus` if absent so the health endpoint stays
    honest ("the loop hasn't run yet") rather than 503ing -- a real deployment
    always has one by the time it serves traffic (``lifespan`` sets it up front,
    mutated in place by ``_autograb_once``/``_autograb_loop`` in ``web/app.py``);
    this lazy path only matters for a test exercising the router without going
    through ``lifespan``.
    """
    current = getattr(request.app.state, "autograb_status", None)
    if not isinstance(current, AutograbStatus):
        current = AutograbStatus()
        request.app.state.autograb_status = current
    return current


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def api_key_matches(provided: str | None, expected: str | None) -> bool:
    """Constant-time check of the incoming header against the stored key.

    ``expected`` is the decrypted ``SystemSettings.app_api_key`` (the column is
    Fernet-encrypted at rest). ``hmac.compare_digest`` keeps the comparison
    timing-safe. A missing header or an uninitialised install (no stored key)
    never matches.

    The values are compared as UTF-8 BYTES: ``hmac.compare_digest`` raises
    ``TypeError`` on a ``str`` containing non-ASCII characters, so a malformed
    header would otherwise surface as an unhandled 500 instead of an honest 401.
    Encoding both sides keeps the comparison constant-time and total.
    """
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _configured_setup_token() -> str | None:
    token = get_settings().setup_token
    if token is None:
        return None
    value = token.get_secret_value().strip()
    return value or None


def is_setup_token_required(request: Request | None = None) -> bool:
    """Whether this request requires ``X-Setup-Token`` before initialization.

    True only when a token is BOTH enforced (``dev_auth_bypass`` off) AND actually
    configured. A server with bypass off but no ``setup_token`` set has nothing to
    validate a submitted token against, so advertising the requirement would render
    a setup-token field that can never succeed -- a dead end (north-star #1). This
    only reports the advisory status; it does not itself gate the setup routes.
    """
    _ = request
    settings = get_settings()
    return not settings.dev_auth_bypass and _configured_setup_token() is not None


def _pre_init_setup_token_valid(request: Request) -> bool:
    expected_setup_token = _configured_setup_token()
    provided_setup_token = request.headers.get(SETUP_TOKEN_HEADER_NAME)
    return api_key_matches(provided_setup_token, expected_setup_token)


def enforce_pre_init_setup_token(request: Request, *, initialized: bool) -> None:
    """Enforce the OPTIONAL pre-init hardening token (``PLEX_MANAGER_SETUP_TOKEN``).

    A no-op post-init, when no token is configured, or under ``dev_auth_bypass``
    (:func:`is_setup_token_required` already folds the bypass in). While the install
    is still uninitialized AND a token is configured, the request MUST carry a
    matching ``X-Setup-Token`` — else an honest 401 ``invalid_setup_token``.

    This is the SINGLE pre-init token gate, shared by :func:`require_setup_admin`
    (the setup sub-API) and the sign-in endpoint (``POST /api/v1/auth/plex``). The
    sign-in claim is the FIRST step of first-run setup, so gating it here is what
    makes the token actually harden the exclusive first-owner claim — not merely
    ``/complete``. Without it an attacker owning any Plex server could win the claim
    and lock out the true owner (recoverable only by DB surgery, a north-star-#1
    violation).
    """
    if not initialized and is_setup_token_required() and not _pre_init_setup_token_valid(request):
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_setup_token",
            message="The setup token is missing or wrong.",
            hint="Check PLEX_MANAGER_SETUP_TOKEN on the server.",
        )


def hash_session_token(token: str) -> str:
    """Return the stored digest for a random browser-session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_unsafe_method(method: str) -> bool:
    return method.upper() not in {"GET", "HEAD", "OPTIONS", "TRACE"}


def _require_csrf_for_session(request: Request) -> None:
    if not _is_unsafe_method(request.method):
        return
    header = request.headers.get(CSRF_HEADER_NAME)
    cookie = request.cookies.get(CSRF_COOKIE_NAME)
    if (
        not header
        or not cookie
        or not hmac.compare_digest(header.encode("utf-8"), cookie.encode("utf-8"))
    ):
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="csrf_token_required",
            message="The request was blocked by CSRF protection.",
            hint="Refresh the page and try again.",
        )


def _normalize_dt(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def _session_auth_context(
    request: Request,
    session: AsyncSession,
    *,
    enforce_csrf: bool,
) -> AuthContext | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    token_hash = hash_session_token(token)
    now = datetime.now(UTC)
    result = await session.execute(
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(
            AuthSession.token_hash == token_hash,
            AuthSession.revoked_at.is_(None),
        )
    )
    row = result.first()
    if row is None:
        return None
    auth_session, user = row
    expires_at = _normalize_dt(auth_session.expires_at)
    if expires_at <= now:
        return None
    if enforce_csrf:
        _require_csrf_for_session(request)
    return AuthContext(
        method=AuthMethod.plex_session,
        user_id=user.id,
        plex_id=user.plex_id,
        username=user.username,
        email=user.email,
        avatar_url=user.avatar_url,
        is_admin=user.permissions > 0,
        session_expires_at=expires_at,
    )


async def authenticate_request(
    request: Request,
    session: AsyncSession,
    *,
    provided_api_key: str | None = None,
    enforce_csrf: bool = True,
) -> AuthContext | None:
    """Return request auth context, accepting API key or Plex session cookie.

    The legacy app API key remains a valid recovery/automation credential. Browser
    sessions are checked only after the key path fails, so API-key callers are not
    subject to CSRF enforcement.
    """
    if get_settings().dev_auth_bypass:
        return AuthContext(method=AuthMethod.dev_bypass, is_admin=True)
    system = await load_system_settings(session)
    expected = system.app_api_key if system is not None else None
    if api_key_matches(provided_api_key, expected):
        return AuthContext(method=AuthMethod.api_key, is_admin=True)
    return await _session_auth_context(request, session, enforce_csrf=enforce_csrf)


async def require_api_key(
    provided: Annotated[str | None, Depends(_api_key_header)],
    session: Annotated[AsyncSession, Depends(get_session)],
    request: Request,
) -> AuthContext:
    """Enforce app authentication.

    The header source is :class:`APIKeyHeader`, so the security scheme + per-route
    requirement appear in the exported OpenAPI (generated clients then send the
    key). The stored key is Fernet-encrypted at rest; the incoming value is
    constant-time-compared (``hmac.compare_digest``) against the decrypted value.
    A valid Plex session cookie is accepted as the normal browser auth path.
    Skipped entirely when ``settings.dev_auth_bypass`` is set (dev only).
    """
    context = await authenticate_request(request, session, provided_api_key=provided)
    if context is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_api_key")
    return context


async def require_api_key_short_session(
    request: Request,
    provided: Annotated[str | None, Depends(_api_key_header)],
) -> AuthContext:
    """Auth for long-lived streaming endpoints that must hold no DB session.

    :func:`require_api_key` validates against a session sourced from
    :func:`get_session` — a *yield* dependency that stays checked out for the
    whole request lifetime. For an ordinary request that is momentary, but for an
    SSE stream the "request" lives as long as the browser tab, so the session (and
    its connection) would be pinned for hours. With the small aiosqlite pool
    (~5 slots) shared with the reconcile/autograb/eviction workers, a handful of
    long-lived tabs would exhaust it.

    This accepts the same API-key-or-Plex-session contract as
    :func:`require_api_key`, but validates it against a session that is opened and
    **closed up front**, before the endpoint begins streaming. The stream itself
    therefore holds no connection. The header source remains
    :class:`APIKeyHeader`, so the OpenAPI security scheme is unchanged and the app
    factory can still advertise cookie auth as the alternative browser path.
    """
    maker_obj = getattr(request.app.state, "sessionmaker", None)
    maker: async_sessionmaker[AsyncSession]
    if isinstance(maker_obj, async_sessionmaker):
        maker = cast("async_sessionmaker[AsyncSession]", maker_obj)
    else:
        maker = get_sessionmaker()
    async with maker() as session:
        context = await authenticate_request(
            request,
            session,
            provided_api_key=provided,
            enforce_csrf=False,
        )
    if context is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_api_key")
    return context


async def require_admin(
    context: Annotated[AuthContext, Depends(require_api_key)],
) -> AuthContext:
    """Require an app administrator.

    API-key and dev-bypass auth are administrator contexts. Plex session auth is
    administrator-only when the signed-in Plex account owns the configured server.
    """
    if not context.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    return context


async def require_admin_short_session(
    context: Annotated[AuthContext, Depends(require_api_key_short_session)],
) -> AuthContext:
    """Require an administrator without retaining a DB session.

    Long-lived streams use this companion to :func:`require_admin`: credential
    validation completes against a short-lived session before the response
    starts, and shared Plex users stay on the privacy-safe polling path.
    """
    if not context.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    return context


async def require_setup_admin(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthContext:
    """Gate every setup endpoint except ``/status`` on an authenticated admin.

    The order matters and each failure is an honest :class:`AppError` envelope
    (north star #3), never a bare ``detail``:

    1. ``dev_auth_bypass`` short-circuits to a dev admin context (dev only).
    2. While the install is still uninitialized AND an operator configured a
       hardening ``PLEX_MANAGER_SETUP_TOKEN`` (:func:`is_setup_token_required`),
       the request must carry a matching ``X-Setup-Token`` — a valid token falls
       THROUGH to the auth check below, it is not itself a credential. Post-init
       the token is never consulted again.
    3. Normal auth: a Plex session cookie (CSRF-checked on unsafe methods) or the
       legacy ``X-Api-Key``. No credential ⇒ 401 ``session_required`` — the prose
       nudges toward Plex sign-in while setup is unfinished, plain sign-in after.
    4. A non-admin (a signed-in Plex account that does not own the server) ⇒ 403
       ``admin_required``.
    """
    if get_settings().dev_auth_bypass:
        return AuthContext(method=AuthMethod.dev_bypass, is_admin=True)
    system = await load_system_settings(session)
    initialized = system is not None and system.initialized
    enforce_pre_init_setup_token(request, initialized=initialized)
    context = await authenticate_request(
        request,
        session,
        provided_api_key=request.headers.get(API_KEY_HEADER_NAME),
    )
    if context is None:
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="session_required",
            message=(
                "Sign in to continue." if initialized else "Sign in with Plex to continue setup."
            ),
        )
    if not context.is_admin:
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="admin_required",
            message="This action needs an administrator.",
        )
    return context


# --------------------------------------------------------------------------- #
# Adapter factories (decrypt creds + share the AsyncClient)
# --------------------------------------------------------------------------- #
async def get_tmdb(
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> MetadataPort:
    """Build a configured :class:`MetadataPort` (TMDB), or 409 if unconfigured."""
    api_key = await SettingsStore(session).get("tmdb_api_key")
    if not api_key:
        raise ServiceNotConfiguredError("tmdb")
    return TmdbMetadata(client, api_key)


async def get_prowlarr(
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> IndexerPort:
    """Build a configured :class:`IndexerPort` (Prowlarr), or 409 if unconfigured."""
    store = SettingsStore(session)
    url = await store.get("prowlarr_url")
    api_key = await store.get("prowlarr_api_key")
    if not url or not api_key:
        raise ServiceNotConfiguredError("prowlarr")
    return ProwlarrIndexer(client, url, api_key)


@dataclass(frozen=True)
class _CachedQbittorrent:
    """One cached :class:`QbittorrentClient` plus the key (settings + the
    ``httpx.AsyncClient`` it was built against) it was built from, so either a
    settings change OR an ASGI lifespan restart invalidates the cache instead
    of silently reusing stale credentials / a closed HTTP client."""

    key: tuple[str, str, str, str | None, httpx.AsyncClient]
    client: QbittorrentClient


async def resolve_qbittorrent(
    state: State,
    session: AsyncSession,
    client: httpx.AsyncClient,
) -> DownloadClientPort:
    """Build (or reuse) the configured qBittorrent adapter, else raise 409.

    Caches ONE :class:`QbittorrentClient` on ``state.qbittorrent_client``, keyed
    on the effective qbt settings tuple (url/username/password/prowlarr_url)
    PLUS the ``httpx.AsyncClient`` instance passed in this call (Codex P2: an
    ASGI lifespan shutdown/startup on the SAME ``FastAPI`` app object -- common
    in tests, and possible for a real process that re-enters lifespan -- closes
    ``app.state.http_client`` and replaces it with a fresh instance, but
    ``app.state.qbittorrent_client`` is a SEPARATE attribute that survives that
    swap untouched. A cache keyed on settings alone would then keep returning a
    ``QbittorrentClient`` still wrapping the CLOSED old ``httpx.AsyncClient``,
    so every qbt call would fail (``RuntimeError: client has been closed``)
    until settings changed or the process restarted. Including ``client`` in
    the key -- compared by identity, since ``httpx.AsyncClient`` defines no
    ``__eq__`` -- makes a lifespan restart behave exactly like a settings
    change: the key no longer matches, so the next resolve rebuilds against the
    NEW client. This also handles repeated restarts cleanly: each restart's
    client is a distinct object, so each produces its own cache entry with no
    special-casing needed.

    Without this cache, every caller of :func:`get_qbittorrent` -- including the
    reconcile and auto-grab loops on their 15s ticks -- built a brand-new
    ``QbittorrentClient`` with ``_logged_in = False`` AND a null adapter-local
    ``_session_cookie``, so it re-``POST``ed ``/auth/login`` every cycle: pure
    waste that drowned the genuine-login INFO log. Since #177 the qBittorrent
    session cookie is held BY the adapter instance (never the process-wide
    ``httpx.AsyncClient`` jar, whose portless cookies could cross services), so a
    fresh instance loses the captured SID itself, not merely the ``_logged_in``
    flag -- making instance reuse the ONLY way the session survives across
    cycles. Reusing the instance keeps both ``_logged_in`` and the validated
    ``_session_cookie``; the adapter's own ``_request`` still re-logs-in
    transparently on a genuine 403, so that path is unaffected.

    Mirrors :func:`get_health_cache`'s lazy ``app.state`` accessor pattern. A
    settings change OR a new ``http`` client produces a different key, so the
    next resolve rebuilds a fresh client (which re-parses the ``ServiceUrl``
    from the new ``url`` and logs in again with the new credentials, over the
    new ``httpx.AsyncClient``) rather than reusing one whose ``ServiceUrl`` /
    creds / underlying HTTP client were built from a stale generation. The key
    captures the raw ``url`` string that feeds ``ServiceUrl.parse``, so any
    change to the parsed service target necessarily changes the key.

    Concurrency: the cached wrapper is shared across the reconcile loop, the
    autograb loop, and concurrent HTTP requests -- exactly as the underlying
    ``app.state.http_client`` already is. The only added shared mutable state
    is ``_logged_in`` (bool), ``_session_cookie`` (tuple | None),
    ``_properties_cache`` (dict), ``_stop_start`` (bool | None). asyncio is
    single-threaded (no mid-statement preemption); a rare concurrent double-login
    is idempotent (both POST valid creds, the SID cookie converges). Same sharing
    posture as ``reconcile_status``/``health_cache``. No lock is warranted.
    """
    store = SettingsStore(session)
    url = await store.get("qbittorrent_url")
    username = await store.get("qbittorrent_username")
    password = await store.get("qbittorrent_password")
    if not url or not username or password is None:
        raise ServiceNotConfiguredError("qbittorrent")
    # The operator-configured Prowlarr endpoint is the ONE origin the torrent-
    # source safe-fetch may follow to a private address: Prowlarr serves
    # magnetless .torrent downloadUrls pointing at itself, and self-hosted
    # Prowlarr is typically on 127.0.0.1 / RFC1918 / a compose alias the SSRF
    # veto would otherwise reject — making every magnetless private-tracker
    # release ungrabbable. The app already trusts this exact URL with an API key
    # for every search call. None (unconfigured) keeps the veto fully closed.
    prowlarr_url = await store.get("prowlarr_url")
    # ``client`` is compared by identity (``httpx.AsyncClient`` defines no
    # ``__eq__``, so tuple ``==`` falls back to ``is``) -- an ASGI lifespan
    # restart swaps in a NEW client object, which changes this key even though
    # every settings value is unchanged, forcing a rebuild against the live
    # client rather than reusing one bound to the closed old one.
    key = (url, username, password, prowlarr_url or None, client)
    cached = getattr(state, "qbittorrent_client", None)
    if isinstance(cached, _CachedQbittorrent) and cached.key == key:
        return cached.client
    qbt = QbittorrentClient(
        client, url, username, password, trusted_source_origin=prowlarr_url or None
    )
    state.qbittorrent_client = _CachedQbittorrent(key, qbt)
    return qbt


async def get_qbittorrent(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> DownloadClientPort:
    """Build (or reuse the cached) configured :class:`DownloadClientPort`
    (qBittorrent), else 409. See :func:`resolve_qbittorrent`."""
    return await resolve_qbittorrent(request.app.state, session, client)


async def get_qbittorrent_optional(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> DownloadClientPort | None:
    """Like :func:`get_qbittorrent`, but ``None`` when qBittorrent is unconfigured.

    For an endpoint that only CONDITIONALLY needs the client -- the mark-failed move
    with ``remove_torrent=false`` runs a pure DB fail/blocklist/re-arm and never
    touches qBittorrent, so requiring the (eager, dependency-resolved)
    :func:`get_qbittorrent` there would 409 ``service_not_configured`` on an install
    without qBittorrent even though the caller opted out of removal. The endpoint
    re-imposes the honest 409 itself when removal IS requested but the client is
    unconfigured (never a silent skip).
    """
    try:
        return await resolve_qbittorrent(request.app.state, session, client)
    except ServiceNotConfiguredError:
        return None


def get_downloads_host_root() -> str:
    """Resolve the HOST-namespace downloads root ``grab()`` should direct
    qBittorrent's ``save_path`` to (issues #133/#157).

    ``Settings.downloads_root`` (``PLEX_MANAGER_DOWNLOADS_ROOT`` -- the SAME
    variable docker-compose already uses as the ``/downloads`` bind source;
    ``env_file: .env`` hands it to this container too) is the ONLY source (see
    :func:`~plex_manager.services.path_visibility.resolve_downloads_host_root` for
    why there is no ``/proc/self/mountinfo`` fallback: that field cannot recover a
    host-namespace path). ``""`` (never ``None`` -- callers thread this straight
    into ``grab()``'s ``save_path: str`` parameter) when unset (bare metal, no
    Docker split): qBittorrent's own default is then left in charge, unchanged
    prior behaviour, never a guessed path.

    A plain function, not async: the settings read is a cheap synchronous env
    lookup, the same "no ``asyncio.to_thread`` needed" precedent
    ``setup_validation``'s own synchronous filesystem probes already set.
    """
    settings = get_settings()
    return path_visibility.resolve_downloads_host_root(settings.downloads_root) or ""


def get_filesystem() -> FileSystemPort:
    """Return the local filesystem adapter (no credentials needed).

    Constructed with NO ``library_roots`` — every existing caller only ever uses
    ``move``/``hardlink_or_copy``/``largest_video_file``/``list_video_files``,
    none of which consult the root guard. ``delete()`` (ADR-0012's disk-pressure
    eviction) refuses every path on an instance built this way BY DESIGN
    (fail-closed, never open) — eviction must use
    :func:`get_eviction_filesystem` instead, never this one.
    """
    return LocalFileSystem()


def get_media_probe() -> MediaProbePort:
    """Return the bounded ffprobe adapter used before library placement."""
    return FfprobeMediaProbe()


def get_eviction_filesystem(
    movies_root: str | None,
    tv_root: str | None,
    anime_movie_root: str | None = None,
    anime_tv_root: str | None = None,
) -> FileSystemPort:
    """Build the ONLY :class:`FileSystemPort` instance eviction's ``delete()`` may
    ever be handed (ADR-0012): scoped to whichever of the library roots are
    actually configured, so the containment guard has something real to check
    against. :func:`get_filesystem`'s default instance has NO roots and refuses
    everything — using it here would make every eviction sweep a silent no-op.

    Takes plain values (not other ``Depends``) so it composes identically from
    the periodic eviction loop (``web/app.py``), the manual
    ``POST /api/v1/ops/evict`` trigger, and the report-issue purge path, all of
    which already resolve ``movies_root``/``tv_root``/``anime_movie_root``/
    ``anime_tv_root`` via the matching ``get_*_root_optional`` dependencies.

    The anime roots (ADR-0015) default to ``None`` so no existing caller breaks
    at import time; every real call site passes them explicitly. Without them
    here, an anime title's ``library_path`` sits outside every configured root
    and the delete-guard refuses it — a report-issue purge would silently leave
    the bad file on disk after blocklisting and re-searching.
    """
    roots = [root for root in (movies_root, tv_root, anime_movie_root, anime_tv_root) if root]
    return LocalFileSystem(library_roots=roots)


async def get_library(
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> LibraryPort:
    """Build a configured :class:`LibraryPort` (Plex), or 409 if unconfigured."""
    store = SettingsStore(session)
    url = await store.get("plex_url")
    token = await store.get("plex_token")
    if not url or not token:
        raise ServiceNotConfiguredError("plex")
    return PlexLibrary(client, url, token)


async def get_library_optional(
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> LibraryPort | None:
    """Like :func:`get_library`, but ``None`` when Plex is unconfigured.

    Request-time availability dedupe degrades gracefully: an install without Plex
    configured still creates requests (never a 409 on the request path), just
    without the in-library short-circuit.
    """
    try:
        return await get_library(session, client)
    except ServiceNotConfiguredError:
        return None


def _blank_to_none(value: str | None) -> str | None:
    """Normalize an unset OR whitespace-only stored root value to ``None`` (issue #83).

    A root setting is free-text and may be present-but-whitespace (e.g. an
    operator submitting a stray space through ``PUT /settings``, which has no
    validator stripping it, unlike ``POST /setup/complete``'s
    ``SetupCompleteRequest``). Such a value is truthy in Python, so a plain
    ``or None``/``if not root`` check -- as every ``get_*_root*`` function below
    used to do -- lets it sail through as if it were a real, configured root:
    downstream code would then resolve a relative whitespace path against the
    process CWD instead of tripping the honest "unset" refusal it's meant to.
    This is the ONE place every root read goes through, so the strip lives here
    rather than scattered across each of the six getters below.

    WHITESPACE-ONLY DETECTION ONLY: a stripped-empty value becomes ``None``,
    but any non-blank value is returned byte-identical to what was stored --
    NEVER ``.strip()``-ed. A previous version returned the stripped value for
    the non-blank case too, which silently retargeted import/scan/evict to a
    different path than the one ``GET /settings`` displays whenever a stored
    root carried incidental leading/trailing padding (e.g. ``"  /media/x  "``):
    the operator sees one path, but every filesystem operation resolves
    another. Settings display and filesystem behavior must always agree.
    """
    if value is None:
        return None
    if not value.strip():
        return None
    return value


async def get_movies_root(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str:
    """Return the configured Movies library root, or 409 if unset."""
    root = _blank_to_none(await SettingsStore(session).get("movies_root"))
    if root is None:
        raise ServiceNotConfiguredError("movies_root")
    return root


async def get_movies_root_optional(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str | None:
    """Return the Movies root, or ``None`` when unset (the importer waits, no crash).

    Normalizes a falsy OR whitespace-only stored value to ``None`` (see
    :func:`_blank_to_none`) so callers can use a single ``is None`` check,
    matching :func:`get_movies_root`'s treatment of "unset". Without this, an
    empty-string or whitespace-only root would sail past an ``is None`` guard
    downstream and silently resolve relative paths against the process CWD
    instead of tripping the honest ``ImportBlocked`` it's meant to.
    """
    return _blank_to_none(await SettingsStore(session).get("movies_root"))


async def get_tv_root(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str:
    """Return the configured TV library root, or 409 if unset.

    Mirrors :func:`get_movies_root`; used by a route that genuinely cannot proceed
    at all without a TV root (there is none of those yet in the beta -- the import
    endpoints use :func:`get_tv_root_optional` instead so a per-row honest block
    replaces an upfront 409 -- but this is kept as the required counterpart for
    symmetry with the movies-side dependency and any future all-or-nothing route).
    """
    root = _blank_to_none(await SettingsStore(session).get("tv_root"))
    if root is None:
        raise ServiceNotConfiguredError("tv_root")
    return root


async def get_tv_root_optional(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str | None:
    """Return the TV root, or ``None`` when unset (the importer surfaces an honest,
    per-row ``ImportBlocked`` for a tv download instead of a crash or an upfront
    409 that would also block importing movies on an install with no TV root).

    Mirrors :func:`get_movies_root_optional`'s falsy-or-whitespace-to-``None``
    normalization: an empty-string OR whitespace-only setting is "unset", not a
    valid root, so downstream ``is None`` guards must see it as such.
    """
    return _blank_to_none(await SettingsStore(session).get("tv_root"))


async def get_anime_movie_root_optional(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str | None:
    """Return the anime-movies library root, or ``None`` when unset (ADR-0015).

    Mirrors :func:`get_movies_root_optional`'s falsy-or-whitespace-to-``None``
    normalization. Unset is the common case — importing then routes an anime
    movie to the normal ``movies_root`` exactly as before this setting existed.
    """
    return _blank_to_none(await SettingsStore(session).get("anime_movie_root"))


async def get_anime_tv_root_optional(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str | None:
    """Return the anime-TV library root, or ``None`` when unset (ADR-0015).

    Mirrors :func:`get_tv_root_optional`'s falsy-or-whitespace-to-``None``
    normalization. Unset is the common case — importing then routes an anime
    episode to the normal ``tv_root`` exactly as before this setting existed.
    """
    return _blank_to_none(await SettingsStore(session).get("anime_tv_root"))


# --------------------------------------------------------------------------- #
# Pure-domain dependencies (no I/O, but injected so tests can swap them)
# --------------------------------------------------------------------------- #
def get_parser() -> ParserPort:
    """Return the release-name parser (guessit adapter, confined to its module)."""
    return GuessitParser()


def get_quality_profile() -> QualityProfile:
    """Return the alpha's hardcoded default quality profile (read-only)."""
    return default_profile()


# --------------------------------------------------------------------------- #
# Operability beta (ADR-0012): typed, web-editable numeric/boolean settings
# --------------------------------------------------------------------------- #
# Safe defaults per the blueprint (``docs/design/operability-beta-plan.md``).
# Exported so the services layer / tests can reference the SAME numbers a fresh
# install effectively runs with, without duplicating the literal.
DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT: float = 90.0
DISK_PRESSURE_TARGET_PERCENT_DEFAULT: float = 80.0
EVICTION_GRACE_DAYS_DEFAULT: int = 30
EVICTION_ENABLED_DEFAULT: bool = True
EVICTION_PROACTIVE_ENABLED_DEFAULT: bool = False
EVICTION_INTERVAL_MINUTES_DEFAULT: float = 30.0
LOG_RETENTION_DAYS_DEFAULT: int = 7
# The row-count companion to LOG_RETENTION_DAYS_DEFAULT (issue #152): bounds
# ``log_events`` growth even under a chatty install with a generous retention
# window, where age-based pruning alone would never trip. 100,000 rows is
# generous for a single beta-week install's diagnostic trail while staying
# comfortably small for SQLite.
LOG_MAX_ROWS_DEFAULT: int = 100_000
# Auto-grab worker (ADR-0013): the background request->search->grab loop runs by
# default; an operator can turn it off from Settings without touching a terminal.
AUTO_GRAB_ENABLED_DEFAULT: bool = True

# Upper bounds for the three settings above that feed directly into a sleep
# duration or a timedelta cutoff (issue #92) live in ``web.settings_bounds``,
# a dependency-free leaf module both this module AND ``web.schemas`` import --
# see that module's docstring for why (a real, verified circular import if
# ``web.schemas`` imported these from here instead). Re-exported via
# ``__all__`` above so callers/tests reach them the same way as every other
# operability constant, via ``web.deps``.

# Parses a stored boolean setting. ``TypeAdapter(bool)`` accepts the
# case-insensitive token set pydantic recognizes (``true``/``false``/``1``/``0``/
# ``yes``/``no``/``on``/``off``/``t``/``f``/``y``/``n``); the plain-string
# ``settings.value`` column has no dedicated boolean type, so this string parse
# (inside :func:`resolve_bool_setting`, which also strips surrounding whitespace)
# is the single boolean gate shared by the runtime getters below AND ``GET
# /settings``'s ``_sanitize_typed_settings`` -- one rule, so the page and the
# loops can never disagree on which stored string is a valid bool.
_BOOL_SETTING_ADAPTER: TypeAdapter[bool] = TypeAdapter(bool)


# --------------------------------------------------------------------------- #
# Shared raw-value resolvers (PR #142): the ONE place a stored typed setting's
# raw string becomes the EFFECTIVE runtime value. Used by BOTH the typed
# getters below (what the eviction / auto-grab / log-retention loops read) and
# ``GET /settings``'s ``_sanitize_typed_settings`` (what the page presents), so
# the two can never disagree on what a corrupt stored value means -- the page
# must never claim a state the running service isn't in (north star #3).
#
# Directional degradation policy, chosen per what is SAFER for existing data
# when a stored value violates a bound (upgrade compatibility: a value that
# predates the bounds, or a hand-edit, must never silently become MORE
# destructive than what it meant when it was written):
#
# * ABOVE an upper bound -> CLAMP to the bound. A pre-bounds huge
#   ``eviction_grace_days`` / ``log_retention_days`` was a legitimate way to
#   effectively disable age-based eviction / log expiry; substituting the
#   default (30 / 7 days) on upgrade would suddenly evict month-old titles or
#   delete week-old logs. Same for ``eviction_interval_minutes`` (the MAX
#   already guarantees a weekly wake-up) and the disk-pressure percents (a
#   stored >100 threshold meant "never trip"; clamping to 100 = only-when-full
#   is the closest safe meaning, where the default 90 would START evicting at
#   90% used on upgrade).
# * BELOW a lower bound -> DEFAULT, never a floor-clamp. Every floor here is
#   the DESTRUCTIVE end of the scale: grace/retention 0 = evict/expire
#   immediately, threshold 0 = permanently "under pressure", a sub-zero target
#   = evict everything, interval <= 0 = a hot-spinning loop. The default is
#   the safe, tested value; clamping to the floor would maximize the damage.
# * Unparsable / non-finite -> DEFAULT (nothing to clamp toward).
#
# Every resolver returns ``(effective, honored)``: ``honored`` is True only
# when the effective value IS the raw stored value (so the GET sanitizer can
# echo the raw string verbatim); every degradation is logged at WARNING naming
# the key, never silent.
def _parse_finite_float(raw: str) -> float | None:
    """``float(raw)`` narrowed to finite, or ``None`` (unparsable / ``inf`` / ``nan``).

    ``float()`` happily parses ``"inf"``/``"nan"`` without raising -- a stored
    non-finite value would otherwise sail through and hang whichever loop feeds
    it into ``asyncio.sleep`` (issue #92) -- so non-finite folds into the same
    ``None`` (= degrade to default) a genuinely unparsable string gets.
    """
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _parse_int(raw: str) -> int | None:
    """``int(raw)``, or ``None`` when unparsable."""
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_disk_pressure_percent(key: str, raw: str | None, default: float) -> tuple[float, bool]:
    """Individually resolve ONE disk-pressure percent to ``[0, 100]`` (no pair logic)."""
    if raw is None:
        return default, True
    parsed = _parse_finite_float(raw)
    if parsed is None:
        _logger.warning(
            "setting %r has an unparsable or non-finite value %r; using default %s",
            key,
            raw,
            default,
        )
        return default, False
    if parsed > DISK_PRESSURE_PERCENT_MAX:
        _logger.warning(
            "setting %r is %s, above %s; clamping to %s",
            key,
            parsed,
            DISK_PRESSURE_PERCENT_MAX,
            DISK_PRESSURE_PERCENT_MAX,
        )
        return DISK_PRESSURE_PERCENT_MAX, False
    if parsed < DISK_PRESSURE_PERCENT_MIN:
        _logger.warning(
            "setting %r is %s, below %s; using default %s",
            key,
            parsed,
            DISK_PRESSURE_PERCENT_MIN,
            default,
        )
        return default, False
    return parsed, True


@dataclass(frozen=True)
class DiskPressurePercents:
    """The EFFECTIVE (post-resolution) disk-pressure pair the sweep runs with.

    ``threshold``/``target`` are always a WORKABLE pair (``target <= threshold``)
    -- see :func:`resolve_disk_pressure_percents`. Each ``*_honored`` is True only
    when that side's effective value is exactly the raw stored value (used by the
    ``GET /settings`` sanitizer to decide raw-echo vs effective-value display).
    """

    threshold: float
    target: float
    threshold_honored: bool
    target_honored: bool


def resolve_disk_pressure_percents(
    raw_threshold: str | None, raw_target: str | None
) -> DiskPressurePercents:
    """Resolve the stored disk-pressure pair to the effective, WORKABLE pair.

    Each side is first resolved individually (clamp above 100, default below 0
    or on garbage -- see the policy comment above). Then the PAIR invariant is
    re-validated: per-side substitution can manufacture an inverted pair from a
    half-corrupt store -- e.g. a valid stored ``threshold=50`` with a corrupt
    ``target=-1`` would substitute the default target 80, and ``(50, 80)`` makes
    ``select_evictions``'s ``projected <= target`` stop condition select NOTHING
    anywhere in the 50-80% used band: a sweep that trips but never relieves
    pressure, while the Settings form cannot even re-save the pair it displays.
    An inverted resolved pair therefore clamps the TARGET down to the resolved
    threshold (with a WARNING): the threshold -- WHEN eviction starts -- is the
    operator-meaningful side and is preserved, and ``target == threshold`` is
    the MINIMAL eviction consistent with it (never more eviction than any pair
    the operator could legitimately have stored with that threshold, and never
    a dead band). The threshold itself is never changed by the pair rule.
    """
    threshold, threshold_honored = _resolve_disk_pressure_percent(
        "disk_pressure_threshold_percent", raw_threshold, DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT
    )
    target, target_honored = _resolve_disk_pressure_percent(
        "disk_pressure_target_percent", raw_target, DISK_PRESSURE_TARGET_PERCENT_DEFAULT
    )
    if target > threshold:
        _logger.warning(
            "disk_pressure_target_percent resolved to %s, above disk_pressure_threshold_percent"
            " %s; using %s so the pressure sweep has a workable (non-inverted) pair",
            target,
            threshold,
            threshold,
        )
        target = threshold
        target_honored = False
    return DiskPressurePercents(
        threshold=threshold,
        target=target,
        threshold_honored=threshold_honored,
        target_honored=target_honored,
    )


def resolve_eviction_interval_minutes(raw: str | None) -> tuple[float, bool]:
    """Resolve the sweep interval to ``(0, EVICTION_INTERVAL_MAX_MINUTES]``.

    Above the MAX clamps to it: a pre-bounds huge interval meant "sweep almost
    never", the MAX already guarantees at least a weekly wake-up, and the
    30-minute default would make sweeps orders of magnitude more frequent on
    upgrade. At/below zero -- an EXCLUSIVE bound with no safe floor to clamp to
    (a zero/negative sleep hot-spins ``_eviction_loop``) -- and garbage fall
    back to the default.
    """
    if raw is None:
        return EVICTION_INTERVAL_MINUTES_DEFAULT, True
    parsed = _parse_finite_float(raw)
    if parsed is None:
        _logger.warning(
            "setting 'eviction_interval_minutes' has an unparsable or non-finite value %r;"
            " using default %s",
            raw,
            EVICTION_INTERVAL_MINUTES_DEFAULT,
        )
        return EVICTION_INTERVAL_MINUTES_DEFAULT, False
    if parsed > EVICTION_INTERVAL_MAX_MINUTES:
        _logger.warning(
            "setting 'eviction_interval_minutes' is %s, above %s; clamping to %s",
            parsed,
            EVICTION_INTERVAL_MAX_MINUTES,
            EVICTION_INTERVAL_MAX_MINUTES,
        )
        return EVICTION_INTERVAL_MAX_MINUTES, False
    if parsed <= 0:
        _logger.warning(
            "setting 'eviction_interval_minutes' is %s, not above 0; using default %s",
            parsed,
            EVICTION_INTERVAL_MINUTES_DEFAULT,
        )
        return EVICTION_INTERVAL_MINUTES_DEFAULT, False
    return parsed, True


def _resolve_bounded_count(
    key: str, raw: str | None, default: int, maximum: int
) -> tuple[int, bool]:
    """Resolve a non-negative integer setting to ``[0, maximum]`` (grace / log
    retention days, and the ``log_max_rows`` row cap -- none of these are a
    ``timedelta`` specifically, just a bounded non-negative count).

    Above ``maximum`` clamps to it: a pre-bounds huge value was a legitimate
    "effectively never evict / never expire / never cap" configuration, and
    substituting the default on upgrade would be data-destructive (a 30-day
    grace suddenly makes month-old titles evictable; a 7-day retention deletes
    logs the operator meant to keep). NEGATIVE values fall back to the default,
    never a clamp to 0 -- a 0 grace/retention/row-cap is the DESTRUCTIVE end of
    the scale (immediately evictable / nothing retained / nothing kept), the
    opposite of what a corrupt value should degrade to; for the two day-count
    callers, a negative value also pushes the ``timedelta`` cutoff into the
    future (over-evicting / wholesale log deletion), and a huge value overflows
    ``timedelta`` (its own limit is 999,999,999 days) -- the row-cap caller has
    no ``timedelta`` at all, but the same directional policy is still the
    honest choice for a corrupt/out-of-range stored count.
    """
    if raw is None:
        return default, True
    parsed = _parse_int(raw)
    if parsed is None:
        _logger.warning(
            "setting %r has an unparsable value %r; using default %s", key, raw, default
        )
        return default, False
    if parsed > maximum:
        _logger.warning("setting %r is %s, above %s; clamping to %s", key, parsed, maximum, maximum)
        return maximum, False
    if parsed < 0:
        _logger.warning("setting %r is %s, negative; using default %s", key, parsed, default)
        return default, False
    return parsed, True


def resolve_eviction_grace_days(raw: str | None) -> tuple[int, bool]:
    """Resolve ``eviction_grace_days`` -- policy in :func:`_resolve_bounded_count`."""
    return _resolve_bounded_count(
        "eviction_grace_days", raw, EVICTION_GRACE_DAYS_DEFAULT, EVICTION_GRACE_DAYS_MAX
    )


def resolve_log_retention_days(raw: str | None) -> tuple[int, bool]:
    """Resolve ``log_retention_days`` -- policy in :func:`_resolve_bounded_count`."""
    return _resolve_bounded_count(
        "log_retention_days", raw, LOG_RETENTION_DAYS_DEFAULT, LOG_RETENTION_DAYS_MAX
    )


def resolve_log_max_rows(raw: str | None) -> tuple[int, bool]:
    """Resolve ``log_max_rows`` (issue #152) -- policy in :func:`_resolve_bounded_count`.

    The retention sweep's ROW-COUNT companion to ``log_retention_days``'s AGE
    cutoff: age-based pruning alone leaves ``log_events`` unbounded in row count
    under a chatty install with a generous retention window. Same directional
    policy as the day-count settings: a stored value above the cap CLAMPS (a
    pre-bounds huge value meant "keep effectively everything"), a negative or
    unparsable one DEFAULTS (0 rows kept is the destructive end of the scale,
    never what a corrupt value should silently mean).
    """
    return _resolve_bounded_count("log_max_rows", raw, LOG_MAX_ROWS_DEFAULT, LOG_MAX_ROWS_MAX)


def resolve_bool_setting(key: str, raw: str | None, default: bool) -> tuple[bool, bool]:
    """Resolve a stored boolean: recognized token -> its value, else the default.

    The raw value is ``strip()``-ed first, preserving the pre-#142 parser's
    contract (it compared ``raw.strip().lower()``), so a persisted
    whitespace-padded ``" false "`` keeps meaning ``False`` on upgrade instead
    of becoming "unrecognized" and silently re-enabling a loop via the ``True``
    default. The recognized token set is pydantic's own case-insensitive
    coercion set (``true``/``false``/``1``/``0``/``yes``/``no``/``on``/``off``/
    ``t``/``f``/``y``/``n`` -- so ``"False"``/``"TRUE"`` work exactly as they
    did before). An UNRECOGNIZED value (e.g. ``"maybe"``) is NOT silently
    coerced to ``False`` -- it falls back to ``default`` with a WARNING naming
    the key, matching the numeric resolvers' corrupt-value posture, so the loop
    and the Settings page (which presents the same fallback) always agree
    (issue #92).
    """
    if raw is None:
        return default, True
    try:
        return _BOOL_SETTING_ADAPTER.validate_python(raw.strip()), True
    except ValidationError:
        _logger.warning(
            "setting %r has an unrecognized boolean value %r; using default %s", key, raw, default
        )
        return default, False


async def get_disk_pressure_threshold_percent(session: AsyncSession) -> float:
    """Used% at/above which a root's disk-pressure eviction sweep fires (default 90).

    Resolved through :func:`resolve_disk_pressure_percents` -- the SAME
    resolution ``GET /settings`` presents -- so a corrupt stored value degrades
    identically for the page and for ``_eviction_tick``: above 100 clamps to
    100 (a pre-bounds ``150`` meant "never trip"; the default 90 would START
    evicting at 90% on upgrade), below 0 / garbage falls back to the default,
    every degradation logged at WARNING. Reads both halves of the pair because
    resolution is pair-aware (see the resolver), though the threshold itself is
    never changed by the pair rule.
    """
    store = SettingsStore(session)
    resolved = resolve_disk_pressure_percents(
        await store.get("disk_pressure_threshold_percent"),
        await store.get("disk_pressure_target_percent"),
    )
    return resolved.threshold


async def get_disk_pressure_target_percent(session: AsyncSession) -> float:
    """Used% the sweep evicts stalest-first candidates down towards (default 80).

    Resolved through :func:`resolve_disk_pressure_percents` -- the SAME
    resolution ``GET /settings`` presents. Individually: above 100 clamps to
    100, below 0 / garbage falls back to the default. Then the PAIR rule
    applies: if the resolved target sits ABOVE the resolved threshold (a
    half-corrupt store after per-side substitution), it clamps down to the
    threshold so ``select_evictions`` always gets a workable, non-inverted
    pair -- see the resolver's docstring for why that beats substituting the
    default pair.
    """
    store = SettingsStore(session)
    resolved = resolve_disk_pressure_percents(
        await store.get("disk_pressure_threshold_percent"),
        await store.get("disk_pressure_target_percent"),
    )
    return resolved.target


async def get_eviction_grace_days(session: AsyncSession) -> int:
    """Minimum days since ``last_viewed_at`` before a watched title is evictable (default 30).

    Resolved through :func:`resolve_eviction_grace_days` (shared with the ``GET
    /settings`` sanitizer): a stored value ABOVE ``EVICTION_GRACE_DAYS_MAX``
    CLAMPS to the MAX rather than defaulting -- a pre-bounds huge value (e.g.
    ``3651``) was a legitimate way to effectively disable age-based eviction,
    and degrading it to the 30-day default on upgrade would suddenly make
    month-old titles eviction-eligible (data-destructive). A NEGATIVE value
    (which would push ``eviction_service``'s ``grace_cutoff`` into the FUTURE,
    over-evicting titles still within grace) and garbage fall back to the
    default. Every degradation is a WARNING naming the key.
    """
    value, _honored = resolve_eviction_grace_days(
        await SettingsStore(session).get("eviction_grace_days")
    )
    return value


async def get_eviction_enabled(session: AsyncSession) -> bool:
    """Whether the pressure-triggered eviction sweep may run at all (default true).

    Parsed by :func:`resolve_bool_setting` (shared with ``GET /settings``):
    whitespace-padded/case-variant ``true``/``false`` tokens are honored;
    an unrecognized stored value degrades to the default with a WARNING.
    """
    value, _honored = resolve_bool_setting(
        "eviction_enabled",
        await SettingsStore(session).get("eviction_enabled"),
        EVICTION_ENABLED_DEFAULT,
    )
    return value


async def get_eviction_proactive_enabled(session: AsyncSession) -> bool:
    """Whether past-grace watched+unpinned content evicts even without pressure (default false).

    Same :func:`resolve_bool_setting` parse as :func:`get_eviction_enabled`.
    """
    value, _honored = resolve_bool_setting(
        "eviction_proactive_enabled",
        await SettingsStore(session).get("eviction_proactive_enabled"),
        EVICTION_PROACTIVE_ENABLED_DEFAULT,
    )
    return value


async def get_eviction_interval_minutes(session: AsyncSession) -> float:
    """How often the eviction sweep's own periodic task runs (default 30 minutes).

    Resolved through :func:`resolve_eviction_interval_minutes` (shared with the
    ``GET /settings`` sanitizer): a finite-huge stored value (e.g. a hand-edited
    ``"999999"``) CLAMPS to ``EVICTION_INTERVAL_MAX_MINUTES`` -- the weekly
    wake-up the bound exists to guarantee -- rather than defaulting to a
    30-minute cadence the operator never asked for; non-positive, non-finite,
    and unparsable values fall back to the default (a zero/negative sleep would
    hot-spin ``_eviction_loop``). Every degradation is a WARNING naming the key.
    """
    value, _honored = resolve_eviction_interval_minutes(
        await SettingsStore(session).get("eviction_interval_minutes")
    )
    return value


async def get_log_retention_days(session: AsyncSession) -> int:
    """How many days of captured ``log_events`` rows the retention sweep keeps (default 7).

    Resolved through :func:`resolve_log_retention_days` (shared with the ``GET
    /settings`` sanitizer), with the same directional policy as
    :func:`get_eviction_grace_days`: above the MAX clamps (a pre-bounds huge
    value meant "keep everything" -- defaulting to 7 days would delete logs the
    operator meant to keep); negative (a future cutoff = wholesale log
    deletion) and garbage fall back to the default.
    """
    value, _honored = resolve_log_retention_days(
        await SettingsStore(session).get("log_retention_days")
    )
    return value


async def get_log_max_rows(session: AsyncSession) -> int:
    """Row-count cap the retention sweep prunes ``log_events`` down to (issue
    #152, default 100,000) -- the ROW-COUNT companion to
    :func:`get_log_retention_days`'s AGE cutoff, closing the gap where a
    chatty install with a generous retention window would otherwise grow the
    table unboundedly.

    Resolved through :func:`resolve_log_max_rows`, same directional policy as
    every other bounded-count setting: above the MAX clamps, negative/garbage
    falls back to the default.
    """
    value, _honored = resolve_log_max_rows(await SettingsStore(session).get("log_max_rows"))
    return value


async def get_auto_grab_enabled(session: AsyncSession) -> bool:
    """Whether the background auto-grab worker may run at all (default true, ADR-0013).

    Same :func:`resolve_bool_setting` parse as :func:`get_eviction_enabled`.
    """
    value, _honored = resolve_bool_setting(
        "auto_grab_enabled",
        await SettingsStore(session).get("auto_grab_enabled"),
        AUTO_GRAB_ENABLED_DEFAULT,
    )
    return value
