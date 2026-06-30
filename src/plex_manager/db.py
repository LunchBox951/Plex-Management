"""Database foundation.

The schema is owned by versioned Alembic migrations (see ADR-0007); ORM models
build on the :class:`Base` declared here. The application talks to the database
asynchronously; Alembic runs its migrations synchronously against the URL
returned by :func:`sync_database_url`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from plex_manager.config import get_settings

if TYPE_CHECKING:
    from sqlalchemy.engine.interfaces import DBAPIConnection
    from sqlalchemy.pool import ConnectionPoolEntry


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _set_sqlite_fk_pragma(
    dbapi_connection: DBAPIConnection,
    _connection_record: ConnectionPoolEntry,
) -> None:
    """Issue ``PRAGMA foreign_keys=ON`` on each new SQLite DBAPI connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def enable_sqlite_fk_enforcement(engine: AsyncEngine) -> None:
    """Make SQLite honour ``ON DELETE`` (CASCADE / SET NULL) on every connection.

    SQLite ships with foreign-key enforcement *off* by default and the setting is
    per-connection, so the schema's ``ON DELETE`` clauses are inert until each
    DBAPI connection issues ``PRAGMA foreign_keys=ON``. Without this, deleting a
    parent row neither cascades to children nor nulls referencing columns, and
    FK-violating inserts succeed silently — the integrity guarantees would be
    cosmetic. A no-op for non-SQLite dialects (Postgres enforces FKs natively).
    """
    if engine.dialect.name == "sqlite":
        event.listen(engine.sync_engine, "connect", _set_sqlite_fk_pragma)


def get_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first use."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(async_database_url(get_settings().database_url))
        enable_sqlite_fk_enforcement(_engine)
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


def async_database_url(url: str) -> str:
    """Coerce a (possibly sync) SQLAlchemy URL to the async driver the app needs.

    The reverse of :func:`sync_database_url`. :func:`get_engine` uses
    ``create_async_engine``, which requires an async driver, but the shipped
    ``.env.example`` documents a plain sync URL (``sqlite:///./data/...``). Without
    this coercion a docs-following install would fail at startup with "the
    asyncio extension requires an async driver". Mapping: ``sqlite://`` ->
    ``sqlite+aiosqlite://``, ``postgresql://`` -> ``postgresql+asyncpg://``. A URL
    that already names an async driver matches neither sync prefix and is returned
    unchanged.
    """
    replacements = {
        "sqlite://": "sqlite+aiosqlite://",
        "postgresql://": "postgresql+asyncpg://",
    }
    for sync_prefix, async_prefix in replacements.items():
        if url.startswith(sync_prefix):
            return async_prefix + url[len(sync_prefix) :]
    return url
