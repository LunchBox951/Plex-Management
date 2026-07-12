"""``season_episode_states`` migration (ADR-0020, issue #178): table + backfill.

Mirrors ``test_migrations.py``'s subprocess/command-based upgrade/downgrade
pattern.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from plex_manager.config import get_settings

_PRE_EPISODE_STATES_REVISION = "c86212dad733"


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


def test_migration_creates_table_and_unique_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "season-episode-states.db"
    _upgrade(db_path, "head", monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "season_episode_states" in tables
        cols = {r[1] for r in con.execute("PRAGMA table_info(season_episode_states)")}
        assert {
            "id",
            "season_request_id",
            "episode_number",
            "status",
            "air_date",
            "grabbed_download_id",
        } <= cols
        index_names = {
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='season_episode_states'"
            )
        }
        assert "uq_season_episode_states_season_episode" in index_names
    finally:
        con.close()


def test_migration_backfills_imported_episodes_from_download_episodes_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Last-Man-on-Earth-S4E07 case: an already-imported single-episode
    download must count as ``imported`` after the migration, with no re-download
    triggered."""
    db_path = tmp_path / "backfill.db"
    _upgrade(db_path, _PRE_EPISODE_STATES_REVISION, monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO media_requests (id, tmdb_id, media_type, title, status)
                    VALUES (1, 42, 'tv', 'The Last Man on Earth', 'downloading')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO season_requests (id, media_request_id, season_number, status)
                    VALUES (10, 1, 4, 'downloading')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO downloads (
                        torrent_hash, status, progress, seed_ratio,
                        media_request_id, tmdb_id, season, episodes_json, media_type
                    )
                    VALUES (
                        'tlmoe_s4e07', 'imported', 1.0, 1.0, 1, 42, 4, '[7]', 'tv'
                    )
                    """
                )
            )
    finally:
        engine.dispose()

    _upgrade(db_path, "head", monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT episode_number, status FROM season_episode_states WHERE season_request_id = 10"
        ).fetchall()
    finally:
        con.close()

    assert rows == [(7, "imported")]


def test_migration_seeds_no_rows_for_whole_season_pack_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-season pack import (``episodes_json IS NULL``) enumerates no
    specific episodes -- the migration must NOT synthesize a target from TMDB
    offline; the season reads as "target unknown" until the next auto-grab
    cycle refreshes it."""
    db_path = tmp_path / "pack-backfill.db"
    _upgrade(db_path, _PRE_EPISODE_STATES_REVISION, monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO media_requests (id, tmdb_id, media_type, title, status)
                    VALUES (1, 99, 'tv', 'Some Show', 'completed')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO season_requests (id, media_request_id, season_number, status)
                    VALUES (20, 1, 1, 'completed')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO downloads (
                        torrent_hash, status, progress, seed_ratio,
                        media_request_id, tmdb_id, season, episodes_json, media_type
                    )
                    VALUES (
                        'season_pack_hash', 'imported', 1.0, 1.0, 1, 99, 1, NULL, 'tv'
                    )
                    """
                )
            )
    finally:
        engine.dispose()

    _upgrade(db_path, "head", monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT episode_number FROM season_episode_states WHERE season_request_id = 20"
        ).fetchall()
    finally:
        con.close()

    assert rows == []


def test_migration_downgrade_drops_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "downgrade.db"
    _upgrade(db_path, "head", monkeypatch)
    _downgrade(db_path, _PRE_EPISODE_STATES_REVISION, monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "season_episode_states" not in tables
