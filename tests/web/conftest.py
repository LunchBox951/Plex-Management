"""Fixtures for the web suite: temp Fernet key, in-memory DB, wired app + client.

The app is built with :func:`create_app` (so the real middleware and routers are
exercised), then pointed at an in-memory SQLite engine: ``get_session`` is
overridden and ``app.state.sessionmaker`` is set so the setup-guard middleware
reads the same DB. ``app.state.http_client`` is a ``MockTransport`` client so no
live network is touched. The ASGITransport client drives requests in-process.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from plex_manager.adapters import encryption
from plex_manager.config import get_settings
from plex_manager.db import Base, enable_sqlite_fk_enforcement, get_session
from plex_manager.models import SystemSettings
from plex_manager.web.app import create_app

SessionMaker = async_sessionmaker[AsyncSession]
SeedFn = Callable[..., Awaitable[None]]


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject a throwaway Fernet key via the env override and reset caches."""
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PLEX_MANAGER_FERNET_KEY", key)
    get_settings.cache_clear()
    encryption.reset_fernet_cache()
    yield
    get_settings.cache_clear()
    encryption.reset_fernet_cache()


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
def seed(sessionmaker_: SessionMaker) -> SeedFn:
    """Return a coroutine that inserts the single ``system_settings`` row."""

    async def _seed(*, initialized: bool, app_api_key: str | None = None) -> None:
        # app_api_key is EncryptedStr — the plaintext is encrypted on commit; tests
        # pass the same plaintext in the X-Api-Key header.
        async with sessionmaker_() as session:
            session.add(SystemSettings(initialized=initialized, app_api_key=app_api_key))
            await session.commit()

    return _seed


def _ok_transport() -> httpx.MockTransport:
    """A default transport that answers any request with a trivial 200."""
    return httpx.MockTransport(lambda _request: httpx.Response(200, text="ok"))


@pytest.fixture
async def app(sessionmaker_: SessionMaker) -> AsyncIterator[FastAPI]:
    """The wired FastAPI app pointed at the in-memory DB + a mock HTTP client."""
    application = create_app()
    application.state.sessionmaker = sessionmaker_
    application.state.http_client = httpx.AsyncClient(transport=_ok_transport())

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker_() as session:
            yield session

    application.dependency_overrides[get_session] = _override_session
    try:
        yield application
    finally:
        await application.state.http_client.aclose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """An httpx client driving the app in-process via ASGITransport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
