"""The expand-only updater coordination migration upgrades existing installs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from plex_manager.config import get_settings

_PRE_UPDATE_REVISION = "3d28d05107aa"


def _run(
    db_path: Path,
    revision: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    down: bool = False,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        if down:
            command.downgrade(Config("alembic.ini"), revision)
        else:
            command.upgrade(Config("alembic.ini"), revision)
    finally:
        get_settings.cache_clear()


def _tables(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


def test_existing_install_gains_seeded_coordinator_and_lease_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "update-coordination.db"
    _run(db_path, _PRE_UPDATE_REVISION, monkeypatch)
    assert "update_coordinator_state" not in _tables(db_path)

    _run(db_path, "head", monkeypatch)
    assert {"update_coordinator_state", "maintenance_leases"} <= _tables(db_path)
    with sqlite3.connect(db_path) as connection:
        state = connection.execute(
            "SELECT id, requested_action, action_generation, phase, "
            "last_operation, last_from_build, last_to_build, last_outcome_token_hash, "
            "last_outcome_fingerprint "
            "FROM update_coordinator_state"
        ).fetchall()
        assert state == [(1, "none", 0, "idle", None, None, None, None, None)]
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'uq_maintenance_leases_drain'"
        ).fetchone()
        assert index_sql is not None
        assert "WHERE kind = 'drain'" in str(index_sql[0])


def test_migration_downgrade_drops_only_new_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "update-coordination-down.db"
    _run(db_path, "head", monkeypatch)
    _run(db_path, _PRE_UPDATE_REVISION, monkeypatch, down=True)
    tables = _tables(db_path)
    assert "update_coordinator_state" not in tables
    assert "maintenance_leases" not in tables
    assert "media_requests" in tables
