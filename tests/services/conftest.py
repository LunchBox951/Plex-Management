"""Fixtures for the service suite: temp Fernet key + in-memory async DB."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from plex_manager.adapters import encryption
from plex_manager.config import get_settings
from plex_manager.db import Base, enable_sqlite_fk_enforcement

SessionMaker = async_sessionmaker[AsyncSession]


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject a throwaway Fernet key and reset the cached settings/fernet."""
    monkeypatch.setenv("PLEX_MANAGER_FERNET_KEY", Fernet.generate_key().decode())
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
