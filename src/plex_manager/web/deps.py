"""FastAPI dependencies: DB session, api-key auth, settings store, adapters.

Wiring rules:

* ``get_session`` reuses :func:`plex_manager.db.get_session`.
* ``require_api_key`` enforces the static ``X-Api-Key`` header against
  ``SystemSettings.app_api_key`` — which is Fernet-encrypted at rest, so the
  plaintext is never on disk; the header is constant-time-compared against the
  decrypted value. The header is sourced via ``APIKeyHeader`` so the security
  scheme appears in the OpenAPI. It is skipped when ``settings.dev_auth_bypass``
  is set. Health, setup and docs routes do NOT depend on it.
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

import hmac
import logging
from typing import Annotated, cast

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.adapters.filesystem.local import LocalFileSystem
from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.adapters.plex.library import PlexLibrary
from plex_manager.adapters.prowlarr.adapter import ProwlarrIndexer
from plex_manager.adapters.qbittorrent.adapter import QbittorrentClient
from plex_manager.adapters.tmdb.adapter import TmdbMetadata
from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.domain.quality_profile import QualityProfile, default_profile
from plex_manager.models import Setting, SystemSettings
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.filesystem import FileSystemPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.metadata import MetadataPort
from plex_manager.ports.parser import ParserPort
from plex_manager.services import log_capture_service
from plex_manager.services.health_service import ReconcileStatus, SubsystemHealth, TtlCache

__all__ = [
    "API_KEY_HEADER_NAME",
    "DISK_PRESSURE_TARGET_PERCENT_DEFAULT",
    "DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT",
    "EVICTION_ENABLED_DEFAULT",
    "EVICTION_GRACE_DAYS_DEFAULT",
    "EVICTION_INTERVAL_MINUTES_DEFAULT",
    "EVICTION_PROACTIVE_ENABLED_DEFAULT",
    "KNOWN_SETTING_KEYS",
    "LOG_RETENTION_DAYS_DEFAULT",
    "SECRET_MASK",
    "SECRET_SETTING_KEYS",
    "ServiceNotConfiguredError",
    "SettingsStore",
    "api_key_matches",
    "ensure_system_settings",
    "get_disk_pressure_target_percent",
    "get_disk_pressure_threshold_percent",
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
    "get_log_retention_days",
    "get_movies_root",
    "get_movies_root_optional",
    "get_parser",
    "get_prowlarr",
    "get_qbittorrent",
    "get_quality_profile",
    "get_reconcile_status",
    "get_session",
    "get_tmdb",
    "get_tv_root",
    "get_tv_root_optional",
    "load_system_settings",
    "require_api_key",
    "require_pre_init_or_api_key",
]

_logger = logging.getLogger(__name__)

# The bearer-token header. Declared via ``APIKeyHeader`` (below) so FastAPI emits
# the security scheme + per-route requirement into the OpenAPI document — without
# it, generated clients would treat protected routes as unauthenticated and omit
# the key.
API_KEY_HEADER_NAME = "X-Api-Key"
# ``auto_error=False``: we do the rejection ourselves so the failure detail stays
# the stable ``invalid_api_key`` (and so the pre-init paths can stay open).
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


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
        """
        is_secret = key in SECRET_SETTING_KEYS
        row = await self._row(key)
        if row is None:
            row = Setting(key=key, is_secret=is_secret)
            self._session.add(row)
        row.is_secret = is_secret
        if is_secret:
            row.encrypted_value = value
            row.value = None
        else:
            row.value = value
            row.encrypted_value = None
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


