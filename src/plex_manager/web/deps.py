"""FastAPI dependencies: DB session, api-key auth, settings store, adapters.

Wiring rules:

* ``get_session`` reuses :func:`plex_manager.db.get_session`.
* ``require_api_key`` enforces the static ``X-Api-Key`` header against
  ``SystemSettings.app_api_key_hash`` — the incoming header is SHA-256-hashed and
  constant-time-compared, so the plaintext key is never at rest. The header is
  sourced via ``APIKeyHeader`` so the security scheme appears in the OpenAPI. It
  is skipped when ``settings.dev_auth_bypass`` is set. Health, setup and docs
  routes do NOT depend on it.
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
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.adapters.prowlarr.adapter import ProwlarrIndexer
from plex_manager.adapters.qbittorrent.adapter import QbittorrentClient
from plex_manager.adapters.tmdb.adapter import TmdbMetadata
from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.domain.quality_profile import QualityProfile, default_profile
from plex_manager.models import Setting, SystemSettings
from plex_manager.ports.download_client import DownloadClientPort
from plex_manager.ports.indexer import IndexerPort
from plex_manager.ports.metadata import MetadataPort
from plex_manager.ports.parser import ParserPort

__all__ = [
    "API_KEY_HEADER_NAME",
    "KNOWN_SETTING_KEYS",
    "SECRET_MASK",
    "SECRET_SETTING_KEYS",
    "ServiceNotConfiguredError",
    "SettingsStore",
    "ensure_system_settings",
    "get_http_client",
    "get_parser",
    "get_prowlarr",
    "get_qbittorrent",
    "get_quality_profile",
    "get_session",
    "get_tmdb",
    "hash_api_key",
    "load_system_settings",
    "require_api_key",
    "require_pre_init_or_api_key",
]

# The bearer-token header. Declared via ``APIKeyHeader`` (below) so FastAPI emits
# the security scheme + per-route requirement into the OpenAPI document — without
# it, generated clients would treat protected routes as unauthenticated and omit
# the key.
API_KEY_HEADER_NAME = "X-Api-Key"
# ``auto_error=False``: we do the rejection ourselves so the failure detail stays
# the stable ``invalid_api_key`` (and so the pre-init paths can stay open).
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


def hash_api_key(token: str) -> str:
    """Return the SHA-256 hex digest used to store / compare the app API key.

    The plaintext bearer token is NEVER persisted; only this digest is. Auth then
    hashes the incoming header and constant-time-compares it to the stored digest,
    so a DB-backup leak cannot yield a usable key (ADR-0005).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# The canonical config keys (also the ``settings.key`` values and the wire field
# names in the settings schema — one stable naming, no translation layer).
KNOWN_SETTING_KEYS: tuple[str, ...] = (
    "plex_url",
    "plex_token",
    "prowlarr_url",
    "prowlarr_api_key",
    "qbittorrent_url",
    "qbittorrent_username",
    "qbittorrent_password",
    "tmdb_api_key",
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


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def _api_key_matches(provided: str | None, expected_hash: str | None) -> bool:
    """Constant-time check of the incoming header against the stored hash.

    The header is hashed and ``hmac.compare_digest``-compared to the stored
    SHA-256 digest, so neither the comparison nor the database read can leak a
    usable key. A missing header or an uninitialised install (no stored hash)
    never matches.
    """
    if not provided or not expected_hash:
        return False
    return hmac.compare_digest(hash_api_key(provided), expected_hash)


async def require_api_key(
    provided: Annotated[str | None, Depends(_api_key_header)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> None:
    """Enforce the ``X-Api-Key`` header against ``SystemSettings.app_api_key_hash``.

    The header source is :class:`APIKeyHeader`, so the security scheme + per-route
    requirement appear in the exported OpenAPI (generated clients then send the
    key). The incoming value is SHA-256-hashed and constant-time-compared
    (``hmac.compare_digest``) to the stored digest — the plaintext key is never at
    rest. Skipped entirely when ``settings.dev_auth_bypass`` is set (dev only).
    """
    if get_settings().dev_auth_bypass:
        return
    system = await load_system_settings(session)
    expected = system.app_api_key_hash if system is not None else None
    if not _api_key_matches(provided, expected):
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
    if not _api_key_matches(provided, system.app_api_key_hash):
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


# --------------------------------------------------------------------------- #
# Pure-domain dependencies (no I/O, but injected so tests can swap them)
# --------------------------------------------------------------------------- #
def get_parser() -> ParserPort:
    """Return the release-name parser (guessit adapter, confined to its module)."""
    return GuessitParser()


def get_quality_profile() -> QualityProfile:
    """Return the alpha's hardcoded default quality profile (read-only)."""
    return default_profile()
