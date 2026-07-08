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
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

import plex_manager.models as models
from plex_manager.config import get_settings
from plex_manager.db import Base

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The last revision before TV support — an existing install would be at (at least)
# this point when the TV migration first runs.
_PRE_TV_REVISION = "41d427bd38e6"
_EXPECTED_ENUM_CHECK_NAMES = {
    "blocklist": {"ck_blocklist_media_type_enum", "ck_blocklist_reason_enum"},
    "download_history": {"ck_download_history_event_type_enum"},
    "media_requests": {"ck_media_requests_media_type_enum", "ck_media_requests_status_enum"},
    "request_dedup_locks": {"ck_request_dedup_locks_media_type_enum"},
    "season_requests": {"ck_season_requests_status_enum"},
    "downloads": {"ck_downloads_media_type_enum"},
}


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


def test_fresh_schema_uses_migration_enum_check_constraint_names() -> None:
    engine = create_engine("sqlite://")
    try:
        assert models.MediaRequest.__tablename__ == "media_requests"
        Base.metadata.create_all(engine)
        inspector = inspect(engine)

        names_by_table = {
            table: {constraint["name"] for constraint in inspector.get_check_constraints(table)}
            for table in _EXPECTED_ENUM_CHECK_NAMES
        }

        assert names_by_table == _EXPECTED_ENUM_CHECK_NAMES
    finally:
        engine.dispose()


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


def test_stalled_event_type_migration_widens_the_check_constraint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #165's self-heal writes ``event_type='stalled'``; a database already
    migrated to ``bfaa63130ee7`` (pre-#165) must reject it, and upgrading across
    ``26bc01829ae1`` must then accept it — the CHECK constraint
    ``b7e2d4f6c8a1`` gave this column really does need widening, not just the
    Python-side enum."""
    db_path = tmp_path / "stalled-event.db"
    _upgrade(db_path, "bfaa63130ee7", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                conn.execute(text("INSERT INTO download_history (event_type) VALUES ('stalled')"))
            conn.rollback()
    finally:
        engine.dispose()

    _upgrade(db_path, "head", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            conn.execute(text("INSERT INTO download_history (event_type) VALUES ('stalled')"))
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


def test_release_title_migration_backfills_from_download_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``release_title`` column-adding migration (issue #134) backfills EXISTING
    rows from ``download_history.source_title`` -- an operator upgrading mid-download
    must see a human release name in the queue, not a hole this migration leaves."""
    db_path = tmp_path / "backfill.db"
    # Pre-migration head: a downloads row with no release_title column yet, and a
    # matching grabbed history event carrying the release's source_title.
    _upgrade(db_path, "7bcbce2c2e2b", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO downloads (torrent_hash, status, progress, seed_ratio,
                        target_seed_ratio, retry_count, torrent_attempt)
                    VALUES ('aa1', 'downloading', 0, 0, 1, 0, 1)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO download_history (torrent_hash, event_type, source_title)
                    VALUES ('aa1', 'grabbed', 'Some.Movie.2020.1080p.WEB-DL.x264-GROUP')
                    """
                )
            )
    finally:
        engine.dispose()

    _upgrade(db_path, "head", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            release_title = conn.execute(
                text("SELECT release_title FROM downloads WHERE torrent_hash = 'aa1'")
            ).scalar_one()
    finally:
        engine.dispose()
    assert release_title == "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"


def test_release_title_migration_backfill_prefers_the_newest_history_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash re-grabbed across a terminal-row reuse has MULTIPLE history rows; the
    backfill must pick the NEWEST ``source_title`` -- the one that actually owns the
    row's current download -- not an arbitrary or oldest match."""
    db_path = tmp_path / "backfill-latest.db"
    _upgrade(db_path, "7bcbce2c2e2b", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO downloads (torrent_hash, status, progress, seed_ratio,
                        target_seed_ratio, retry_count, torrent_attempt)
                    VALUES ('reused_hash', 'downloading', 0, 0, 1, 0, 1)
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO download_history (torrent_hash, event_type, source_title)
                    VALUES ('reused_hash', 'grabbed', 'Old.Release-GROUP')
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO download_history (torrent_hash, event_type, source_title)
                    VALUES ('reused_hash', 'grabbed', 'New.Release-GROUP')
                    """
                )
            )
    finally:
        engine.dispose()

    _upgrade(db_path, "head", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            release_title = conn.execute(
                text("SELECT release_title FROM downloads WHERE torrent_hash = 'reused_hash'")
            ).scalar_one()
    finally:
        engine.dispose()
    assert release_title == "New.Release-GROUP"


def test_release_title_migration_leaves_unmatched_rows_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A downloads row with no corresponding ``download_history`` event (e.g. a
    hashless legacy row) is left honestly ``NULL`` -- the backfill never fabricates
    a release name."""
    db_path = tmp_path / "backfill-none.db"
    _upgrade(db_path, "7bcbce2c2e2b", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO downloads (torrent_hash, status, progress, seed_ratio,
                        target_seed_ratio, retry_count, torrent_attempt)
                    VALUES ('no_history', 'downloading', 0, 0, 1, 0, 1)
                    """
                )
            )
    finally:
        engine.dispose()

    _upgrade(db_path, "head", monkeypatch)

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            release_title = conn.execute(
                text("SELECT release_title FROM downloads WHERE torrent_hash = 'no_history'")
            ).scalar_one()
    finally:
        engine.dispose()
    assert release_title is None
