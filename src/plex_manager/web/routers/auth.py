"""Browser authentication via Plex hosted sign-in.

The app keeps the existing ``X-Api-Key`` recovery/automation path, but normal
browser access uses a Plex owner login and an HTTP-only session cookie.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.adapters.plex.oauth import PlexOAuthClient, PlexOAuthPending
from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.models import AuthSession, PlexLoginState, User
from plex_manager.web.deps import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    AuthContext,
    AuthMethod,
    SettingsStore,
    authenticate_request,
    get_http_client,
    hash_session_token,
    require_api_key,
)
from plex_manager.web.schemas import (
    AuthMeResponse,
    AuthUser,
    PlexLoginCompleteRequest,
    PlexLoginStartResponse,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_CLIENT_ID_SETTING = "plex_oauth_client_identifier"
_SESSION_DAYS = 30
_COOKIE_PATH = "/"
_LOGIN_COOKIE_NAME = "plexmgr.login"
_LOGIN_COOKIE_PATH = "/api/v1/auth/plex"


@router.post("/plex/start")
async def plex_start_endpoint(
    response: Response,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> PlexLoginStartResponse:
    """Create a plex.tv PIN challenge and return the hosted auth URL."""

    client_identifier = await _get_or_create_client_identifier(session)
    state = secrets.token_urlsafe(32)
    browser_token = secrets.token_urlsafe(32)
    oauth = PlexOAuthClient(client, client_identifier=client_identifier)
    pin = await oauth.create_pin(return_url=_callback_url(request, state=state))
    expires_at = pin.expires_at
    session.add(
        PlexLoginState(
            state=state,
            pin_id=pin.pin_id,
            code=pin.code,
            client_identifier=client_identifier,
            browser_token_hash=hash_session_token(browser_token),
            expires_at=expires_at,
        )
    )
    await session.commit()
    _set_login_cookie(response, request=request, browser_token=browser_token, expires_at=expires_at)
    return PlexLoginStartResponse(state=state, auth_url=pin.auth_url, expires_at=expires_at)


@router.post("/plex/complete")
async def plex_complete_endpoint(
    body: PlexLoginCompleteRequest,
    response: Response,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> AuthMeResponse:
    """Complete a Plex PIN challenge, verify owner access, and issue cookies."""

    state = await _load_pending_state(
        session,
        body.state,
        browser_token=request.cookies.get(_LOGIN_COOKIE_NAME),
    )
    oauth = PlexOAuthClient(client, client_identifier=state.client_identifier)
    try:
        user_token = await oauth.poll_pin(state.pin_id)
    except PlexOAuthPending as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="plex_auth_pending"
        ) from exc

    store = SettingsStore(session)
    plex_url = await store.get("plex_url")
    plex_token = await store.get("plex_token")
    if not plex_url or not plex_token:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="service_not_configured")

    account = await oauth.fetch_account(user_token)
    resources = await oauth.fetch_resources(user_token)
    machine_identifier = await oauth.fetch_server_identity(plex_url, plex_token)
    server_resource = next(
        (resource for resource in resources if resource.client_identifier == machine_identifier),
        None,
    )
    if server_resource is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not_valid_plex_user")
    is_admin = server_resource.owned

    user = await _upsert_user(
        session,
        account_id=account.plex_id,
        username=account.username,
        is_admin=is_admin,
    )
    user.email = account.email
    user.avatar_url = account.avatar_url
    user.encrypted_plex_token = user_token
    user.last_login = datetime.now(UTC)
    await _consume_pending_state(session, state.id)

    raw_session = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=_SESSION_DAYS)
    session.add(
        AuthSession(
            user_id=user.id,
            token_hash=hash_session_token(raw_session),
            expires_at=expires_at,
            last_seen_at=datetime.now(UTC),
        )
    )
    await session.commit()
    _set_session_cookies(
        response,
        request=request,
        session_token=raw_session,
        csrf_token=csrf_token,
        expires_at=expires_at,
    )
    _clear_login_cookie(response, request=request)
    return _me_response(
        AuthContext(
            method=AuthMethod.plex_session,
            user_id=user.id,
            plex_id=user.plex_id,
            username=user.username,
            email=user.email,
            avatar_url=user.avatar_url,
            is_admin=user.permissions > 0,
        )
    )


@router.get("/me")
async def me_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AuthMeResponse:
    """Return current auth state without requiring auth."""

    context = await authenticate_request(
        request,
        session,
        provided_api_key=request.headers.get("X-Api-Key"),
        enforce_csrf=False,
    )
    return _me_response(context)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout_endpoint(
    request: Request,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_api_key)],
) -> None:
    """Revoke the current Plex browser session and clear auth cookies."""

    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        result = await session.execute(
            select(AuthSession).where(AuthSession.token_hash == hash_session_token(token))
        )
        auth_session = result.scalars().first()
        if auth_session is not None and auth_session.revoked_at is None:
            auth_session.revoked_at = datetime.now(UTC)
            await session.commit()
    _clear_session_cookies(response, request=request)


async def _get_or_create_client_identifier(session: AsyncSession) -> str:
    store = SettingsStore(session)
    existing = await store.get(_CLIENT_ID_SETTING)
    if existing:
        return existing
    created = f"plex-manager-{secrets.token_urlsafe(18)}"
    await store.set(_CLIENT_ID_SETTING, created)
    await session.flush()
    return created


async def _load_pending_state(
    session: AsyncSession,
    state_value: str,
    *,
    browser_token: str | None,
) -> PlexLoginState:
    if not browser_token:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="invalid_plex_login_state")
    result = await session.execute(
        select(PlexLoginState).where(
            PlexLoginState.state == state_value,
            PlexLoginState.browser_token_hash == hash_session_token(browser_token),
        )
    )
    state = result.scalars().first()
    now = datetime.now(UTC)
    if state is None or state.consumed_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="invalid_plex_login_state")
    expires_at = (
        state.expires_at.replace(tzinfo=UTC)
        if state.expires_at.tzinfo is None
        else state.expires_at
    )
    if expires_at <= now:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="plex_login_expired")
    return state


async def _consume_pending_state(session: AsyncSession, state_id: int) -> None:
    """Atomically mark a PIN state consumed before issuing a browser session."""

    result = cast(
        "Any",
        await session.execute(
            update(PlexLoginState)
            .where(PlexLoginState.id == state_id, PlexLoginState.consumed_at.is_(None))
            .values(consumed_at=datetime.now(UTC))
        ),
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="invalid_plex_login_state")


async def _upsert_user(
    session: AsyncSession,
    *,
    account_id: int,
    username: str,
    is_admin: bool,
) -> User:
    result = await session.execute(select(User).where(User.plex_id == account_id))
    user = result.scalars().first()
    if user is None:
        user = User(plex_id=account_id, username=username, permissions=1 if is_admin else 0)
        session.add(user)
        await session.flush()
    else:
        user.username = username
        user.permissions = 1 if is_admin else 0
    return user


def _me_response(context: AuthContext | None) -> AuthMeResponse:
    if context is None:
        return AuthMeResponse(authenticated=False)
    user = (
        AuthUser(
            id=context.user_id,
            plex_id=context.plex_id,
            username=context.username or "",
            email=context.email,
            avatar_url=context.avatar_url,
            is_admin=context.is_admin,
        )
        if context.user_id is not None
        else None
    )
    return AuthMeResponse(
        authenticated=True,
        auth_method=context.method.value,
        is_admin=context.is_admin,
        user=user,
    )


def _callback_url(request: Request, *, state: str) -> str:
    base = get_settings().public_base_url or str(request.base_url)
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=500, detail="invalid_public_base_url")
    return f"{base.rstrip('/')}/auth/plex/callback?state={state}"


def _cookie_secure(request: Request) -> bool:
    configured = get_settings().auth_cookie_secure
    if configured is not None:
        return configured
    return request.url.scheme == "https"


def _set_login_cookie(
    response: Response,
    *,
    request: Request,
    browser_token: str,
    expires_at: datetime,
) -> None:
    max_age = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        _LOGIN_COOKIE_NAME,
        browser_token,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        max_age=max_age,
        path=_LOGIN_COOKIE_PATH,
    )


def _set_session_cookies(
    response: Response,
    *,
    request: Request,
    session_token: str,
    csrf_token: str,
    expires_at: datetime,
) -> None:
    max_age = max(0, int((expires_at - datetime.now(UTC)).total_seconds()))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        httponly=True,
        secure=_cookie_secure(request),
        samesite="lax",
        max_age=max_age,
        path=_COOKIE_PATH,
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        httponly=False,
        secure=_cookie_secure(request),
        samesite="lax",
        max_age=max_age,
        path=_COOKIE_PATH,
    )


def _clear_session_cookies(response: Response, *, request: Request) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=_COOKIE_PATH,
        secure=_cookie_secure(request),
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path=_COOKIE_PATH,
        secure=_cookie_secure(request),
        samesite="lax",
    )


def _clear_login_cookie(response: Response, *, request: Request) -> None:
    response.delete_cookie(
        _LOGIN_COOKIE_NAME,
        path=_LOGIN_COOKIE_PATH,
        secure=_cookie_secure(request),
        samesite="lax",
    )
