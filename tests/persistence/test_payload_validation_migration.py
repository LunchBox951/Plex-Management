"""Existing unfinished downloads must enter validation without resuming paused clients."""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from plex_manager.config import get_settings


def test_upgrade_enrols_unfinished_downloads_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "payload.db"
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), "c5cf0a125f5f")
        with sqlite3.connect(path) as db:
            for status in (
                "downloading",
                "metadata_fetching",
                "client_missing",
                "import_pending",
                "import_blocked",
                "importing",
                "imported",
                "failed",
            ):
                db.execute(
                    "INSERT INTO downloads (torrent_hash, status) VALUES (?, ?)", (status, status)
                )
        command.upgrade(Config("alembic.ini"), "head")
        with sqlite3.connect(path) as db:
            rows = db.execute(
                "SELECT status, payload_pending, payload_resume FROM downloads"
            ).fetchall()
        assert all(
            pending == (status not in {"failed", "imported"}) and resume == 0
            for status, pending, resume in rows
        )
    finally:
        get_settings.cache_clear()
