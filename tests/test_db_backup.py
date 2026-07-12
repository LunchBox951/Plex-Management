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
import shutil
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


def _real_old_rev() -> str:
    """A real, in-graph revision that is NOT head (the migration base).

    ``create_pre_migration_backup`` only takes a backup when ``current`` is a
    revision this image's Alembic graph actually knows (else it treats it as an
    older-image / newer-DB mismatch and skips). Tests that want a genuine pending
    forward migration therefore stamp a real revision -- the base -- rather than a
    synthetic string that the graph would (correctly) reject.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    bases = ScriptDirectory.from_config(Config("alembic.ini")).get_bases()
    assert bases, "expected at least one Alembic base revision"
    base = bases[0]
    assert base != _real_head(), "base and head must differ for a pending-migration fixture"
    return base


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
    old_rev = _real_old_rev()
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

    # Manifest records the FILE_INCLUDED state, not the env or lost-key wording.
    manifest = (result / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "secret.key (mode 0600)" in manifest
    assert "NO KEY" not in manifest
    assert "this install's key is supplied via" not in manifest


def test_snapshot_captures_uncommitted_wal_rows(tmp_path: Path) -> None:
    """A plain ``shutil.copy`` of the main .db file must FAIL this test.

    SQLite checkpoints the WAL back into the main file when the last
    connection closes, so a writer that closes cleanly before the backup runs
    leaves nothing uncheckpointed to lose -- a naive file copy would pass just
    as well as the real WAL-consistent snapshot, and the test would prove
    nothing. The writer connection is therefore kept OPEN across the
    ``create_pre_migration_backup`` call (mirroring the app process, which
    holds its database connection open across the whole pre-migration-backup
    step), so the row genuinely lives only in ``-wal`` at backup time. Verified
    empirically: with this connection held open, a bare ``shutil.copy(src,
    dst)`` of just the .db file raises ``sqlite3.OperationalError: no such
    table: probe`` on the copy, while ``_snapshot_sqlite``'s
    ``sqlite3.Connection.backup`` API correctly captures the row.
    """
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        conn.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (_real_old_rev(),))
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT)")
        conn.commit()
        # Committed but deliberately NOT checkpointed -- lives only in -wal
        # until something checkpoints it. The connection stays open past this
        # point (no close before the backup call) so the WAL is never
        # auto-checkpointed out from under the test.
        conn.execute("INSERT INTO probe (note) VALUES ('uncommitted-to-main-file')")
        conn.commit()

        result = db_backup.create_pre_migration_backup(settings)
    finally:
        conn.close()

    assert result is not None
    copied_db = result / "plex_manager.db"
    copy_conn = sqlite3.connect(f"file:{copied_db}?mode=ro", uri=True)
    try:
        rows = copy_conn.execute("SELECT note FROM probe").fetchall()
    finally:
        copy_conn.close()
    assert rows == [("uncommitted-to-main-file",)]


def test_snapshot_naive_copy_would_miss_uncheckpointed_wal_rows(tmp_path: Path) -> None:
    """Direct regression guard for the WAL-consistency claim itself.

    Proves, independently of ``create_pre_migration_backup``, that a bare
    ``shutil.copy`` of an open, uncheckpointed WAL database drops the row that
    ``_snapshot_sqlite`` (the SQLite backup API) preserves -- so a future
    accidental revert of ``_snapshot_sqlite`` to a plain file copy fails this
    test directly, not just via a side effect of connection lifecycle.
    """
    db_path = tmp_path / "source.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT)")
        conn.commit()
        conn.execute("INSERT INTO probe (note) VALUES ('uncommitted-to-main-file')")
        conn.commit()

        naive_copy = tmp_path / "naive.db"
        shutil.copy(db_path, naive_copy)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            naive_conn = sqlite3.connect(f"file:{naive_copy}?mode=ro", uri=True)
            try:
                naive_conn.execute("SELECT note FROM probe").fetchall()
            finally:
                naive_conn.close()

        api_copy = tmp_path / "api.db"
        db_backup._snapshot_sqlite(db_path, api_copy)  # pyright: ignore[reportPrivateUsage]
        api_conn = sqlite3.connect(f"file:{api_copy}?mode=ro", uri=True)
        try:
            rows = api_conn.execute("SELECT note FROM probe").fetchall()
        finally:
            api_conn.close()
        assert rows == [("uncommitted-to-main-file",)]
    finally:
        conn.close()


def test_missing_key_backs_up_db_only_nonfatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Lost-key case: no secret.key AND no env override -- a DB-only backup.

    The manifest must say so LOUDLY and must NOT claim the install uses
    PLEX_MANAGER_FERNET_KEY (telling the operator to restore an env value that
    never existed was the round-2 finding): the runbook has to send them hunting
    for the ORIGINAL key, not a nonexistent env secret.
    """
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    _stamp_db(db_path, _real_old_rev())
    # No secret.key written and no override -- the key is genuinely absent everywhere.

    with caplog.at_level(logging.WARNING, logger=db_backup.logger.name):
        result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    assert (result / "plex_manager.db").exists()
    assert not (result / "secret.key").exists()
    assert any(
        "NO encryption key" in record.message and "DATABASE-ONLY" in record.message
        for record in caplog.records
    )
    manifest = (result / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "NO KEY" in manifest
    assert "DATABASE-ONLY" in manifest
    assert "recover it separately" in manifest
    # Must NOT tell the operator this install's key lives in the env -- it doesn't.
    assert "this install's key is supplied via" not in manifest
    assert "Ensure the SAME PLEX_MANAGER_FERNET_KEY" not in manifest


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
    _stamp_db(db_path, _real_old_rev())

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
    _stamp_db(db_path, _real_old_rev())

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


def test_env_override_key_not_copied_even_when_stale_file_present(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Finding 1: PLEX_MANAGER_FERNET_KEY wins over any on-disk secret.key.

    An operator who moved the key into the env but left a STALE secret.key in the
    volume must not get a backup that embeds that stale file and a manifest
    claiming the key was included -- restoring it after a rollback would leave the
    DB undecryptable. The active key lives only in the env, so the backup records
    that and copies nothing.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "plex_manager.db"
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        data_dir=str(data_dir),
        database_url=f"sqlite+aiosqlite:///{db_path}",
        fernet_key="env-override-not-the-file-value",  # pyright: ignore[reportArgumentType]
    )
    _stamp_db(db_path, _real_old_rev())
    stale_key = _write_key(data_dir)
    stale_key.write_bytes(b"STALE-on-disk-key-that-no-longer-matches==")

    with caplog.at_level(logging.WARNING, logger=db_backup.logger.name):
        result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    # The stale on-disk key must NOT be embedded in the backup.
    assert not (result / "secret.key").exists()
    manifest = (result / "MANIFEST.txt").read_text(encoding="utf-8")
    # The manifest must record the ENV-RESIDENT state: the key exists (in the
    # operator's environment) and the restore step is to re-supply that same
    # value -- distinct from the lost-key DB-only wording (round-2 finding).
    assert "this install's key is supplied via" in manifest
    assert "Ensure the SAME PLEX_MANAGER_FERNET_KEY" in manifest
    assert "NO KEY" not in manifest
    assert "DATABASE-ONLY" not in manifest
    assert any(
        "PLEX_MANAGER_FERNET_KEY" in record.message and "not copied" in record.message.lower()
        for record in caplog.records
    )


def test_skips_and_does_not_prune_for_unknown_newer_revision(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Finding 2: an older image against a newer-stamped DB must NOT back up/prune.

    The stamped revision is absent from this image's migration graph; Alembic
    would reject the upgrade anyway. Taking a backup here snapshots the
    already-migrated DB and -- worse -- _prune() could delete the REAL
    pre-migration backup after a few restarts. So skip both, and leave existing
    backups untouched.
    """
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    _stamp_db(db_path, "9999-revision-not-in-this-images-graph")
    _write_key(data_dir)

    # A genuine pre-migration backup already on disk -- the recovery path that
    # must survive an accidental older-image start.
    backups_root = data_dir / "backups"
    backups_root.mkdir(parents=True)
    genuine = backups_root / "pre-migrate-realbase-20200101T000000Z"
    genuine.mkdir()
    (genuine / "plex_manager.db").write_bytes(b"real-backup")
    before = sorted(p.name for p in backups_root.iterdir())

    with caplog.at_level(logging.WARNING, logger=db_backup.logger.name):
        result = db_backup.create_pre_migration_backup(settings)

    assert result is None
    # No new backup written and nothing pruned -- the genuine unit is intact.
    assert sorted(p.name for p in backups_root.iterdir()) == before
    assert (genuine / "plex_manager.db").read_bytes() == b"real-backup"
    assert any("migration graph" in record.message for record in caplog.records)


def test_unknown_head_backup_does_not_prune_existing_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding: a defensive unknown-head backup must never evict existing backups.

    Under ``restart: unless-stopped``, a broken image that keeps failing
    ``alembic upgrade head`` restarts forever, producing one defensive backup
    per restart (the head is undeterminable each time). If each of those
    triggered the normal keep-5 prune, enough restarts would evict the genuine
    pre-migration backup that guards the last successful migration -- exactly
    the recovery unit ADR-0021 depends on. So an unknown-head backup must add
    itself without pruning anything, even when the total now exceeds
    ``_KEEP_COUNT``.
    """
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    _stamp_db(db_path, _real_head())
    _write_key(data_dir)

    backups_root = data_dir / "backups"
    backups_root.mkdir(parents=True)
    extra = db_backup._KEEP_COUNT + 3  # pyright: ignore[reportPrivateUsage]
    existing: list[str] = []
    for i in range(extra):
        stale = backups_root / f"pre-migrate-fake-rev-2020010{i}T000000Z"
        stale.mkdir()
        os.utime(stale, (1_000_000 + i, 1_000_000 + i))
        existing.append(stale.name)

    monkeypatch.setattr(db_backup, "_head_revision", lambda: db_backup._UNKNOWN_HEAD)  # pyright: ignore[reportPrivateUsage]

    result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    remaining = sorted(p.name for p in backups_root.iterdir() if p.is_dir())
    # Every pre-existing backup survives AND the new defensive one is added --
    # nothing pruned even though the count now exceeds _KEEP_COUNT. A normal
    # (non-defensive) backup path would have pruned this down to _KEEP_COUNT.
    assert set(existing).issubset(set(remaining))
    assert result.name in remaining
    assert len(remaining) == extra + 1


def test_manifest_instructs_removing_stale_wal_shm_sidecars(tmp_path: Path) -> None:
    """Finding: the restore runbook must tell the operator to drop stale WAL/SHM.

    The snapshot is a STANDALONE file taken via the SQLite backup API. If the
    operator restores it next to leftover ``<db>-wal`` / ``<db>-shm`` sidecars
    from whatever database was running most recently (typically a newer one),
    SQLite replays those frames on open -- silently reintroducing the newer
    schema/data the restore was meant to roll back away from.
    """
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    _stamp_db(db_path, _real_old_rev())
    _write_key(data_dir)

    result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    manifest = (result / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "-wal" in manifest
    assert "-shm" in manifest


def test_partial_backup_is_not_published_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3: a failure after the dir is created leaves NO pre-migrate-* unit.

    The backup is built under a ``.tmp-`` staging name and only renamed to its
    final ``pre-migrate-*`` name once the DB, key, and manifest are all written.
    A mid-way failure removes the staging dir, so nothing partial can be mistaken
    for a restore unit or counted by _prune.
    """
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    _stamp_db(db_path, _real_old_rev())
    _write_key(data_dir)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("manifest write failed")

    monkeypatch.setattr(db_backup, "_write_manifest", _boom)

    with pytest.raises(OSError, match="manifest write failed"):
        db_backup.create_pre_migration_backup(settings)

    backups_root = data_dir / "backups"
    if backups_root.exists():
        names = [p.name for p in backups_root.iterdir()]
        assert not any(n.startswith("pre-migrate-") for n in names), names
        assert not any(n.startswith(".tmp-") for n in names), names


def test_prune_sweeps_abandoned_temp_dirs(tmp_path: Path) -> None:
    """Finding 3: _prune removes orphaned ``.tmp-`` staging dirs from prior crashes."""
    settings = _settings(tmp_path)
    data_dir = Path(settings.data_dir)
    db_path = data_dir / "plex_manager.db"
    _stamp_db(db_path, _real_old_rev())
    _write_key(data_dir)

    backups_root = data_dir / "backups"
    backups_root.mkdir(parents=True)
    orphan = backups_root / ".tmp-pre-migrate-crashed-20200101T000000Z"
    orphan.mkdir()
    (orphan / "plex_manager.db").write_bytes(b"half-written")

    result = db_backup.create_pre_migration_backup(settings)

    assert result is not None
    assert not orphan.exists()
    # The freshly published backup is a normal pre-migrate-* unit, not a temp dir.
    assert result.name.startswith("pre-migrate-")
    assert not result.name.startswith(".tmp-")
