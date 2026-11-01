"""Review metadata must preserve downloads and their active-slot uniqueness guard."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from plex_manager.config import get_settings


def test_review_migration_preserves_existing_downloads_and_unique_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "review.db"
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    get_settings.cache_clear()
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "2efdfe4b6871")
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO downloads (torrent_hash,status) VALUES ('retained','downloading')"
            )
            original_index = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='uq_downloads_active_request'"
            ).fetchone()
        for revision in ("head", "2efdfe4b6871", "head"):
            if revision == "head":
                command.upgrade(config, revision)
            else:
                command.downgrade(config, revision)
            with sqlite3.connect(path) as db:
                assert (
                    db.execute(
                        "SELECT sql FROM sqlite_master WHERE name='uq_downloads_active_request'"
                    ).fetchone()
                    == original_index
                )
                assert db.execute(
                    "SELECT status FROM downloads WHERE torrent_hash='retained'"
                ).fetchone() == ("downloading",)
                if revision == "head":
                    assert db.execute(
                        "SELECT import_review_reason,import_review_approved,"
                        "import_review_blocklist_id FROM downloads"
                    ).fetchone() == (None, 0, None)
    finally:
        get_settings.cache_clear()