async def require_api_key(
    provided: Annotated[str | None, Depends(_api_key_header)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Enforce the ``X-Api-Key`` header against ``SystemSettings.app_api_key``.

    The header source is :class:`APIKeyHeader`, so the security scheme + per-route
    requirement appear in the exported OpenAPI (generated clients then send the
    key). The stored key is Fernet-encrypted at rest; the incoming value is
    constant-time-compared (``hmac.compare_digest``) against the decrypted value.
    Skipped entirely when ``settings.dev_auth_bypass`` is set (dev only).
    """
    if get_settings().dev_auth_bypass:
        return
    system = await load_system_settings(session)
    expected = system.app_api_key if system is not None else None
    if not api_key_matches(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_api_key")


async def require_pre_init_or_api_key(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Open before first-run init; require ``X-Api-Key`` once initialized.

    The setup ``validate/*`` probes must be callable pre-init (no app key exists
    yet), but each drives a server-side request to a caller-supplied URL. Leaving
    them anonymous post-init would turn them into an SSRF / reachability oracle,
    so once ``initialized`` is set they fall under the same api-key gate as the
    rest of the API (still skippable via ``dev_auth_bypass``).

    Unlike :func:`require_api_key`, the header is read imperatively from the
    request (not via :class:`APIKeyHeader`): these setup routes are intentionally
    NOT marked as secured in the OpenAPI, since they are open before init.
    """
    system = await load_system_settings(session)
    if system is None or not system.initialized:
        return
    if get_settings().dev_auth_bypass:
        return
    provided = request.headers.get(API_KEY_HEADER_NAME)
    if not api_key_matches(provided, system.app_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_api_key")


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


async def get_qbittorrent(
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> DownloadClientPort:
    """Build a configured :class:`DownloadClientPort` (qBittorrent), else 409."""
    store = SettingsStore(session)
    url = await store.get("qbittorrent_url")
    username = await store.get("qbittorrent_username")
    password = await store.get("qbittorrent_password")
    if not url or not username or password is None:
        raise ServiceNotConfiguredError("qbittorrent")
    return QbittorrentClient(client, url, username, password)


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


def get_eviction_filesystem(movies_root: str | None, tv_root: str | None) -> FileSystemPort:
    """Build the ONLY :class:`FileSystemPort` instance eviction's ``delete()`` may
    ever be handed (ADR-0012): scoped to whichever of the two library roots are
    actually configured, so the containment guard has something real to check
    against. :func:`get_filesystem`'s default instance has NO roots and refuses
    everything — using it here would make every eviction sweep a silent no-op.

    Takes plain values (not other ``Depends``) so it composes identically from
    the periodic eviction loop (``web/app.py``) and a future manual
    ``POST /api/v1/ops/evict`` trigger, both of which already resolve
    ``movies_root``/``tv_root`` via :func:`get_movies_root_optional`/
    :func:`get_tv_root_optional`.
    """
    roots = [root for root in (movies_root, tv_root) if root]
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


async def get_movies_root(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str:
    """Return the configured Movies library root, or 409 if unset."""
    root = await SettingsStore(session).get("movies_root")
    if not root:
        raise ServiceNotConfiguredError("movies_root")
    return root


async def get_movies_root_optional(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str | None:
    """Return the Movies root, or ``None`` when unset (the importer waits, no crash).

    Normalizes a falsy stored value (``""``) to ``None`` so callers can use a
    single ``is None`` check, matching :func:`get_movies_root`'s ``if not root``
    treatment of "unset". Without this, an empty-string root would sail past an
    ``is None`` guard downstream and silently resolve relative paths against the
    process CWD instead of tripping the honest ``ImportBlocked`` it's meant to.
    """
    return await SettingsStore(session).get("movies_root") or None


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
    root = await SettingsStore(session).get("tv_root")
    if not root:
        raise ServiceNotConfiguredError("tv_root")
    return root


async def get_tv_root_optional(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> str | None:
    """Return the TV root, or ``None`` when unset (the importer surfaces an honest,
    per-row ``ImportBlocked`` for a tv download instead of a crash or an upfront
    409 that would also block importing movies on an install with no TV root).

    Mirrors :func:`get_movies_root_optional`'s falsy-to-``None`` normalization: an
    empty-string setting is "unset", not a valid root, so downstream ``is None``
    guards must see it as such.
    """
    return await SettingsStore(session).get("tv_root") or None


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

# Values that parse as boolean-true; anything else (including unset/unparsable)
# is false. Matches the plain-string ``settings.value`` storage -- there is no
# dedicated boolean column type here (mirrors ``is_secret``'s own dialect-portable
# boolean handling being a DIFFERENT, ORM-level concern from this string parse).
_TRUE_STRINGS: frozenset[str] = frozenset({"1", "true", "yes", "on"})


async def _get_float_setting(session: AsyncSession, key: str, default: float) -> float:
    """Return ``key`` parsed as ``float``, or ``default`` if unset/unparsable.

    A parse failure is logged (never silent) but never raises -- a malformed
    stored value must not crash the reconcile / eviction / log-retention loops
    that read these; it falls back to the safe default instead (honesty over
    silence: the fallback is visible in the log, not just silently applied).
    """
    raw = await SettingsStore(session).get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        _logger.warning(
            "setting %r has an unparsable value %r; using default %s", key, raw, default
        )
        return default


async def _get_int_setting(session: AsyncSession, key: str, default: int) -> int:
    """Return ``key`` parsed as ``int``, or ``default`` if unset/unparsable."""
    raw = await SettingsStore(session).get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _logger.warning(
            "setting %r has an unparsable value %r; using default %s", key, raw, default
        )
        return default


async def _get_bool_setting(session: AsyncSession, key: str, default: bool) -> bool:
    """Return ``key`` parsed as ``bool`` (case-insensitive ``1``/``true``/``yes``/``on``)."""
    raw = await SettingsStore(session).get(key)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_STRINGS


async def get_disk_pressure_threshold_percent(session: AsyncSession) -> float:
    """Used% at/above which a root's disk-pressure eviction sweep fires (default 90)."""
    return await _get_float_setting(
        session, "disk_pressure_threshold_percent", DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT
    )


async def get_disk_pressure_target_percent(session: AsyncSession) -> float:
    """Used% the sweep evicts stalest-first candidates down towards (default 80)."""
    return await _get_float_setting(
        session, "disk_pressure_target_percent", DISK_PRESSURE_TARGET_PERCENT_DEFAULT
    )


async def get_eviction_grace_days(session: AsyncSession) -> int:
    """Minimum days since ``last_viewed_at`` before a watched title is evictable (default 30)."""
    return await _get_int_setting(session, "eviction_grace_days", EVICTION_GRACE_DAYS_DEFAULT)


async def get_eviction_enabled(session: AsyncSession) -> bool:
    """Whether the pressure-triggered eviction sweep may run at all (default true)."""
    return await _get_bool_setting(session, "eviction_enabled", EVICTION_ENABLED_DEFAULT)


async def get_eviction_proactive_enabled(session: AsyncSession) -> bool:
    """Whether past-grace watched+unpinned content evicts even without pressure (default false)."""
    return await _get_bool_setting(
        session, "eviction_proactive_enabled", EVICTION_PROACTIVE_ENABLED_DEFAULT
    )


async def get_eviction_interval_minutes(session: AsyncSession) -> float:
    """How often the eviction sweep's own periodic task runs (default 30 minutes)."""
    return await _get_float_setting(
        session, "eviction_interval_minutes", EVICTION_INTERVAL_MINUTES_DEFAULT
    )


async def get_log_retention_days(session: AsyncSession) -> int:
    """How many days of captured ``log_events`` rows the retention sweep keeps (default 7)."""
    return await _get_int_setting(session, "log_retention_days", LOG_RETENTION_DAYS_DEFAULT)
