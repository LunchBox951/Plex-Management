"""``completion_generation`` migration (issue #494): columns, no backfill.

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

_PRE_COMPLETION_GENERATION_REVISION = "111b3b3c67fb"


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


def _columns(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def test_migration_adds_the_completion_generation_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "completion-generation.db"
    _alembic_to(db_path, "head", monkeypatch)
    assert "completion_generation" in _columns(db_path, "media_requests")
    assert {"completed_at", "completion_generation"} <= _columns(db_path, "season_requests")

    _downgrade(db_path, _PRE_COMPLETION_GENERATION_REVISION, monkeypatch)
    assert "completion_generation" not in _columns(db_path, "media_requests")
    assert not {"completed_at", "completion_generation"} & _columns(db_path, "season_requests")


def test_migration_leaves_existing_completed_rows_unstamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No backfill, by design: the completion generation of a row that completed
    BEFORE these columns existed was never recorded, and synthesizing one would
    fabricate history (honesty over silence). ``NULL`` is safe in both
    directions -- the availability pass snapshots that ``NULL`` and the CAS
    matches only a row that is still ``NULL``, while the first bump after the
    upgrade lands on 1, which no earlier snapshot can match."""
    db_path = tmp_path / "completion-generation-legacy.db"
    _alembic_to(db_path, _PRE_COMPLETION_GENERATION_REVISION, monkeypatch)

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
        movies = con.execute("SELECT status, completion_generation FROM media_requests").fetchall()
        seasons = con.execute(
            "SELECT status, completed_at, completion_generation FROM season_requests"
        ).fetchall()
    finally:
        con.close()

    assert movies == [("completed", None)]
    assert seasons == [("completed", None, None)]
