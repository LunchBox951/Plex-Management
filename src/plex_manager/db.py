"""Database foundation.

The schema is owned by versioned Alembic migrations (see ADR-0007); ORM models
build on the :class:`Base` declared here. The application talks to the database
asynchronously; Alembic runs its migrations synchronously against the URL
returned by :func:`sync_database_url`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from plex_manager.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Return the process-wide async session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a database session scoped to a single request (FastAPI dependency)."""
    async with get_sessionmaker()() as session:
        yield session


def sync_database_url(async_url: str) -> str:
    """Translate the app's async SQLAlchemy URL into the sync URL Alembic uses.

    Migrations run synchronously, so the async driver suffix is stripped
    (``sqlite+aiosqlite://`` -> ``sqlite://``, ``postgresql+asyncpg://`` ->
    ``postgresql://``). A URL that is already synchronous is returned unchanged.
    """
    replacements = {
        "sqlite+aiosqlite://": "sqlite://",
        "postgresql+asyncpg://": "postgresql://",
    }
    for async_prefix, sync_prefix in replacements.items():
        if async_url.startswith(async_prefix):
            return sync_prefix + async_url[len(async_prefix) :]
    return async_url
