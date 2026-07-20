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

import asyncio
import secrets
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, NamedTuple, cast

import httpx
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import CursorResult, func, select, update
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
from plex_manager.services import session_lifecycle

# The deps MODULE itself is imported (not just names from it) so the shared
# process-local ``plex_identity_generation`` counter is read/re-checked as
# ``deps.plex_identity_generation.value`` -- a genuine cross-module attribute
# read CodeQL can see, unlike a ``from``-imported bare name (see the ``Cell``
# docstring in ``web.deps``; alerts #363/#368, issue #385). ``secret_rotation``
# reads the same-module lock the same way.
from plex_manager.web import deps
from plex_manager.web.deps import (
    CSRF_COOKIE_NAME,
    PLEX_MACHINE_ID_SETTING,
    SESSION_COOKIE_NAME,
    AuthContext,
    AuthMethod,
    Cell,
    SettingsStore,
    api_key_header,
    api_key_matches,
    app_key_rotate_lock,
    authenticate_request,
    enforce_pre_init_setup_token,
    ensure_system_settings,
    get_http_client,
    hash_session_token,
    load_system_settings,
    require_admin,
    require_api_key,
)
from plex_manager.web.errors import AppError
from plex_manager.web.events import close_realtime_streams
from plex_manager.web.routers.settings import rollback_to_completion, secret_rotation
from plex_manager.web.schemas import (
    ActiveSessionsResponse,
    ActiveSessionUser,
    AuthMeResponse,
    AuthUser,
    PlexSignInRequest,
    RecoverySessionGroup,
    RevokeSessionsRequest,
    RevokeSessionsResponse,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_CLIENT_ID_SETTING = "plex_oauth_client_identifier"
_SESSION_DAYS = 30
_COOKIE_PATH = "/"

# Upper bound on the token-rotation sign-in's IN-BOUNDARY access recompute
# (issue #389 facet 4). The recompute runs while holding the process-global
# ``secret_rotation_lock``, and on the pre-rework fallback path (no stored
# ``plex_machine_identifier``) it issues a live ``/identity`` probe whose only
# other bound is the shared HTTP client's 30-second timeout -- long enough for a
# hung Plex server to stall every other rotation AND the durable log drain
# (which takes the same lock every tick). A few seconds is generous for a LAN
# ``/identity`` round trip; on expiry the sign-in fails CLOSED with an honest,
# retryable envelope (``server_identity_recheck_timeout``) and the boundary
# rolls back -- never an unbounded lock hold, never an unverified admission.
_IN_BOUNDARY_ACCESS_RECHECK_TIMEOUT_SECONDS: Final = 5.0

# Bound on the sign-in shape-decision retry (issue #400 round-2 finding 1). The
# ordinary (no-retire) tail confirms its classification against the COMMITTED
# stored token under the lock; if a concurrent rotation moved the token to a
# DIFFERENT value the request re-classifies and re-dispatches (the next pass
# takes the rotation branch, which retires the committed value through the
# redaction protocol). A DIFFERENT stored value routes straight to rotation, so
# in practice this converges in one re-classification; the bound only guards a
# pathological store that keeps changing under every attempt, failing CLOSED
# rather than spinning.
_MAX_SIGN_IN_SHAPE_ATTEMPTS: Final = 3

# In-process, per-client-IP sign-in throttle. A best-effort abuse brake for the
# ONE unauthenticated write endpoint, not a security boundary: it is deliberately
# simple (a sliding 60s window in a module-level dict), resets on restart, and is
# per-process. Stale-key cleanup reclaims expired bookkeeping — at most one
# full-dict scan per window, so a flood of distinct IPs cannot turn every sign-in
# request into an O(live keys) scan on the event loop — but it cannot cap the
# number of simultaneously live keys without changing per-key admission behavior.
# Tests clear it via ``reset_sign_in_throttle``.
_SIGN_IN_MAX_PER_MINUTE = 10
_SIGN_IN_WINDOW_SECONDS = 60.0
_sign_in_attempts: dict[str, list[float]] = {}
# Wrapped in ``Cell`` (see its docstring in ``web.deps``) rather than a bare
# ``global``-rebound float: the only reader of a given write is the NEXT call to
# ``_evict_stale_sign_in_throttle_keys``, a cross-invocation liveness CodeQL's
# py/unused-global-variable dead-store analysis doesn't track (alert #358, issue
# #385).
_last_stale_key_eviction = Cell(float("-inf"))


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

    # Stamp the process-local plex-identity generation BEFORE the access
    # decision (issue #400). A repoint bumps this counter AFTER its
    # revoke+machine-id commit is durable and while it still holds
    # ``secret_rotation_lock`` (see ``routers/settings.py``). The ordinary
    # (no-rotation) path re-checks this stamp INSIDE its own brief lock section
    # just before minting: because both the repoint's bump and the sign-in's
    # re-check happen under the one lock, a move seen there always reflects
    # fully-committed repoint state (recompute against it), and a move NOT seen
    # means the sign-in's mint serialized before the repoint's sweep (which then
    # revokes the fresh session). The token-rotation path recomputes access
    # in-lock via the boundary's ``pre_rewrite`` hook (facet 4) and needs no
    # generation check -- the lock alone serializes it with the repoint.
    identity_generation = deps.plex_identity_generation.value
    # The pre-lock access decision. For the token-rotation path this is
    # RECOMPUTED inside the boundary (facet 4 below); it is authoritative only
    # for the ordinary, no-rotation path, which never waits on the lock.
    if not initialized:
        is_admin = await _claim_or_resume_setup(session, account, resources)
    else:
        is_admin = await _post_init_access(session, account, resources, client=plex_tv)

    user, demoted = await _upsert_user(
        session,
        account_id=account.plex_id,
        username=account.username,
        is_admin=is_admin,
    )
    # The RETIRING credential, read BEFORE the new token is staged (issue #374):
    # ``EncryptedStr`` decrypts transparently on load, so this is the plaintext
    # value the redact-at-rotation pass (ADR-0026) must erase from log history.
    old_token = user.encrypted_plex_token
    staged_permissions = user.permissions  # what _upsert_user just computed/staged

    # The commit (below, either shape) persists the staged user-row writes
    # (including any demotion) alongside the new session. Only AFTER that commit
    # persists the downgrade do we close the demoted user's realtime streams
    # (issue #183): a close before the commit would race a reconnect that re-reads
    # the old admin permissions and resubscribes to admin topics. Post-commit, any
    # reconnect reads the demoted permissions, so no admin stream can survive the
    # downgrade. On the rotation shape the close runs via the boundary's
    # ``on_committed`` hook (codex #399 round 4): the boundary re-raises a
    # cancellation remembered during the commit BEFORE control returns here, so
    # an inline post-``with`` close could be skipped with the demotion already
    # durable -- leaving the demoted user's admin streams open until lease
    # expiry. Reads ``demoted``/``user`` at call time, after the body below has
    # recomputed them under the lock -- BOTH the post-init ordinary tail and the
    # rotation body REBIND ``demoted``/``user`` (shared closure cells with this
    # function), so any refactor that moves that rebinding into a nested function
    # must add ``nonlocal`` or this closure would silently see the stale pre-lock
    # value. The ordinary path calls this directly post-commit; the rotation path
    # runs it via the boundary's ``on_committed`` hook (codex #399 round 4) so a
    # cancellation remembered during the commit -- which the boundary re-raises
    # BEFORE control returns to the caller -- cannot skip it with the demotion
    # already durable.
    def _close_demoted_streams() -> None:
        if demoted:
            close_realtime_streams(
                request.app,
                reason="permission_downgraded",
                auth_method=AuthMethod.plex_session.value,
                user_id=user.id,
            )

    async def _recompute_access_in_lock() -> None:
        """RECOMPUTE the access decision while holding ``secret_rotation_lock``.

        Shared by both sign-in shapes when a Plex repoint may have moved the
        configured server out from under the pre-lock decision:

        * the ordinary path (issue #400) calls it directly, inside its own
          brief lock section, only when the identity generation moved;
        * the token-rotation path passes it as the boundary's ``pre_rewrite``
          hook (facet 4).

        A repoint holds this SAME lock across its revoke+commit and bumps the
        generation under it, so once we hold the lock the configured server can
        no longer move under us -- re-running ``_post_init_access`` binds this
        sign-in's admission/admin decision to the server as it stands NOW. An
        account with access only to the OLD server fails closed
        (``_post_init_access`` raises, nothing staged/minted) instead of a
        session against a server it cannot reach.

        TIME-BOUNDED because it runs under the process-global lock:
        ``_post_init_access``'s pre-rework fallback (no stored machine id)
        probes the Plex server's ``/identity`` live, and an unresponsive server
        must not hold the lock for the HTTP client's full 30s timeout (stalling
        every rotation and the log drain). On expiry the sign-in fails CLOSED --
        nothing staged or minted -- with a distinct, retryable envelope rather
        than a silent fallback to the stale pre-lock decision.
        """
        nonlocal is_admin
        try:
            async with asyncio.timeout(_IN_BOUNDARY_ACCESS_RECHECK_TIMEOUT_SECONDS):
                is_admin = await _post_init_access(session, account, resources, client=plex_tv)
        except TimeoutError as exc:
            raise AppError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code="server_identity_recheck_timeout",
                message="Could not re-verify your access to the configured Plex server in time.",
                hint="Check that the Plex server is reachable, then sign in again.",
            ) from exc

    async def _reread_retiring_token(rotation_session: AsyncSession) -> frozenset[str]:
        """Re-derive the token being retired, UNDER the boundary lock (facet 2).

        ``old_token`` was read before the lock. Two concurrent sign-ins for THIS
        account with different replacement tokens can both observe the same
        pre-lock ``old_token``; the loser must retire whatever value is ACTUALLY
        stored now (the winner's freshly committed token), not the stale pre-lock
        read, or the winner's token is left uncovered by the historical rewrite.
        Reading it here — after the boundary's in-lock rollback, in its fresh
        transaction — sees the committed current value. If it already equals the
        incoming token (a same-token race), there is nothing to retire. Reads
        ``user`` at call time: on a re-classified ordinary→rotation dispatch the
        loop rebinds ``user`` to the freshly-committed row before this runs.
        """
        await rotation_session.refresh(user)
        current = user.encrypted_plex_token
        if not current or current == body.auth_token:
            return frozenset()
        return frozenset({current})

    # SHAPE decision (issue #374 rotation vs. ordinary), re-tried because the
    # classifying ``old_token`` was read BEFORE the lock (issue #400 round-2
    # finding 1). A plain ordinary tail keyed on that pre-lock value could
    # overwrite a token a concurrent rotation just committed WITHOUT retiring it
    # through the redaction protocol -- both the "stored A, re-sign-in A while a
    # peer rotates A→B" race and two first-ever sign-ins with different tokens
    # (both see ``old_token is None``, the loser overwrites the winner). The
    # ordinary tail therefore confirms its no-retire basis against the COMMITTED
    # token under the lock and re-dispatches as a rotation if it moved.
    for _ in range(_MAX_SIGN_IN_SHAPE_ATTEMPTS):
        if old_token is not None and old_token != body.auth_token:
            # ROTATION: the stored token VALUE is changing (issue #374). Replace
            # it inside the same locked boundary ADR-0026 built for every secret
            # mutation. ``reread_retiring`` re-derives the retiring value fresh
            # under the lock; ``incoming_values`` masks the new token in-flight;
            # the boundary reads the rest of the transition set itself. Sign-in
            # fails CLOSED: any rewrite/commit failure rolls back the token
            # write, the session mint, and the historical rewrite together, and
            # cookies are set only after the boundary commits.
            async with secret_rotation(
                session,
                request,
                retiring_values=frozenset({old_token}),
                incoming_values=frozenset({body.auth_token}),
                reread_retiring=_reread_retiring_token,
                # The pre-init claim path stays in the body below: it performs
                # only local DB work (no live probe), so it cannot hold the
                # writer lock against a slow network peer the way the post-init
                # recompute can. ``_recompute_access_in_lock`` (facet 4) is
                # shared with the ordinary path.
                pre_rewrite=_recompute_access_in_lock if initialized else None,
                on_committed=_close_demoted_streams,
            ):
                # The boundary's in-lock rollback DISCARDED every pre-entry row
                # write; re-stage all of them so they commit atomically with the
                # historical rewrite and the session mint. (Access was already
                # recomputed under the lock by ``_recompute_access_in_lock``.)
                if not initialized:
                    is_admin = await _claim_or_resume_setup(session, account, resources)
                await SettingsStore(session).set_if_absent(_CLIENT_ID_SETTING, client_identifier)
                # Re-read the user row fresh (raises loudly if it went transient
                # rather than silently dropping the writes). ``previous_permissions``
                # is this user's committed authority BEFORE this sign-in; the
                # demotion flag is recomputed from the freshly-decided permissions
                # so a downgrade decided under the lock still closes streams
                # post-commit.
                await session.refresh(user)
                previous_permissions = user.permissions
                new_permissions = 1 if is_admin else 0
                demoted = new_permissions < previous_permissions
                _apply_signin_fields(
                    user, account, permissions=new_permissions, token=body.auth_token
                )
                staged = _stage_browser_session(session, user_id=user.id)
                # Deterministically flush the staged token so the boundary's
                # fresh post-yield ``secret_values()`` read narrows to the NEW
                # value.
                await session.flush()
            # The demoted-stream close already ran inside the boundary
            # (``on_committed``); only response construction remains out here.
            _set_session_cookies(
                response,
                request=request,
                session_token=staged.raw_token,
                csrf_token=staged.csrf_token,
                expires_at=staged.expires_at,
            )
            break

        # ORDINARY candidate (``old_token`` is None or already equals the
        # submitted token): a re-sign-in with the IDENTICAL token or a FIRST-EVER
        # token. No value is being retired, so this owes no historical rewrite
        # and never enters the ``secret_rotation`` boundary.
        if not initialized:
            # Pre-init: no repoint can race the first-owner claim (repointing is
            # a post-init admin action), and the claim CAS already serializes
            # concurrent first sign-ins -- keep the lockless mint.
            _apply_signin_fields(
                user, account, permissions=staged_permissions, token=body.auth_token
            )
            await _issue_browser_session(session, response, request=request, user_id=user.id)
            _close_demoted_streams()
            break

        # Post-init ordinary: mint under the SAME lock a repoint holds across its
        # revoke+commit+generation-bump (issue #400). The EXPENSIVE access
        # decision stayed lockless above; only this brief tail runs under the
        # lock. End the pre-lock transaction FIRST, driven to completion so a
        # client-disconnect cancellation can neither tear the rollback's DB op
        # mid-flight nor race ``get_session``'s scope closing the session under
        # it (issue #400 round-2 finding 2 -- the shared boundary helper), and so
        # no held writer awaits the lock a drain tick may hold and need to write.
        await rollback_to_completion(session)
        # Drop the pre-lock identity map too: the discarded optimistic
        # ``_upsert_user`` object shares this account's primary key with the row
        # the fresh in-lock reads below load, and a lingering stale copy would
        # collide in the identity map (SAWarning, replaced-on-flush). Expunging
        # after the rollback lets the locked tail read a truly clean slate.
        session.expunge_all()
        async with deps.secret_rotation_lock.value:
            # Holding the lock, any repoint is either fully done (committed +
            # bumped, lock released) or not yet started -- never mid-flight under
            # us. Confirm the no-retire classification against the COMMITTED
            # token (finding 1): a concurrent rotation may have committed a
            # DIFFERENT value while we computed access lockless, and overwriting
            # it here would retire it OUTSIDE the redaction protocol. If it
            # moved, re-classify from the committed basis -- the next pass takes
            # the rotation branch, which retires that value properly.
            committed = await find_user_by_plex_id(session, account.plex_id)
            if committed is not None:
                committed_token = committed.encrypted_plex_token
                if committed_token is not None and committed_token != body.auth_token:
                    old_token = committed_token
                    user = committed  # a persistent row the rotation branch can refresh
                    continue
            # Re-check the identity generation: a move means a repoint committed
            # a new configured server while we computed access, so recompute
            # against that fully-committed state (fails closed on lost access;
            # demotes on lost ownership). No move means our mint serializes
            # before any later repoint's revoke sweep, which then revokes our
            # just-minted session like any other.
            if deps.plex_identity_generation.value != identity_generation:
                await _recompute_access_in_lock()
            # Re-stage everything the pre-lock rollback discarded, in the lock's
            # fresh transaction: the client identifier (create-once idempotent)
            # and the user row + demotion flag re-derived from the (possibly
            # recomputed) access decision.
            await SettingsStore(session).set_if_absent(_CLIENT_ID_SETTING, client_identifier)
            user, demoted = await _upsert_user(
                session,
                account_id=account.plex_id,
                username=account.username,
                is_admin=is_admin,
            )
            _apply_signin_fields(user, account, permissions=user.permissions, token=body.auth_token)
            await _issue_browser_session(session, response, request=request, user_id=user.id)
        # Post-commit (``expire_on_commit=False`` keeps ``user``/``demoted``
        # live): close the demoted user's realtime streams AFTER the downgrade is
        # durable so no reconnect re-reads the old admin permissions.
        _close_demoted_streams()
        break
    else:
        # The stored token changed under us on every attempt (a pathological,
        # endlessly-racing store): fail CLOSED rather than spin or risk an
        # unredacted overwrite. The account's existing session/credential is
        # untouched; the client can simply retry.
        raise AppError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="sign_in_retry_exhausted",
            message="Sign-in kept racing a credential change. Please try again.",
            hint="Retry the sign-in in a moment.",
        )
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
    # Serialize the key check + session mint against the app-key rotate/revoke
    # critical section on the SAME ``app_key_rotate_lock`` those endpoints hold (issue
    # #293 P2). Without it the exchange could validate ``provided`` against the OLD
    # key, a concurrent rotate could then commit a new key while bulk-revoking only
    # the recovery sessions that EXISTED at that moment, and this exchange would go on
    # to insert a fresh recovery session minted from the now-stale key — a session that
    # outlives the key it was born from. Holding the lock across the re-read, the
    # ``api_key_matches`` check, and ``_issue_browser_session``'s insert+commit makes
    # the two orderings mutually exclusive: if rotate wins the lock, the re-read below
    # sees the NEW key and we reject; if the exchange wins, its session is committed
    # BEFORE rotate runs, so rotate's bulk-revoke sweeps it up like any other. The
    # ``load_system_settings`` here is the first DB read on this request's session
    # (``_throttle_sign_in`` is pure in-memory), so it opens a fresh transaction inside
    # the lock and observes any rotation that already committed.
    async with app_key_rotate_lock:
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
    # ``via_api_key_header=True``: the header itself authenticated this exchange (the
    # cookie it just minted has authenticated nothing yet). Later requests ride the
    # cookie and get ``False`` from ``_session_auth_context``.
    return _me_response(
        AuthContext(method=AuthMethod.api_key, is_admin=True, via_api_key_header=True)
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
# Admin session management (issue #56)
# --------------------------------------------------------------------------- #
@router.get("/sessions")
async def list_active_sessions_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_admin)],
) -> ActiveSessionsResponse:
    """List every active browser session an admin can see and revoke (admin-only).

    ADR-0016 sessions validate LOCALLY (plex.tv is never on the per-request
    path), so a removed or demoted user keeps access until their session is
    revoked — this is the operator's web-operable view of who is currently signed
    in, the companion to :func:`revoke_user_sessions_endpoint`. "Active" mirrors
    the auth path exactly: not revoked, not past the absolute ``expires_at`` cap,
    and not idled out past :data:`session_lifecycle.SESSION_IDLE_WINDOW`.

    Recovery (``X-Api-Key``-exchange) sessions have no Plex identity
    (``user_id`` NULL), so they cannot appear as a per-user row; they are
    surfaced as a single aggregated ``recovery`` group instead, and are equally
    revocable (issue #56). This keeps the list honest: a break-glass admin cookie
    is visible and cuttable, not an invisible standing grant.
    """
    now = datetime.now(UTC)
    idle_cutoff = now - session_lifecycle.SESSION_IDLE_WINDOW
    active_session = (
        AuthSession.revoked_at.is_(None),
        AuthSession.expires_at > now,
        func.coalesce(AuthSession.last_seen_at, AuthSession.created_at) > idle_cutoff,
    )
    result = await session.execute(
        select(
            User.id,
            User.plex_id,
            User.username,
            User.permissions,
            func.count(AuthSession.id),
            func.max(AuthSession.last_seen_at),
        )
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(*active_session)
        .group_by(User.id, User.plex_id, User.username, User.permissions)
        .order_by(User.username)
    )
    users = [
        ActiveSessionUser(
            user_id=user_id,
            plex_id=plex_id,
            username=username,
            is_admin=permissions > 0,
            session_count=count,
            last_seen_at=session_lifecycle.ensure_utc(last_seen) if last_seen is not None else None,
            is_current_user=context.user_id == user_id,
        )
        for user_id, plex_id, username, permissions, count, last_seen in result.all()
    ]
    recovery_row = (
        await session.execute(
            select(
                func.count(AuthSession.id),
                func.max(AuthSession.last_seen_at),
            ).where(AuthSession.user_id.is_(None), *active_session)
        )
    ).one()
    recovery_count, recovery_last_seen = recovery_row
    recovery = (
        RecoverySessionGroup(
            session_count=recovery_count,
            last_seen_at=(
                session_lifecycle.ensure_utc(recovery_last_seen)
                if recovery_last_seen is not None
                else None
            ),
        )
        if recovery_count
        else None
    )
    return ActiveSessionsResponse(users=users, recovery=recovery)


