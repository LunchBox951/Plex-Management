"""Browser authentication via a plex.tv token verified server-side.

The browser runs the plex.tv PIN flow itself and hands the resulting token to
``POST /api/v1/auth/plex``. This endpoint NEVER trusts the browser's claims:
identity and server ownership are re-derived server-side from plex.tv's v2 API
before any user or session row is written (north star #3 — honest, re-derived
state). Pre-init the first account that OWNS a Plex server claims setup
exclusively; post-init an account is admitted iff it has access to the
configured server (admin iff it owns it).

The app keeps the existing ``X-Api-Key`` recovery/automation path; normal
browser access uses this Plex sign-in plus an HTTP-only session cookie.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import httpx
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.adapters.plex.oauth import (
    PlexAccount,
    PlexResource,
    PlexTvClient,
    account_server_resource,
    owned_servers,
)
from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.models import AuthSession, SystemSettings, User
from plex_manager.web.deps import (
    CSRF_COOKIE_NAME,
    PLEX_MACHINE_ID_SETTING,
    SESSION_COOKIE_NAME,
    AuthContext,
    AuthMethod,
    SettingsStore,
    api_key_header,
    api_key_matches,
    authenticate_request,
    enforce_pre_init_setup_token,
    ensure_system_settings,
    get_http_client,
    hash_session_token,
    load_system_settings,
    require_api_key,
)
from plex_manager.web.errors import AppError
from plex_manager.web.events import close_realtime_streams
from plex_manager.web.schemas import AuthMeResponse, AuthUser, PlexSignInRequest

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_CLIENT_ID_SETTING = "plex_oauth_client_identifier"
_SESSION_DAYS = 30
_COOKIE_PATH = "/"

# In-process, per-client-IP sign-in throttle. A best-effort abuse brake for the
# ONE unauthenticated write endpoint, not a security boundary: it is deliberately
# simple (a sliding 60s window in a module-level dict), resets on restart, and is
# per-process. Tests clear it via ``reset_sign_in_throttle``.
_SIGN_IN_MAX_PER_MINUTE = 10
_SIGN_IN_WINDOW_SECONDS = 60.0
_sign_in_attempts: dict[str, list[float]] = {}


@router.post("/plex")
async def plex_sign_in_endpoint(
    body: PlexSignInRequest,
    response: Response,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> AuthMeResponse:
    """Verify a browser-obtained plex.tv token and mint a session.

    The browser ran the plex.tv PIN flow itself; this endpoint never trusts its
    claims — identity and server ownership are re-derived server-side from
    plex.tv's v2 API before any user or session row is written.
    """
    _throttle_sign_in(request)

    system = await load_system_settings(session)
    initialized = system is not None and system.initialized
    # The OPTIONAL pre-init hardening token (PLEX_MANAGER_SETUP_TOKEN) must gate the
    # EXCLUSIVE first-owner claim, not merely /complete: the sign-in claim IS the
    # first step of first-run setup. Without this, any account owning any Plex
    # server could win the pre-init claim and permanently lock out the true owner
    # (recoverable only by DB surgery — a north-star-#1 violation). Enforced BEFORE
    # any plex.tv call so a caller lacking the token cannot even drive the flow; a
    # no-op post-init and when no token is configured.
    enforce_pre_init_setup_token(request, initialized=initialized)

    client_identifier = await _get_or_create_client_identifier(session)
    plex_tv = PlexTvClient(client, client_identifier=client_identifier)
    account = await plex_tv.fetch_account(body.auth_token)
    resources = await plex_tv.fetch_resources(body.auth_token)

    if not initialized:
        is_admin = await _claim_or_resume_setup(session, account, resources)
    else:
        is_admin = await _post_init_access(session, account, resources, client=plex_tv)

    user = await _upsert_user(
        session, account_id=account.plex_id, username=account.username, is_admin=is_admin
    )
    user.email = account.email
    user.avatar_url = account.avatar_url
    user.encrypted_plex_token = body.auth_token
    user.last_login = datetime.now(UTC)

    await _issue_browser_session(session, response, request=request, user_id=user.id)
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


@router.post("/api-key")
async def exchange_api_key_endpoint(
    response: Response,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    provided: Annotated[str | None, Depends(api_key_header)],
) -> AuthMeResponse:
    """Exchange a valid ``X-Api-Key`` for the SAME HTTP-only session cookie.

    The recovery / automation key (ADR-0005, ADR-0016) is the terminal-free
    break-glass credential that authenticates when plex.tv is unreachable. Before
    this endpoint the browser had to keep the raw key in JS-readable
    ``localStorage`` so the break-glass path survived a reload — exactly the
    cleartext-storage pattern CodeQL #263 flags. Exchanging the key ONCE for the
    same cookie the Plex sign-in flow mints means the key never needs JS-readable
    storage: the resulting session is an admin-authority recovery session with no
    Plex identity (``auth_sessions.user_id`` NULL), and every later request
    authenticates by the HTTP-only cookie exactly like a Plex session does.

    The key is validated against the stored ``SystemSettings.app_api_key`` with a
    constant-time compare — accepting a Plex session cookie here (as
    ``require_api_key`` would) is deliberately NOT done: that would let any
    signed-in NON-admin mint an ADMIN recovery session. Only a caller who proves
    the recovery key gets one.
    """
    _throttle_sign_in(request)
    system = await load_system_settings(session)
    expected = system.app_api_key if system is not None else None
    # The header is sourced via the shared ``APIKeyHeader`` dependency (issue #293
    # finding 5) so the ``X-Api-Key`` requirement appears in the exported OpenAPI —
    # a raw ``Request.headers.get`` left the contract silent and generated clients
    # would omit the key and get an undocumented 401.
    if not api_key_matches(provided, expected):
        # A DISTINCT code from the generic ``invalid_api_key`` an expired session
        # yields (issue #293 finding 2): a rejected/mistyped recovery key at this
        # exchange endpoint must NOT trip the SPA's global "session expired ->
        # bounce to Plex login" 401 handler, which would yank the operator off the
        # break-glass key screen. The frontend branches on this code to keep the
        # KeyEntry screen showing "key rejected" instead.
        raise AppError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="recovery_key_rejected",
            message="That access key was not accepted.",
            hint="Check the recovery key from Settings → Access, then try again.",
        )
    await _issue_browser_session(session, response, request=request, user_id=None)
    return _me_response(AuthContext(method=AuthMethod.api_key, is_admin=True))


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
    revoked_user_id: int | None = None
    revoked_recovery = False
    if token:
        result = await session.execute(
            select(AuthSession).where(AuthSession.token_hash == hash_session_token(token))
        )
        auth_session = result.scalars().first()
        if auth_session is not None and auth_session.revoked_at is None:
            if auth_session.user_id is None:
                # A recovery/break-glass session (``user_id IS NULL``): it carries
                # no Plex identity, so it reports as ``api_key`` auth on the hub.
                revoked_recovery = True
            else:
                revoked_user_id = auth_session.user_id
            auth_session.revoked_at = datetime.now(UTC)
            await session.commit()
    if revoked_user_id is not None:
        close_realtime_streams(
            request.app,
            reason="session_logged_out",
            auth_method=AuthMethod.plex_session.value,
            user_id=revoked_user_id,
        )
    elif revoked_recovery:
        # Proactively close the recovery session's open SSE stream(s) instead of
        # waiting for reconnect/expiry to catch the revoked cookie (issue #293
        # finding 1). Recovery sessions carry no per-user identity on the hub, so
        # this closes every ``api_key`` stream — the same granularity the app-key
        # rotate/revoke path already uses; a header-authenticated automation client
        # simply reconnects with its still-valid key.
        close_realtime_streams(
            request.app,
            reason="session_logged_out",
            auth_method=AuthMethod.api_key.value,
        )
    _clear_session_cookies(response, request=request)


# --------------------------------------------------------------------------- #
# Access decisions
# --------------------------------------------------------------------------- #
async def _claim_or_resume_setup(
    session: AsyncSession,
    account: PlexAccount,
    resources: Sequence[PlexResource],
) -> bool:
    """Pre-init: the first OWNER to sign in claims setup, exclusively.

    Ownership is the entry gate — an account with no owned server can never be
    the setup admin. The claim is a compare-and-set on the singleton
    ``system_settings`` row: exactly one signed-in owner can stamp
    ``setup_started_at``. A later, DIFFERENT account that loses the race is
    refused; the SAME account (already the claimant, its user row present)
    resumes rather than being locked out.
    """
    if not owned_servers(resources):
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="no_owned_servers",
            message="Your Plex account does not own any Plex Media Server.",
            hint="Sign in with the account that owns the server this app should manage.",
        )
    await ensure_system_settings(session)
    claim = cast(
        # Unquoted (a runtime expression, not a string annotation) so both
        # ``CursorResult`` and ``Any`` are genuine runtime references, not names
        # that live only inside a string. SQLAlchemy's ``CursorResult`` supports
        # subscription at runtime, so this evaluates fine; the string form left
        # the imports looking unused to static scanners (CodeQL #269/#270).
        CursorResult[Any],
        await session.execute(
            update(SystemSettings)
            .where(SystemSettings.id == 1, SystemSettings.setup_started_at.is_(None))
            .values(setup_started_at=datetime.now(UTC))
        ),
    )
    if claim.rowcount == 0:
        existing = await session.execute(select(User).where(User.plex_id == account.plex_id))
        if existing.scalars().first() is None:
            raise AppError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="setup_already_claimed",
                message="Setup was already started by a different Plex account.",
                hint="Finish setup from the account that started it, or reset the database.",
            )
    return True  # claimant (or resuming claimant) is the admin


async def _post_init_access(
    session: AsyncSession,
    account: PlexAccount,
    resources: Sequence[PlexResource],
    *,
    client: PlexTvClient,
) -> bool:
    """Post-init: admit an account iff it can reach the configured server.

    The configured server's machine identifier is read from settings; on a
    pre-rework DB that never stored it, fall back to probing the Plex server's
    ``/identity`` with the stored service credentials. An account with no
    matching resource is refused; otherwise it is admitted, admin iff it OWNS
    that server.
    """
    _ = account  # identity is asserted by the fetched token; kept for a uniform signature
    store = SettingsStore(session)
    machine_identifier = await store.get(PLEX_MACHINE_ID_SETTING)
    if not machine_identifier:
        plex_url = await store.get("plex_url")
        plex_token = await store.get("plex_token")
        if not plex_url or not plex_token:
            raise AppError(
                status_code=status.HTTP_409_CONFLICT,
                code="service_not_configured",
                message="No Plex server is configured.",
                hint="An administrator must finish setup first.",
            )
        machine_identifier = await client.fetch_server_identity(plex_url, plex_token)
    resource = account_server_resource(resources, machine_identifier)
    if resource is None:
        raise AppError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="server_access_denied",
            message="Your Plex account has no access to the configured server.",
            hint="Ask the server owner to share the server with your account.",
        )
    return resource.owned


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
def _sign_in_throttle_key(request: Request) -> str:
    """The sliding-window throttle key for this request.

    ``trusted_proxy_hops`` (default 0) keys on ``request.client.host`` alone --
    the exact prior behaviour, so an operator who never sets the knob sees no
    change. Behind the documented reverse-proxy topology (docker-compose binds
    127.0.0.1; the operator fronts it with their own TLS proxy),
    ``request.client.host`` is ALWAYS the proxy's address, collapsing the
    "per-IP" throttle into a de-facto global cap an attacker can trip to lock
    out the real owner. Opting in with ``trusted_proxy_hops=N`` reads the Nth
    entry from the RIGHT of ``X-Forwarded-For`` -- the standard trusted-hop-count
    algorithm, since only the operator's own proxy chain is trusted to have
    appended entries; anything further left could be forged by the client
    itself. An absent, malformed, or shorter-than-N header falls back to the
    direct peer so it can never widen the trust boundary.
    """
    direct = request.client.host if request.client else "unknown"
    hops = get_settings().trusted_proxy_hops
    if hops <= 0:
        return direct
    header = request.headers.get("x-forwarded-for", "")
    if not header:
        return direct
    entries = [part.strip() for part in header.split(",")]
    if len(entries) < hops:
        return direct
    # Only the TRUSTED suffix (the last ``hops`` entries, appended by the
    # operator's own proxy chain) needs to be well-formed. Entries to its left
    # are client-controlled -- a client can freely send blank/malformed junk
    # there, and rejecting the whole header on that basis would let an
    # attacker force every request behind the proxy back onto the shared
    # ``direct`` key, recreating the exact global-cap lockout this throttle
    # key exists to prevent.
    trusted_suffix = entries[-hops:]
    if any(not entry for entry in trusted_suffix):
        return direct
    return trusted_suffix[0]


def _throttle_sign_in(request: Request) -> None:
    """Reject a client IP that exceeds the sliding-window sign-in budget."""
    key = _sign_in_throttle_key(request)
    now = time.monotonic()
    window_start = now - _SIGN_IN_WINDOW_SECONDS
    attempts = [stamp for stamp in _sign_in_attempts.get(key, []) if stamp > window_start]
    if len(attempts) >= _SIGN_IN_MAX_PER_MINUTE:
        _sign_in_attempts[key] = attempts
        raise AppError(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="sign_in_throttled",
            message="Too many sign-in attempts. Wait a minute and try again.",
        )
    attempts.append(now)
    _sign_in_attempts[key] = attempts


def reset_sign_in_throttle() -> None:
    """Clear the in-process sign-in throttle.

    A deliberately public test-isolation hook: the throttle is module-level state
    that would otherwise leak attempt counts across tests, so suites call this from
    an autouse fixture to stay order-independent. Named without a leading underscore
    so tests reference it without tripping pyright's private-usage check.
    """
    _sign_in_attempts.clear()


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
async def _get_or_create_client_identifier(session: AsyncSession) -> str:
    """Return the app's stable plex.tv device identifier, creating it once.

    CREATE-ONCE, never rotate: this is the ``X-Plex-Client-Identifier`` the app
    presents on every plex.tv call it ever makes (sign-in verification here,
    server discovery in the setup router, repoint validation in the settings
    router). plex.tv registers each DISTINCT identifier as a new device on the
    operator's account, so an identifier that rotated after minting would sprout
    a trail of phantom devices and desync requests that already loaded the old
    one — which is the whole point of persisting exactly one.

    All race handling lives in :meth:`SettingsStore.set_if_absent`: two
    simultaneous first-ever sign-ins on a clean DB may both mint a candidate,
    but exactly one value persists and BOTH requests proceed under it — the
    loser adopts the winner's identifier rather than 500ing (an unhandled
    ``IntegrityError``) or, worse, overwriting it (what the last-write-wins
    :meth:`SettingsStore.set` upsert would do — the composition bug a call-site
    ``IntegrityError`` guard here used to invite once the store grew its own
    conflict recovery). A candidate is minted unconditionally; when an
    identifier already exists, ``set_if_absent`` returns it and the unused
    candidate is simply discarded — cheap, and it keeps this call site a single
    race-free operation.
    """
    minted = f"plex-manager-{secrets.token_urlsafe(18)}"
    return await SettingsStore(session).set_if_absent(_CLIENT_ID_SETTING, minted)


async def find_user_by_plex_id(session: AsyncSession, plex_id: int) -> User | None:
    """Return the ``User`` row for a Plex account id, or ``None``.

    Public (no leading underscore) on purpose, mirroring
    :func:`reset_sign_in_throttle`: the upsert race test monkeypatches this
    lookup to simulate the concurrent-first-sign-in window (both racers observe
    "no row" before either commits) without reaching into private module state,
    which pyright's private-usage check would reject.
    """
    result = await session.execute(select(User).where(User.plex_id == plex_id))
    return result.scalars().first()


async def _upsert_user(
    session: AsyncSession,
    *,
    account_id: int,
    username: str,
    is_admin: bool,
) -> User:
    """Create-or-update the ``User`` row for this verified Plex account.

    Concurrency-safe on the FIRST sign-in: two simultaneous first-time sign-ins
    for the SAME Plex account can both pass the no-row lookup and both INSERT;
    ``users.plex_id`` is UNIQUE, so the loser's flush raises ``IntegrityError``.
    The loser must not 500 (the account is legitimately signing in — the same
    identity merely arrived twice): roll the failed transaction back, re-read the
    WINNER's committed row, and proceed — the caller's session mint then attaches
    a second ``AuthSession`` to the one shared user row, exactly as two
    sequential sign-ins would. The rollback is safe on the realistic (post-init)
    path because nothing else is pending in the session — post-init access
    decisions only read; the pre-init claim CAS serializes same-account sign-ins
    at the ``setup_started_at`` UPDATE before this runs, so a pre-init loser
    with a pending claim write cannot reach this collision.

    The refreshed fields (``username``/``permissions``) are applied to the
    winner's row too — recovery converges on the same state the plain update
    path would have written.
    """
    user = await find_user_by_plex_id(session, account_id)
    if user is None:
        created = User(plex_id=account_id, username=username, permissions=1 if is_admin else 0)
        session.add(created)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            user = await find_user_by_plex_id(session, account_id)
            if user is None:  # pragma: no cover - the conflicting row must exist
                raise
        else:
            return created
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


# --------------------------------------------------------------------------- #
# Session issuance (shared by Plex sign-in and the recovery-key exchange)
# --------------------------------------------------------------------------- #
async def _issue_browser_session(
    session: AsyncSession,
    response: Response,
    *,
    request: Request,
    user_id: int | None,
) -> None:
    """Mint an ``auth_sessions`` row, commit, and set the session + CSRF cookies.

    The single session-issuance path both browser-auth flows share: Plex sign-in
    passes the verified owning ``user_id``; the recovery-key exchange passes
    ``None`` (an admin recovery session with no Plex identity, CodeQL #263). Only
    the SHA-256 hash of the random token is stored; the raw token rides the
    HTTP-only cookie. The commit flushes any caller-pending writes too (the Plex
    path stages user-row updates before calling this).
    """
    raw_session = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=_SESSION_DAYS)
    session.add(
        AuthSession(
            user_id=user_id,
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


# --------------------------------------------------------------------------- #
# Cookie mechanics (unchanged contract: HTTP-only session + readable CSRF)
# --------------------------------------------------------------------------- #
def _cookie_secure(request: Request) -> bool:
    """Whether the session/CSRF cookies carry the ``Secure`` attribute.

    An explicit ``auth_cookie_secure`` override wins; otherwise the flag follows
    the request scheme, so a plain-http LAN install does not set ``Secure`` (the
    browser would silently refuse to send it back) while an https deployment does.
    """
    configured = get_settings().auth_cookie_secure
    if configured is not None:
        return configured
    return request.url.scheme == "https"


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
