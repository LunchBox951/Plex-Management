"""First-run setup wizard endpoints.

``status`` is reachable before setup so the SPA can discover whether a bootstrap
token is required. ``complete`` and ``validate/*`` require ``X-Setup-Token`` before
init (no app key exists yet) and the API key after init. ``status`` reports only
the install-state flag — it NEVER re-serves the app api key (the key is revealed
exactly once, in the ``/complete`` response). ``complete`` is one-shot: once
initialized it is rejected (409) so an anonymous caller can't overwrite creds or
re-mint the key; post-init configuration changes go through the authenticated
``PUT /api/v1/settings``.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_409_CONFLICT

from plex_manager.db import get_session
from plex_manager.models import SystemSettings
from plex_manager.web.deps import (
    API_KEY_HEADER_NAME,
    SETUP_TOKEN_HEADER_NAME,
    SettingsStore,
    ensure_system_settings,
    get_http_client,
    is_setup_token_required,
    load_system_settings,
    require_pre_init_or_api_key,
    require_setup_token_pre_init,
)
from plex_manager.web.schemas import (
    ErrorDetail,
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
SetupTokenHeader = Annotated[
    str | None,
    Header(
        alias=SETUP_TOKEN_HEADER_NAME,
        description=(
            "Required before setup only when /api/v1/setup/status reports "
            "setup_token_required=true."
        ),
    ),
]
ApiKeyHeader = Annotated[
    str | None,
    Header(
        alias=API_KEY_HEADER_NAME,
        description="Required after setup is initialized.",
    ),
]
_SETUP_TOKEN_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorDetail, "description": "Invalid setup token or API key"},
}
_SETUP_COMPLETE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_SETUP_TOKEN_RESPONSES,
    409: {"model": ErrorDetail, "description": "Setup already initialized"},
}


@router.post(
    "/validate/plex",
    dependencies=[Depends(require_pre_init_or_api_key)],
    responses=_SETUP_TOKEN_RESPONSES,
)
async def validate_plex_endpoint(
    body: PlexValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    _setup_token: SetupTokenHeader = None,
    _api_key: ApiKeyHeader = None,
) -> ServiceValidateResponse:
    """Test candidate Plex credentials."""
    return await validate_plex(client, body.url, body.token)


@router.post(
    "/validate/prowlarr",
    dependencies=[Depends(require_pre_init_or_api_key)],
    responses=_SETUP_TOKEN_RESPONSES,
)
async def validate_prowlarr_endpoint(
    body: ProwlarrValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    _setup_token: SetupTokenHeader = None,
    _api_key: ApiKeyHeader = None,
) -> ServiceValidateResponse:
    """Test candidate Prowlarr credentials."""
    return await validate_prowlarr(client, body.url, body.api_key)


@router.post(
    "/validate/qbittorrent",
    dependencies=[Depends(require_pre_init_or_api_key)],
    responses=_SETUP_TOKEN_RESPONSES,
)
async def validate_qbittorrent_endpoint(
    body: QbittorrentValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    _setup_token: SetupTokenHeader = None,
    _api_key: ApiKeyHeader = None,
) -> ServiceValidateResponse:
    """Test candidate qBittorrent credentials."""
    return await validate_qbittorrent(client, body.url, body.username, body.password)


@router.post(
    "/validate/tmdb",
    dependencies=[Depends(require_pre_init_or_api_key)],
    responses=_SETUP_TOKEN_RESPONSES,
)
async def validate_tmdb_endpoint(
    body: TmdbValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    _setup_token: SetupTokenHeader = None,
    _api_key: ApiKeyHeader = None,
) -> ServiceValidateResponse:
    """Test a candidate TMDB api key."""
    return await validate_tmdb(client, body.api_key)


@router.post(
    "/complete",
    dependencies=[Depends(require_setup_token_pre_init)],
    responses=_SETUP_COMPLETE_RESPONSES,
)
async def complete(
    body: SetupCompleteRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _setup_token: SetupTokenHeader = None,
) -> SetupStatusResponse:
    """Persist the validated creds, mint the app api key, mark initialized.

    One-shot AND concurrency-safe: rejected with 409 once initialized. Two
    concurrent ``/complete`` calls can both pass an in-memory ``initialized`` check
    and double-write (a ``settings.key`` unique-constraint 500, or one overwriting
    the other's just-issued ``app_api_key``). To prevent that, initialization is
    claimed with a CONDITIONAL update (``... WHERE id = 1 AND initialized = false``):
    exactly one request flips the row, and the loser sees ``rowcount == 0`` and is
    rejected 409 — so only the winner mints the key and writes the creds. Re-running
    setup post-init would also let an unauthenticated caller overwrite every stored
    credential and re-disclose the app key, so post-init changes must go through the
    authenticated ``PUT /settings``.
    """
    # Ensure the singleton row (id=1) exists so the conditional update has a target.
    await ensure_system_settings(session)

    # Mint the bearer token. It is stored Fernet-encrypted at rest (EncryptedStr,
    # like every other secret) and revealed in plaintext exactly once — in the
    # response below — so a DB-backup leak cannot yield a usable key (ADR-0005).
    app_api_key = secrets.token_urlsafe(_API_KEY_BYTES)
    now = datetime.now(UTC)
    # Atomically claim initialization. Only the still-uninitialized row matches, so
    # a concurrent second caller updates 0 rows and is rejected below — the claim is
    # the single serialization point that guarantees one key and one set of creds.
    claim = cast(
        CursorResult[Any],
        await session.execute(
            update(SystemSettings)
            .where(SystemSettings.id == 1, SystemSettings.initialized.is_(False))
            .values(
                initialized=True,
                app_api_key=app_api_key,
                setup_started_at=now,
                setup_completed_at=now,
            )
        ),
    )
    if claim.rowcount == 0:
        await session.rollback()
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="already_initialized")

    # We won the claim — persist the validated creds in the same transaction.
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
    # Iterates ``values`` (exactly the fields the setup wizard collects), NOT
    # ``KNOWN_SETTING_KEYS`` — that tuple also carries the operability-beta
    # settings (disk-pressure thresholds, eviction tuning, log retention), which
    # are never part of the wizard and must stay UNSET here so their typed
    # getters (``web/deps.py``) fall back to their safe defaults, not a
    # KeyError on a field this request body never had.
    for key, value in values.items():
        await store.set(key, value)
    # Library roots are independently optional: movie-only, TV-only, and mixed
    # installs are all valid. Write a root only when the operator actually supplied
    # one, so an unset root reads back as None from GET /settings rather than an
    # empty string.
    if body.movies_root:
        await store.set("movies_root", body.movies_root)
    if body.tv_root:
        await store.set("tv_root", body.tv_root)

    await session.commit()
    return SetupStatusResponse(initialized=True, app_api_key=app_api_key)


@router.get("/status")
async def status(
    request: Request,
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
    return SetupStatusResponse(
        initialized=initialized,
        app_api_key=None,
        setup_token_required=not initialized and is_setup_token_required(request),
    )
