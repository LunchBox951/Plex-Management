"""First-run setup wizard endpoints.

``status`` and ``complete`` are reachable pre-init (no app key exists yet) and are
exempt from the setup guard. ``status`` reports only the install-state flag — it
NEVER re-serves the app api key (the key is revealed exactly once, in the
``/complete`` response). ``complete`` is one-shot: once initialized it is rejected
(409) so an anonymous caller can't overwrite creds or re-mint the key; post-init
configuration changes go through the authenticated ``PUT /api/v1/settings``. The
``validate/*`` probes do a real lightweight connection check to a caller-supplied
URL; they are open pre-init but require the api key once initialized (so they
can't become an anonymous SSRF / reachability oracle).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_409_CONFLICT

from plex_manager.db import get_session
from plex_manager.web.deps import (
    KNOWN_SETTING_KEYS,
    SettingsStore,
    ensure_system_settings,
    get_http_client,
    load_system_settings,
    require_pre_init_or_api_key,
)
from plex_manager.web.schemas import (
    PlexValidateRequest,
    ProwlarrValidateRequest,
    QbittorrentValidateRequest,
    ServiceValidateResponse,
    SetupCompleteRequest,
    SetupStatusResponse,
    TmdbValidateRequest,
)
from plex_manager.web.setup_validation import (
    validate_plex,
    validate_prowlarr,
    validate_qbittorrent,
    validate_tmdb,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])

_API_KEY_BYTES = 32


@router.post("/validate/plex", dependencies=[Depends(require_pre_init_or_api_key)])
async def validate_plex_endpoint(
    body: PlexValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> ServiceValidateResponse:
    """Test candidate Plex credentials."""
    return await validate_plex(client, body.url, body.token)


@router.post("/validate/prowlarr", dependencies=[Depends(require_pre_init_or_api_key)])
async def validate_prowlarr_endpoint(
    body: ProwlarrValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> ServiceValidateResponse:
    """Test candidate Prowlarr credentials."""
    return await validate_prowlarr(client, body.url, body.api_key)


@router.post("/validate/qbittorrent", dependencies=[Depends(require_pre_init_or_api_key)])
async def validate_qbittorrent_endpoint(
    body: QbittorrentValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> ServiceValidateResponse:
    """Test candidate qBittorrent credentials."""
    return await validate_qbittorrent(client, body.url, body.username, body.password)


@router.post("/validate/tmdb", dependencies=[Depends(require_pre_init_or_api_key)])
async def validate_tmdb_endpoint(
    body: TmdbValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> ServiceValidateResponse:
    """Test a candidate TMDB api key."""
    return await validate_tmdb(client, body.api_key)


@router.post("/complete")
async def complete(
    body: SetupCompleteRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SetupStatusResponse:
    """Persist the validated creds, mint the app api key, mark initialized.

    One-shot: rejected with 409 once initialized. Re-running setup post-init would
    let an unauthenticated caller overwrite every stored credential (repoint
    qB/Prowlarr/Plex/TMDB at attacker infrastructure) and re-disclose the app key,
    so post-init changes must go through the authenticated ``PUT /settings``.
    """
    system = await ensure_system_settings(session)
    if system.initialized:
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="already_initialized")
    store = SettingsStore(session)
    values: dict[str, str] = {
        "plex_url": body.plex_url,
        "plex_token": body.plex_token,
        "prowlarr_url": body.prowlarr_url,
        "prowlarr_api_key": body.prowlarr_api_key,
        "qbittorrent_url": body.qbittorrent_url,
        "qbittorrent_username": body.qbittorrent_username,
        "qbittorrent_password": body.qbittorrent_password,
        "tmdb_api_key": body.tmdb_api_key,
    }
    for key in KNOWN_SETTING_KEYS:
        await store.set(key, values[key])

    if not system.app_api_key:
        system.app_api_key = secrets.token_urlsafe(_API_KEY_BYTES)
    if system.setup_started_at is None:
        system.setup_started_at = datetime.now(UTC)
    system.initialized = True
    system.setup_completed_at = datetime.now(UTC)
    await session.commit()
    return SetupStatusResponse(initialized=True, app_api_key=system.app_api_key)


@router.get("/status")
async def status(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SetupStatusResponse:
    """Report install state only — never the app api key.

    The key is revealed exactly once, in the ``/complete`` response (the SPA
    persists it then). Re-serving it from this unauthenticated GET would hand any
    anonymous caller the master ``X-Api-Key`` post-init, nullifying the entire
    auth model, so ``app_api_key`` is always ``None`` here.
    """
    system = await load_system_settings(session)
    initialized = system is not None and system.initialized
    return SetupStatusResponse(initialized=initialized, app_api_key=None)