@router.post("/sessions/revoke")
async def revoke_user_sessions_endpoint(
    body: RevokeSessionsRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_admin)],
) -> RevokeSessionsResponse:
    """Revoke a batch of active sessions on demand (admin-only).

    The web-operable lever issue #56 asks for: today only the automatic
    mass-revoke-on-verified-repoint exists, which is a different mechanism. Two
    targets, discriminated by ``body.kind``:

    * ``"user"`` stamps ``revoked_at`` on all of ``body.user_id``'s still-active
      sessions, then closes that user's open realtime streams so a demoted admin's
      SSE cannot keep delivering admin topics past revocation (same family as
      issue #183). Their next request re-authenticates and 401s.
    * ``"recovery"`` does the same for every active recovery session (the
      ``POST /auth/api-key`` cookies with no Plex identity), closing the matching
      ``api_key`` realtime streams. Rotation of the recovery KEY is a separate
      mechanism (PR #319); this only cuts existing recovery cookies.

    Both use the auditable-revoke convention (rows survive for the sweep to
    reclaim). No self-lockout footgun by design: an admin MAY revoke their own
    account's sessions (``is_current_user`` flags it in the list), or the recovery
    session they are riding — either simply signs the current operator out, never a
    permanent lockout, since Plex sign-in (and the recovery key) can always mint a
    fresh session (north star #1). A re-revoke or an empty target is a harmless
    ``revoked: 0``.
    """
    if body.kind == "recovery":
        revoked = await session_lifecycle.revoke_recovery_sessions(session)
        await session.commit()
        if revoked:
            close_realtime_streams(
                request.app,
                reason="sessions_revoked",
                auth_method=AuthMethod.api_key.value,
            )
        return RevokeSessionsResponse(revoked=revoked)
    # kind == "user": the request validator guarantees user_id is set. Re-narrow
    # for the type checker with an honest guard rather than an assert.
    user_id = body.user_id
    if user_id is None:  # pragma: no cover - guaranteed by RevokeSessionsRequest
        raise AppError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="invalid_revoke_target",
            message="A user-session revoke needs a user_id.",
        )
    revoked = await session_lifecycle.revoke_user_sessions(session, user_id)
    await session.commit()
    if revoked:
        close_realtime_streams(
            request.app,
            reason="sessions_revoked",
            auth_method=AuthMethod.plex_session.value,
            user_id=body.user_id,
        )
    return RevokeSessionsResponse(revoked=revoked)


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


