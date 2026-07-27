"""``season_requests.completed_at`` migration (issue #494): column, no backfill.

Mirrors ``test_season_episode_states_migration.py``'s command-based
upgrade/downgrade pattern.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from plex_manager.config import get_settings

_PRE_SEASON_COMPLETED_AT_REVISION = "111b3b3c67fb"


def _alembic_to(db_path: Path, revision: str, monkeypatch: pytest.MonkeyPatch) -> None:
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


def _season_columns(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(season_requests)")}
    finally:
        con.close()


def test_migration_adds_the_completion_generation_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "season-completed-at.db"
    _alembic_to(db_path, "head", monkeypatch)
    assert "completed_at" in _season_columns(db_path)

    _downgrade(db_path, _PRE_SEASON_COMPLETED_AT_REVISION, monkeypatch)
    assert "completed_at" not in _season_columns(db_path)


def test_migration_leaves_existing_completed_seasons_unstamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No backfill, by design: the completion generation of a season that
    completed BEFORE this column existed was never recorded, and synthesizing
    one would fabricate a completion instant (honesty over silence). ``NULL`` is
    safe in both directions -- the availability pass snapshots that ``NULL`` and
    the CAS matches only a row that is still ``NULL``, while any re-completion
    after the upgrade stamps a generation that no longer matches it."""
    db_path = tmp_path / "season-completed-at-legacy.db"
    _alembic_to(db_path, _PRE_SEASON_COMPLETED_AT_REVISION, monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO media_requests (id, tmdb_id, media_type, title, status)
                    VALUES (1, 42, 'tv', 'Some Show', 'completed')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO season_requests (id, media_request_id, season_number, status)
                    VALUES (10, 1, 1, 'completed')
                    """
                )
            )
    finally:
        engine.dispose()

    _alembic_to(db_path, "head", monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT status, completed_at FROM season_requests").fetchall()
    finally:
        con.close()

    assert rows == [("completed", None)]
