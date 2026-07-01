"""URL driver coercion: sync<->async translation for engine + Alembic."""

from __future__ import annotations

import pytest
from alembic.config import Config

from plex_manager.db import alembic_database_url, async_database_url, sync_database_url


@pytest.mark.parametrize(
    ("given", "want"),
    [
        # A shipped sync URL (as in .env.example) is coerced to the async driver.
        ("sqlite:///./data/plex_manager.db", "sqlite+aiosqlite:///./data/plex_manager.db"),
        ("postgresql://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        # An already-async URL is left untouched (no double-driver suffix).
        (
            "sqlite+aiosqlite:///./data/plex_manager.db",
            "sqlite+aiosqlite:///./data/plex_manager.db",
        ),
        ("postgresql+asyncpg://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        # An unrecognised scheme passes through unchanged.
        ("mysql://u:p@host/db", "mysql://u:p@host/db"),
    ],
)
def test_async_database_url(given: str, want: str) -> None:
    assert async_database_url(given) == want


def test_async_and_sync_are_inverses_for_the_default() -> None:
    async_url = "sqlite+aiosqlite:///./data/plex_manager.db"
    assert async_database_url(sync_database_url(async_url)) == async_url


def test_alembic_database_url_escapes_percent_encoded_credentials() -> None:
    url = "postgresql+asyncpg://user:p%40ss@localhost/db"
    config = Config("alembic.ini")

    config.set_main_option("sqlalchemy.url", alembic_database_url(url))

    assert config.get_main_option("sqlalchemy.url") == "postgresql://user:p%40ss@localhost/db"
