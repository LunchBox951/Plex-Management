"""The expand-only updater coordination migration upgrades existing installs."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from plex_manager.config import get_settings

_PRE_UPDATE_REVISION = "a9c31e72b4f6"
# The revision immediately before the ADR-0025 stage 0 sidecar-observability
# columns, and that revision itself.
_PRE_SIDECAR_OBSERVABILITY_REVISION = "e91b3f7a5d24"
_SIDECAR_OBSERVABILITY_REVISION = "ec826d3aa951"
_SIDECAR_OBSERVABILITY_COLUMNS = frozenset(
    {
        "updater_observed_build",
        "updater_observed_digest",
        "last_refresh_result",
        "last_refresh_detail_code",
        "last_refresh_from_build",
        "last_refresh_to_build",
        "last_refresh_at",
    }
)


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


def _columns(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as connection:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


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
        indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "ix_update_coordinator_state_updater_last_seen_at" not in indexes
        assert "ix_maintenance_leases_kind" not in indexes
        assert "ix_maintenance_leases_kind_expires" in indexes


def test_sidecar_observability_columns_expand_and_reverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0025 stage 0: the columns are added expand-only and drop cleanly."""
    db_path = tmp_path / "sidecar-observability.db"
    _run(db_path, _PRE_SIDECAR_OBSERVABILITY_REVISION, monkeypatch)
    before = _columns(db_path, "update_coordinator_state")
    assert not (_SIDECAR_OBSERVABILITY_COLUMNS & before)

    _run(db_path, _SIDECAR_OBSERVABILITY_REVISION, monkeypatch)
    after = _columns(db_path, "update_coordinator_state")
    assert after >= _SIDECAR_OBSERVABILITY_COLUMNS
    # Expand-only: the pre-existing columns are untouched.
    assert before <= after
    # Every new column is NULL on the pre-existing seeded singleton row.
    with sqlite3.connect(db_path) as connection:
        selected = ", ".join(sorted(_SIDECAR_OBSERVABILITY_COLUMNS))
        values = connection.execute(
            f"SELECT {selected} FROM update_coordinator_state WHERE id = 1"  # noqa: S608
        ).fetchone()
    assert values is not None
    assert all(value is None for value in values)

    _run(db_path, _PRE_SIDECAR_OBSERVABILITY_REVISION, monkeypatch, down=True)
    reverted = _columns(db_path, "update_coordinator_state")
    assert not (_SIDECAR_OBSERVABILITY_COLUMNS & reverted)
    assert reverted == before


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
