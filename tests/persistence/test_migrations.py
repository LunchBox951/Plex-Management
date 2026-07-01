"""The Alembic migration chain actually runs against SQLite."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from plex_manager.config import get_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The last revision before TV support — an existing install would be at (at least)
# this point when the TV migration first runs.
_PRE_TV_REVISION = "41d427bd38e6"


def _alembic(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PLEX_MANAGER_DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    # Run alembic via the venv interpreter (-m) with fixed, test-controlled args.
    return subprocess.run(  # noqa: S603 — args are constants, not untrusted input
        [sys.executable, "-m", "alembic", *args],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _tables_and_download_cols(db: Path) -> tuple[set[str], set[str]]:
    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(downloads)")}
    finally:
        con.close()
    return tables, cols


def _media_request_cols(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(media_requests)")}
    finally:
        con.close()


def test_migration_chain_upgrades_head_and_downgrades_base(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr

    tables, dl_cols = _tables_and_download_cols(db)
    assert "season_requests" in tables
    assert {"season", "episodes_json"} <= dl_cols

    # Operability beta (ADR-0012, migration ``6c7fca1436d8``) — this (and the
    # existing-install regression below) are the ONLY tests that actually run
    # that migration through Alembic rather than via ``Base.metadata.create_all``.
    assert "log_events" in tables
    assert {"library_path", "keep_forever"} <= _media_request_cols(db)

    down = _alembic(db, "downgrade", "base")
    assert down.returncode == 0, down.stderr


def test_existing_install_upgrades_across_the_tv_revision(tmp_path: Path) -> None:
    """An install already at the pre-TV revision must upgrade cleanly across the TV
    and operability migrations."""
    db = tmp_path / "existing.db"
    stamp = _alembic(db, "upgrade", _PRE_TV_REVISION)
    assert stamp.returncode == 0, stamp.stderr

    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr

    tables, dl_cols = _tables_and_download_cols(db)
    assert "season_requests" in tables
    assert {"season", "episodes_json"} <= dl_cols

    assert "log_events" in tables
    assert {"library_path", "keep_forever"} <= _media_request_cols(db)


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
