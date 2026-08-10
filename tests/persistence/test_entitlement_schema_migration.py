"""``users`` entitlement/share-state columns migration (issue #391 PR-1).

Mirrors ``test_season_episode_states_migration.py``'s command-based
upgrade/downgrade pattern.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from plex_manager.config import get_settings

_PRE_ENTITLEMENT_REVISION = "a41f9c7d20be"


def _upgrade(db_path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        command.upgrade(Config("alembic.ini"), revision)
    finally:
        get_settings.cache_clear()


def _downgrade(db_path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLEX_MANAGER_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    try:
        command.downgrade(Config("alembic.ini"), revision)
    finally:
        get_settings.cache_clear()


def _users_columns(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(users)")}
    finally:
        con.close()


def _users_indexes(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='users'"
            )
        }
    finally:
        con.close()


_NEW_COLUMNS = {
    "entitled_section_keys",
    "entitlements_machine_id",
    "share_state",
    "share_checked_at",
    "share_check_failures",
    "share_check_failed_at",
}
_NEW_INDEXES = {"ix_users_share_state", "ix_users_share_checked_at"}


def test_migration_adds_six_columns_and_two_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "entitlement-fresh.db"
    _upgrade(db_path, "head", monkeypatch)

    assert _users_columns(db_path) >= _NEW_COLUMNS
    assert _users_indexes(db_path) >= _NEW_INDEXES


def test_migration_share_check_failures_defaults_to_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "entitlement-default.db"
    _upgrade(db_path, "head", monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT INTO users (username) VALUES ('watcher')")
        con.commit()
        row = con.execute(
            "SELECT share_check_failures, entitled_section_keys, share_state "
            "FROM users WHERE username = 'watcher'"
        ).fetchone()
    finally:
        con.close()

    # share_check_failures defaults to 0 (server_default); the rest stay
    # honestly NULL -- nothing has ever checked this row.
    assert row == (0, None, None)


def test_migration_existing_install_upgrades_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An install already at the pre-entitlement revision upgrades cleanly, and
    a pre-existing user row picks up the new columns as NULL/0 -- no backfill
    fabricates history for a row that predates the migration."""
    db_path = tmp_path / "entitlement-existing.db"
    _upgrade(db_path, _PRE_ENTITLEMENT_REVISION, monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT INTO users (id, username) VALUES (1, 'preexisting')")
        con.commit()
    finally:
        con.close()

    _upgrade(db_path, "head", monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT share_state, share_check_failures, entitled_section_keys "
            "FROM users WHERE id = 1"
        ).fetchone()
    finally:
        con.close()
    assert row == (None, 0, None)


def test_migration_downgrade_drops_all_six_columns_and_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "entitlement-downgrade.db"
    _upgrade(db_path, "head", monkeypatch)
    _downgrade(db_path, _PRE_ENTITLEMENT_REVISION, monkeypatch)

    assert _users_columns(db_path).isdisjoint(_NEW_COLUMNS)
    assert _users_indexes(db_path).isdisjoint(_NEW_INDEXES)


def test_migration_upgrade_preserves_child_rows_and_fk_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upgrade is a plain ``ALTER TABLE ADD COLUMN`` (no ``users`` rebuild),
    so rows in child tables referencing ``users.id`` must survive untouched and
    the foreign-key graph must stay intact. Guards against a regression back to
    batch mode, whose SQLite move-and-copy drops/recreates the parent table
    while ``migrations/env.py`` runs without FK enforcement."""
    db_path = tmp_path / "entitlement-children.db"
    _upgrade(db_path, _PRE_ENTITLEMENT_REVISION, monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT INTO users (id, username) VALUES (7, 'parent')")
        con.execute(
            "INSERT INTO auth_sessions (user_id, token_hash, expires_at) "
            "VALUES (7, ?, '2099-01-01 00:00:00')",
            ("a" * 64,),
        )
        con.commit()
    finally:
        con.close()

    _upgrade(db_path, "head", monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        child = con.execute(
            "SELECT user_id FROM auth_sessions WHERE token_hash = ?", ("a" * 64,)
        ).fetchone()
        fk_violations = con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()
    assert child == (7,)
    assert fk_violations == []