def _evict_stale_sign_in_throttle_keys(now: float) -> None:
    """Drop keys whose complete attempt history is outside the active window.

    Runs the full-dict scan at most once per window (amortized O(1) per request):
    the throttle guards the unauthenticated sign-in endpoints on the event loop,
    so a per-request scan would hand a many-IP flood an O(live keys) stall on the
    very endpoint the throttle protects. A stale key can therefore linger up to
    one extra window, which only costs memory — per-key admission filters its own
    timestamps and never reads other keys' bookkeeping.
    """
    if now - _last_stale_key_eviction.value < _SIGN_IN_WINDOW_SECONDS:
        return
    _last_stale_key_eviction.value = now
    window_start = now - _SIGN_IN_WINDOW_SECONDS
    # Attempt lists are appended in ``time.monotonic`` order, so the last stamp is
    # the newest: a key is stale exactly when that one stamp has aged out — an O(1)
    # check per key instead of scanning every timestamp.
    stale_keys = [
        key
        for key, attempts in _sign_in_attempts.items()
        if not attempts or attempts[-1] <= window_start
    ]
    for key in stale_keys:
        del _sign_in_attempts[key]


def _throttle_sign_in(request: Request) -> None:
    """Reject a client IP that exceeds the sliding-window sign-in budget."""
    now = time.monotonic()
    _evict_stale_sign_in_throttle_keys(now)
    key = _sign_in_throttle_key(request)
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
    # Rewind the eviction cadence too: throttle tests pin ``time.monotonic`` to
    # small fake values, and a real (large) timestamp left behind by an earlier
    # test would silently disable eviction for the whole faked-clock test.
    _last_stale_key_eviction.value = float("-inf")


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


