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
from sqlalchemy.exc import IntegrityError

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


def _season_request_cols(db: Path) -> set[str]:
    con = sqlite3.connect(db)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(season_requests)")}
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
    assert {
        "library_path",
        "keep_forever",
        "tv_request_mode",
        "requested_seasons_json",
    } <= _media_request_cols(db)
    assert {"installed_quality_id", "installed_profile_index"} <= _season_request_cols(db)

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
    assert {
        "library_path",
        "keep_forever",
        "tv_request_mode",
        "requested_seasons_json",
    } <= _media_request_cols(db)
    assert {"installed_quality_id", "installed_profile_index"} <= _season_request_cols(db)


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

            source_title_index = conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'ix_blocklist_source_title'"
                )
            ).scalar_one_or_none()
            assert source_title_index is None

            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        """
                        INSERT INTO media_requests (tmdb_id, media_type, title, status)
                        VALUES (1, 'not_media', 'Bad', 'pending')
                        """
                    )
                )
            conn.rollback()
            with pytest.raises(IntegrityError):
                conn.execute(
                    text(
                        """
                        INSERT INTO blocklist (source_title, reason)
                        VALUES ('Bad.Release', 'not_reason')
                        """
                    )
                )
            conn.rollback()

            conn.execute(
                text(
                    """
                    INSERT INTO download_history (event_type)
                    VALUES ('evicted')
                    """
                )
            )
            conn.rollback()
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


def test_tv_request_intent_backfills_legacy_rows_as_whole_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every pre-existing TV request is stamped ``whole_show`` with a NULL season
    set — the representation the live app uses for a request that named no finite
    season set (``request_service._tv_request_intent`` -> ``("whole_show", None)``).

    The whole-show/explicit intent bit predates the column and is unrecoverable
    from the surviving rows, so an ``explicit_seasons`` freeze would fabricate an
    intent the operator never held. For a TV row with zero ``season_requests``
    (schema-valid since the initial revision permitted ``media_type='tv'``) that
    freeze would mean an explicit request for NO seasons — rejecting every
    multi-season pack. This test pins the ``whole_show`` default and guards
    movies from being given a TV intent at all.
    """
    import json as json_lib

    db_path = tmp_path / "tv-intent-backfill.db"
    # Stop at main's head — the revision immediately before this one.
    _upgrade(db_path, "b7e2d4f6c8a1", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO media_requests (id, tmdb_id, media_type, title, status)
                    VALUES
                        (1, 42, 'tv', 'No Season Rows', 'completed'),
                        (2, 99, 'tv', 'Tracks 1-3', 'completed'),
                        (3, 7, 'movie', 'A Movie', 'completed')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO season_requests (media_request_id, season_number, status)
                    VALUES (2, 1, 'completed'), (2, 2, 'completed'), (2, 3, 'completed')
                    """
                )
            )
    finally:
        engine.dispose()

    _upgrade(db_path, "9b7a1c5d2e4f", monkeypatch)

    con = sqlite3.connect(db_path)
    try:
        rows = {
            rid: (mode, raw)
            for rid, mode, raw in con.execute(
                "SELECT id, tv_request_mode, requested_seasons_json FROM media_requests"
            )
        }
    finally:
        con.close()

    # Both legacy TV rows -> whole_show, NULL season set (reads back as None),
    # including the one that never had a season_requests row.
    for rid in (1, 2):
        mode, raw = rows[rid]
        assert mode == "whole_show", rid
        parsed = json_lib.loads(raw) if isinstance(raw, str) else raw
        assert parsed is None, rid
    # The movie is never given a TV request intent.
    assert rows[3][0] is None
