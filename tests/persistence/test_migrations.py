from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from plex_manager.config import get_settings


def test_alembic_upgrade_head_builds_sqlite_schema_with_partial_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            media_index = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'uq_media_requests_active'")
            ).scalar_one()
            download_index = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE name = 'uq_downloads_active_request'")
            ).scalar_one()

            assert "import_blocked" in media_index
            assert "completed" in media_index
            assert "status NOT IN ('imported', 'failed', 'no_acceptable_release')" in download_index
    finally:
        engine.dispose()