def _apply_signin_fields(user: User, account: PlexAccount, *, permissions: int, token: str) -> None:
    """Apply EVERY user-row field a sign-in writes — the single source of truth.

    Both sign-in shapes funnel through here: the ordinary path applies it once
    after :func:`_upsert_user`, and the token-rotation path applies it AGAIN
    inside the ADR-0026 boundary's fresh transaction (issue #374), whose
    in-lock rollback discards all pre-lock staging. Keeping the field list in
    one function means a future user-row field cannot be added to one path and
    silently dropped from the other — add it here and both paths carry it.
    (:func:`_upsert_user` also stages ``username``/``permissions`` while
    computing the demotion flag; re-applying the same values here is a no-op.)
    """
    user.username = account.username
    user.permissions = permissions
    user.email = account.email
    user.avatar_url = account.avatar_url
    user.encrypted_plex_token = token
    user.last_login = datetime.now(UTC)


async def _upsert_user(
    session: AsyncSession,
    *,
    account_id: int,
    username: str,
    is_admin: bool,
) -> tuple[User, bool]:
    """Create-or-update the ``User`` row for this verified Plex account.

    Returns the row and whether this sign-in DEMOTED the account (its permissions
    dropped). The demotion write is only *staged* here; the caller commits it (via
    the session mint) and is responsible for closing the demoted user's realtime
    streams AFTER that commit — see the ordering note below.

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

    Permission DOWNGRADE closes realtime streams (issue #183), but the close must
    run AFTER the demotion commits, not here: this function only stages the
    ``permissions`` write, which is not persisted until the caller's session mint
    commits. Closing streams before that commit leaves a window where a fast
    reconnect re-reads the OLD (still-admin) permissions and resubscribes to admin
    topics with no second close to catch it. So we report the demotion and let the
    caller close post-commit, guaranteeing no admin stream survives the downgrade.
    A brand-NEW user (the create path) has no prior authority to lose, so it never
    reports a demotion.
    """
    new_permissions = 1 if is_admin else 0
    user = await find_user_by_plex_id(session, account_id)
    if user is None:
        created = User(plex_id=account_id, username=username, permissions=new_permissions)
        session.add(created)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            user = await find_user_by_plex_id(session, account_id)
            if user is None:  # pragma: no cover - the conflicting row must exist
                raise
        else:
            return created, False
    previous_permissions = user.permissions
    user.username = username
    user.permissions = new_permissions
    return user, new_permissions < previous_permissions


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
class _StagedSession(NamedTuple):
    """A minted-but-uncommitted browser session's cookie material."""

    raw_token: str
    csrf_token: str
    expires_at: datetime


