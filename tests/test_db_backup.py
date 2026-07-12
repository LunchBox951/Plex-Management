"""Tests for the advisory pre-migration DB + key backup (ADR-0021, #221, #222).

Each test builds a throwaway ``Settings`` pointed at a ``tmp_path`` SQLite file
(via ``Settings(_env_file=None, ...)`` so no real ``.env``/process env leaks
in) rather than a real Alembic-migrated database -- ``db_backup`` only reads
the ``alembic_version`` table and the raw file bytes, so a hand-built SQLite
file is sufficient and keeps these tests fast and independent of the actual
migration chain, except where a test explicitly wants the real head revision.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

import pytest

from plex_manager import db_backup
from plex_manager.config import Settings


def _settings(tmp_path: Path, db_name: str = "plex_manager.db") -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / db_name
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        data_dir=str(data_dir),
        database_url=f"sqlite+aiosqlite:///{db_path}",
    )


def _stamp_db(db_path: Path, revision: str) -> None:
    """Create a minimal SQLite file stamped at ``revision`` via ``alembic_version``."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (revision,))
        conn.commit()
    finally:
        conn.close()


def _write_key(data_dir: Path, mode: int = 0o600) -> Path:
    key = data_dir / "secret.key"
    key.write_bytes(b"unit-test-fernet-key-bytes-not-real==")
    key.chmod(mode)
    return key


def _real_head() -> str:
    head = db_backup._head_revision()  # pyright: ignore[reportPrivateUsage]
    assert head is not None and head != db_backup._UNKNOWN_HEAD  # pyright: ignore[reportPrivateUsage]
    return head


def test_skips_when_database_file_missing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    result = db_backup.create_pre_migration_backup(settings)

    assert result is None
    assert not (Path(settings.data_dir) / "backups").exists()


def test_skips_when_already_at_head(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    db_path = Path(settings.data_dir) / "plex_manager.db"
    _stamp_db(db_path, _real_head())
    _write_key(Path(settings.data_dir))

    result = db_backup.create_pre_migration_backup(settings)

    assert result is None
    assert not (Path(settings.data_dir) / "backups").exists()


def test_backs_up_db_and_key_as_unit_when_migration_pending(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    old_rev = "0000synthetic-old-rev"
    _stamp_db(db_path, old_rev)
    key_path = _write_key(data_dir)

    result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    assert result.name.startswith(f"pre-migrate-{old_rev}-")
    assert (result / "MANIFEST.txt").exists()

    copied_db = result / "plex_manager.db"
    assert copied_db.exists()
    conn = sqlite3.connect(f"file:{copied_db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == old_rev

    copied_key = result / "secret.key"
    assert copied_key.exists()
    assert copied_key.read_bytes() == key_path.read_bytes()
    assert (copied_key.stat().st_mode & 0o777) == 0o600


def test_snapshot_captures_uncommitted_wal_rows(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version (version_num) VALUES ('old-rev')")
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT)")
        conn.commit()
        # Committed but deliberately NOT checkpointed -- lives only in -wal until
        # something checkpoints it. A bare file copy of the main .db file would
        # miss this row entirely.
        conn.execute("INSERT INTO probe (note) VALUES ('uncommitted-to-main-file')")
        conn.commit()
    finally:
        conn.close()

    result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    copied_db = result / "plex_manager.db"
    copy_conn = sqlite3.connect(f"file:{copied_db}?mode=ro", uri=True)
    try:
        rows = copy_conn.execute("SELECT note FROM probe").fetchall()
    finally:
        copy_conn.close()
    assert rows == [("uncommitted-to-main-file",)]


def test_missing_key_backs_up_db_only_nonfatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    _stamp_db(db_path, "0000synthetic-old-rev")
    # No secret.key written -- simulates PLEX_MANAGER_FERNET_KEY env override.

    with caplog.at_level(logging.INFO, logger=db_backup.logger.name):
        result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    assert (result / "plex_manager.db").exists()
    assert (result / "MANIFEST.txt").exists()
    assert not (result / "secret.key").exists()
    assert any("preserve" in record.message.lower() for record in caplog.records)


def test_non_sqlite_is_skipped_with_notice(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        data_dir=str(tmp_path),
        database_url="postgresql+asyncpg://user:pass@localhost/plexmanager",
    )

    with caplog.at_level(logging.INFO, logger=db_backup.logger.name):
        result = db_backup.create_pre_migration_backup(settings)

    assert result is None
    assert any("non-sqlite" in record.message.lower() for record in caplog.records)


def test_prune_keeps_only_keep_count(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    _stamp_db(db_path, "0000synthetic-old-rev")

    backups_root = data_dir / "backups"
    backups_root.mkdir(parents=True)
    extra = db_backup._KEEP_COUNT + 2  # pyright: ignore[reportPrivateUsage]
    for i in range(extra):
        # Staggered mtimes, oldest first -- pruning must key on mtime, not the
        # directory name (its revision-hash prefix is not chronological).
        stale = backups_root / f"pre-migrate-fake-rev-2020010{i}T000000Z"
        stale.mkdir()
        os.utime(stale, (1_000_000 + i, 1_000_000 + i))

    result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    remaining = sorted(p.name for p in backups_root.iterdir() if p.is_dir())
    assert len(remaining) == db_backup._KEEP_COUNT  # pyright: ignore[reportPrivateUsage]
    # The freshly-created backup (newest by name) must survive pruning.
    assert result.name in remaining


def test_backup_failure_is_loud_but_nonfatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings(tmp_path)
    db_path = Path(settings.data_dir) / "plex_manager.db"
    _stamp_db(db_path, "0000synthetic-old-rev")

    monkeypatch.setattr(db_backup, "get_settings", lambda: settings)

    def _boom(_src: Path, _dst: Path) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(db_backup, "_snapshot_sqlite", _boom)

    with caplog.at_level(logging.ERROR, logger=db_backup.logger.name):
        db_backup.main()  # must not raise

    assert any(
        "FAILED" in record.message and "proceeding" in record.message for record in caplog.records
    )


def test_head_revision_returns_sentinel_when_undeterminable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _Boom:
        @staticmethod
        def from_config(_config: object) -> _Boom:
            raise RuntimeError("multiple heads")

    import alembic.script

    monkeypatch.setattr(alembic.script, "ScriptDirectory", _Boom)

    with caplog.at_level(logging.WARNING, logger=db_backup.logger.name):
        head = db_backup._head_revision()  # pyright: ignore[reportPrivateUsage]

    assert head == db_backup._UNKNOWN_HEAD  # pyright: ignore[reportPrivateUsage]


def test_multiple_or_unknown_head_forces_defensive_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even a database already at the real head must still be backed up
    # defensively when the head itself cannot be determined (e.g. multiple
    # heads, or an unreadable scripts directory) -- the sentinel never equals a
    # real ``alembic_version`` value, so the "already at head" skip cannot fire.
    settings = _settings(tmp_path)
    db_path = Path(settings.data_dir) / "plex_manager.db"
    _stamp_db(db_path, _real_head())
    _write_key(Path(settings.data_dir))

    monkeypatch.setattr(db_backup, "_head_revision", lambda: db_backup._UNKNOWN_HEAD)  # pyright: ignore[reportPrivateUsage]

    result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
