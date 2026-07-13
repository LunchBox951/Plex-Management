"""Admin session management + sliding idle expiry (issue #56).

``GET /api/v1/auth/sessions`` lists active-session users for an admin; ``POST
/api/v1/auth/sessions/revoke`` cuts one user's sessions on demand. Sessions also
now idle out on top of the absolute ``expires_at`` cap, and authenticated
activity slides ``last_seen_at`` forward (throttled).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from plex_manager.models import AuthSession, User
from plex_manager.services import session_lifecycle as sl
from plex_manager.web.deps import hash_session_token

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "s3cr3t-app-key"

Cookies = dict[str, str]
Headers = dict[str, str]


async def _mint_session(
    app: FastAPI,
    *,
    plex_id: int,
    tag: str,
    is_admin: bool,
    last_seen_at: datetime | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> tuple[int, Cookies, Headers]:
    """Create (or reuse) a user and attach one session; return (user_id, cookies, csrf)."""
    token = f"sess-{tag}"
    csrf = f"csrf-{tag}"
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as session:
        user = (
            (await session.execute(select(User).where(User.plex_id == plex_id))).scalars().first()
        )
        if user is None:
            user = User(
                plex_id=plex_id,
                username=f"user-{plex_id}",
                permissions=1 if is_admin else 0,
            )
            session.add(user)
            await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(token),
                expires_at=expires_at or now + timedelta(days=30),
                last_seen_at=last_seen_at if last_seen_at is not None else now,
                revoked_at=revoked_at,
            )
        )
        await session.commit()
        user_id = user.id
    return user_id, {"plexmgr.session": token, "plexmgr.csrf": csrf}, {"X-CSRF-Token": csrf}


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
async def test_list_sessions_returns_active_users(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    admin_id, admin_cookies, _ = await _mint_session(app, plex_id=100, tag="adm", is_admin=True)
    other_id, _, _ = await _mint_session(app, plex_id=200, tag="oth", is_admin=False)

    response = await client.get("/api/v1/auth/sessions", cookies=admin_cookies)
    assert response.status_code == 200
    by_id = {u["user_id"]: u for u in response.json()["users"]}
    assert set(by_id) == {admin_id, other_id}
    assert by_id[admin_id]["is_current_user"] is True
    assert by_id[admin_id]["is_admin"] is True
    assert by_id[admin_id]["session_count"] == 1
    assert by_id[other_id]["is_current_user"] is False
    assert by_id[other_id]["is_admin"] is False


async def test_list_sessions_requires_admin(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    _, cookies, _ = await _mint_session(app, plex_id=201, tag="nonadm", is_admin=False)
    response = await client.get("/api/v1/auth/sessions", cookies=cookies)
    assert response.status_code == 403
    assert response.json()["detail"] == "admin_required"


async def test_list_sessions_excludes_idle_and_revoked(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    admin_id, admin_cookies, _ = await _mint_session(app, plex_id=100, tag="adm", is_admin=True)
    # An idled-out session: last seen past the idle window.
    idle_seen = datetime.now(UTC) - sl.SESSION_IDLE_WINDOW - timedelta(days=1)
    await _mint_session(app, plex_id=300, tag="idle", is_admin=False, last_seen_at=idle_seen)
    # A revoked session.
    await _mint_session(app, plex_id=400, tag="rev", is_admin=False, revoked_at=datetime.now(UTC))

    response = await client.get("/api/v1/auth/sessions", cookies=admin_cookies)
    assert response.status_code == 200
    assert [u["user_id"] for u in response.json()["users"]] == [admin_id]


# --------------------------------------------------------------------------- #
# Revocation
# --------------------------------------------------------------------------- #
async def test_revoke_kills_target_users_session(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    _, admin_cookies, admin_csrf = await _mint_session(app, plex_id=100, tag="adm", is_admin=True)
    target_id, target_cookies, _ = await _mint_session(app, plex_id=200, tag="tgt", is_admin=False)

    revoke = await client.post(
        "/api/v1/auth/sessions/revoke",
        json={"user_id": target_id},
        cookies=admin_cookies,
        headers=admin_csrf,
    )
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] == 1

    # The target's cookie no longer authenticates.
    me = await client.get("/api/v1/auth/me", cookies=target_cookies)
    assert me.json()["authenticated"] is False


async def test_admin_can_revoke_own_sessions(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    admin_id, admin_cookies, admin_csrf = await _mint_session(
        app, plex_id=100, tag="adm", is_admin=True
    )
    revoke = await client.post(
        "/api/v1/auth/sessions/revoke",
        json={"user_id": admin_id},
        cookies=admin_cookies,
        headers=admin_csrf,
    )
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] == 1
    # No hidden lockout: the admin's own session is simply signed out.
    me = await client.get("/api/v1/auth/me", cookies=admin_cookies)
    assert me.json()["authenticated"] is False


async def test_revoke_unknown_user_is_zero(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    _, admin_cookies, admin_csrf = await _mint_session(app, plex_id=100, tag="adm", is_admin=True)
    revoke = await client.post(
        "/api/v1/auth/sessions/revoke",
        json={"user_id": 999999},
        cookies=admin_cookies,
        headers=admin_csrf,
    )
    assert revoke.status_code == 200
    assert revoke.json()["revoked"] == 0


async def test_revoke_requires_admin(client: httpx.AsyncClient, app: FastAPI, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    _, cookies, csrf = await _mint_session(app, plex_id=201, tag="nonadm", is_admin=False)
    revoke = await client.post(
        "/api/v1/auth/sessions/revoke",
        json={"user_id": 201},
        cookies=cookies,
        headers=csrf,
    )
    assert revoke.status_code == 403


async def test_revoke_without_csrf_is_rejected(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """A session-cookie caller must present the double-submit CSRF token."""
    await seed(initialized=True, app_api_key=_API_KEY)
    _, admin_cookies, _ = await _mint_session(app, plex_id=100, tag="adm", is_admin=True)
    target_id, _, _ = await _mint_session(app, plex_id=200, tag="tgt", is_admin=False)
    revoke = await client.post(
        "/api/v1/auth/sessions/revoke",
        json={"user_id": target_id},
        cookies=admin_cookies,  # no X-CSRF-Token header
    )
    assert revoke.status_code == 403
    assert revoke.json()["detail"] == "csrf_token_required"


# --------------------------------------------------------------------------- #
# Sliding idle expiry
# --------------------------------------------------------------------------- #
async def test_idle_session_is_rejected(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    idle_seen = datetime.now(UTC) - sl.SESSION_IDLE_WINDOW - timedelta(minutes=1)
    _, cookies, _ = await _mint_session(
        app, plex_id=500, tag="idle", is_admin=True, last_seen_at=idle_seen
    )
    me = await client.get("/api/v1/auth/me", cookies=cookies)
    assert me.json()["authenticated"] is False


async def test_active_request_slides_last_seen_forward(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    stale = datetime.now(UTC) - sl.SESSION_LAST_SEEN_REFRESH_INTERVAL - timedelta(minutes=1)
    user_id, cookies, _ = await _mint_session(
        app, plex_id=600, tag="slide", is_admin=True, last_seen_at=stale
    )

    me = await client.get("/api/v1/auth/me", cookies=cookies)
    assert me.json()["authenticated"] is True

    async with app.state.sessionmaker() as session:
        row = (
            (await session.execute(select(AuthSession).where(AuthSession.user_id == user_id)))
            .scalars()
            .one()
        )
    assert sl.ensure_utc(row.last_seen_at) > stale


async def test_recent_activity_does_not_rewrite_last_seen(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """The refresh is throttled: a last_seen within the interval is not rewritten."""
    await seed(initialized=True, app_api_key=_API_KEY)
    recent = datetime.now(UTC) - timedelta(minutes=5)
    user_id, cookies, _ = await _mint_session(
        app, plex_id=700, tag="fresh", is_admin=True, last_seen_at=recent
    )

    me = await client.get("/api/v1/auth/me", cookies=cookies)
    assert me.json()["authenticated"] is True

    async with app.state.sessionmaker() as session:
        row = (
            (await session.execute(select(AuthSession).where(AuthSession.user_id == user_id)))
            .scalars()
            .one()
        )
    # Unchanged — still the seeded value, no per-request write.
    assert sl.ensure_utc(row.last_seen_at) == recent