def _stage_browser_session(session: AsyncSession, *, user_id: int | None) -> _StagedSession:
    """Stage (no commit) an ``auth_sessions`` row and return its cookie material.

    Split from :func:`_issue_browser_session` so the token-rotation sign-in path
    (issue #374) can stage the session INSIDE the ADR-0026 rotation boundary's
    single transaction — the boundary owns the commit there — while every other
    caller keeps the stage-commit-cookies composition below. Only the SHA-256
    hash of the random token is stored; the raw token rides the HTTP-only cookie.
    """
    raw_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=_SESSION_DAYS)
    session.add(
        AuthSession(
            user_id=user_id,
            token_hash=hash_session_token(raw_token),
            expires_at=expires_at,
            last_seen_at=datetime.now(UTC),
        )
    )
    return _StagedSession(raw_token, csrf_token, expires_at)


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
    ``None`` (an admin recovery session with no Plex identity, CodeQL #263). The
    commit flushes any caller-pending writes too (the Plex path stages user-row
    updates before calling this).
    """
    staged = _stage_browser_session(session, user_id=user_id)
    await session.commit()
    _set_session_cookies(
        response,
        request=request,
        session_token=staged.raw_token,
        csrf_token=staged.csrf_token,
        expires_at=staged.expires_at,
    )


# --------------------------------------------------------------------------- #
# Cookie mechanics (unchanged contract: HTTP-only session + readable CSRF)
# --------------------------------------------------------------------------- #
def _cookie_secure(request: Request) -> bool:
    """Whether the session/CSRF cookies carry the ``Secure`` attribute.

    An explicit ``auth_cookie_secure`` override wins; otherwise the flag follows
    the request scheme as the ASGI server reports it, so a plain-http LAN install
    does not set ``Secure`` (the browser would silently refuse to send the cookie
    back). The app itself never reads ``X-Forwarded-Proto``; whether the server
    layer honors it is deployment-dependent (uvicorn trusts loopback peers only,
    by default), so TLS-terminating proxies must set the explicit override.
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
