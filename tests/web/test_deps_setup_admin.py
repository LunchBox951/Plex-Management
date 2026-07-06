"""``require_setup_admin`` — the single dependency every setup endpoint (except
``/status``) leans on: dev-bypass short-circuit, then an optional pre-init
hardening token, then session-or-api-key auth, then an admin check.

Like ``test_error_envelope``, this suite builds its OWN bare ``FastAPI()`` app
mounting one guarded route and installs the error-envelope handlers so the
``AppError`` bodies can be asserted. It never imports ``web.app`` (transitionally
unimportable while the auth router is mid-rewrite) or the create_app-based
fixtures in ``conftest`` — only the DB/session plumbing is rebuilt locally.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from plex_manager.config import get_settings
from plex_manager.db import Base, enable_sqlite_fk_enforcement, get_session
from plex_manager.models import AuthSession, SystemSettings, User
from plex_manager.web.deps import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    SETUP_TOKEN_HEADER_NAME,
    AuthContext,
    hash_session_token,
    require_setup_admin,
)
from plex_manager.web.errors import install_error_handlers

SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "s3cr3t-app-key"
_SESSION_TOKEN = "browser-session-token"  # noqa: S105 — a test cookie value, not a credential
_CSRF_TOKEN = "csrf-token-value"  # noqa: S105 — a test CSRF value, not a credential


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """An in-memory async SQLite engine with the full schema created."""
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    enable_sqlite_fk_enforcement(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def sessionmaker_(engine: AsyncEngine) -> SessionMaker:
    """An ``AsyncSession`` factory bound to the in-memory engine."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def guarded_app(sessionmaker_: SessionMaker) -> FastAPI:
    """A bare app with one ``POST /guarded`` route behind ``require_setup_admin``.

    A POST (not a GET) so the session path's CSRF enforcement is exercised the way
    a real setup mutation would hit it. The route echoes the auth method so a 200
    also proves WHICH credential branch admitted the request.
    """
    app = FastAPI()
    install_error_handlers(app)

    async def guarded(
        context: Annotated[AuthContext, Depends(require_setup_admin)],
    ) -> dict[str, str]:
        return {"method": context.method.value}

    # Register by name (not the ``@app.post`` decorator) so the handler counts as
    # referenced under strict pyright's reportUnusedFunction.
    app.add_api_route("/guarded", guarded, methods=["POST"])

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker_() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    return app


def _client(app: FastAPI, *, cookies: dict[str, str] | None = None) -> httpx.AsyncClient:
    # Cookies are set on the client instance (not per-request) — httpx deprecates
    # per-request cookies, and pristine test output is a gate here.
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies
    )


async def _seed_system(sessionmaker_: SessionMaker, *, initialized: bool) -> None:
    async with sessionmaker_() as session:
        session.add(SystemSettings(initialized=initialized))
        await session.commit()


async def _seed_system_with_key(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        session.add(SystemSettings(initialized=True, app_api_key=_API_KEY))
        await session.commit()


async def _seed_admin_session(
    sessionmaker_: SessionMaker, *, permissions: int, initialized: bool = False
) -> None:
    async with sessionmaker_() as session:
        session.add(SystemSettings(initialized=initialized))
        user = User(plex_id=42, username="owner", permissions=permissions)
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(_SESSION_TOKEN),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()


async def test_uninitialized_no_session_is_session_required(
    guarded_app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_system(sessionmaker_, initialized=False)
    async with _client(guarded_app) as client:
        res = await client.post("/guarded")
    assert res.status_code == 401
    body = res.json()
    assert body["detail"] == "session_required"
    assert body["message"] == "Sign in with Plex to continue setup."


async def test_uninitialized_admin_session_is_allowed(
    guarded_app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_admin_session(sessionmaker_, permissions=1)
    cookies = {SESSION_COOKIE_NAME: _SESSION_TOKEN, CSRF_COOKIE_NAME: _CSRF_TOKEN}
    async with _client(guarded_app, cookies=cookies) as client:
        res = await client.post("/guarded", headers={CSRF_HEADER_NAME: _CSRF_TOKEN})
    assert res.status_code == 200
    assert res.json() == {"method": "plex_session"}


async def test_session_without_csrf_header_is_honest_envelope(
    guarded_app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    """A session mutation missing its CSRF header answers the AppError envelope.

    The double-submit check must surface an honest ``csrf_token_required`` envelope
    (``detail`` + ``message`` + ``hint``), never a bare ``{"detail": ...}`` — an
    operator sees what happened and what to do (north star #3).
    """
    await _seed_admin_session(sessionmaker_, permissions=1)
    # Valid session + CSRF cookies, but NO matching X-CSRF-Token header.
    cookies = {SESSION_COOKIE_NAME: _SESSION_TOKEN, CSRF_COOKIE_NAME: _CSRF_TOKEN}
    async with _client(guarded_app, cookies=cookies) as client:
        res = await client.post("/guarded")
    assert res.status_code == 403
    body = res.json()
    assert body["detail"] == "csrf_token_required"
    assert body["message"] == "The request was blocked by CSRF protection."
    assert body["hint"] == "Refresh the page and try again."


async def test_uninitialized_non_admin_session_is_forbidden(
    guarded_app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_admin_session(sessionmaker_, permissions=0)
    cookies = {SESSION_COOKIE_NAME: _SESSION_TOKEN, CSRF_COOKIE_NAME: _CSRF_TOKEN}
    async with _client(guarded_app, cookies=cookies) as client:
        res = await client.post("/guarded", headers={CSRF_HEADER_NAME: _CSRF_TOKEN})
    assert res.status_code == 403
    body = res.json()
    assert body["detail"] == "admin_required"
    assert body["message"] == "This action needs an administrator."


async def test_configured_setup_token_gates_before_session_check(
    guarded_app: FastAPI, sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_system(sessionmaker_, initialized=False)
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
    get_settings.cache_clear()

    async with _client(guarded_app) as client:
        missing = await client.post("/guarded")
        assert missing.status_code == 401
        body = missing.json()
        assert body["detail"] == "invalid_setup_token"
        assert body["message"] == "The setup token is missing or wrong."
        assert body["hint"] == "Check PLEX_MANAGER_SETUP_TOKEN on the server."

        # A valid token clears the pre-init gate and falls through to the session
        # check, which now fails for the still-missing session (a DIFFERENT 401).
        with_token = await client.post("/guarded", headers={SETUP_TOKEN_HEADER_NAME: "boot-token"})
        assert with_token.status_code == 401
        assert with_token.json()["detail"] == "session_required"


async def test_initialized_api_key_is_allowed(
    guarded_app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_system_with_key(sessionmaker_)
    async with _client(guarded_app) as client:
        res = await client.post("/guarded", headers={"X-Api-Key": _API_KEY})
    assert res.status_code == 200
    assert res.json() == {"method": "api_key"}


async def test_dev_auth_bypass_short_circuits(
    guarded_app: FastAPI, sessionmaker_: SessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_system(sessionmaker_, initialized=False)
    monkeypatch.setenv("PLEX_MANAGER_DEV_AUTH_BYPASS", "1")
    get_settings.cache_clear()

    async with _client(guarded_app) as client:
        res = await client.post("/guarded")
    assert res.status_code == 200
    assert res.json() == {"method": "dev_bypass"}
