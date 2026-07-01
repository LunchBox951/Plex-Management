from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from plex_manager.config import get_settings


def _upgrade(db_path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        get_settings.cache_clear()


def test_alembic_upgrade_head_builds_sqlite_schema_with_partial_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "migrated.db"
    _upgrade(db_path, "head", monkeypatch)

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
            lock_table = conn.execute(
                text("SELECT name FROM sqlite_master WHERE name = 'request_dedup_locks'")
            ).scalar_one()
            assert lock_table == "request_dedup_locks"
    finally:
        engine.dispose()


def test_import_blocked_status_migration_rejects_legacy_duplicate_completed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "legacy-duplicates.db"
    _upgrade(db_path, "f679b4c17194", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO media_requests (tmdb_id, media_type, title, status)
                    VALUES
                        (4242, 'movie', 'Existing One', 'completed'),
                        (4242, 'movie', 'Existing Two', 'completed')
                    """
                )
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="duplicate media_requests"):
        _upgrade(db_path, "41d427bd38e6", monkeypatch)
