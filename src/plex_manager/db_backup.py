"""Advisory pre-migration backup of the SQLite database + Fernet key (ADR-0021).

Run as ``python -m plex_manager.db_backup`` from the container entrypoint,
*before* ``alembic upgrade head``. When a migration is actually pending, this
snapshots the SQLite database (WAL-consistent, via :func:`sqlite3.Connection.backup`
-- not a bare file copy, which would silently miss uncommitted WAL frames) and
the Fernet encryption key as **one recovery unit** into
``<data_dir>/backups/pre-migrate-<from-rev>-<timestamp>/``, alongside a
human-readable ``MANIFEST.txt`` restore runbook. Backups are pruned to the
most recent :data:`_KEEP_COUNT`.

This is advisory, not a replacement for an operator's own backup strategy
(Postgres deployments are explicitly out of scope -- see :func:`_sqlite_file_path`)
and it is deliberately **fail-loud but never fatal**: :func:`main` always exits
0 so the entrypoint's ``set -e`` never bricks a container start over a failed
backup. See ADR-0021 for the policy this implements and the reasoning against
relying on Alembic downgrade scripts as a general rollback path.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy.engine import make_url

from plex_manager.adapters.encryption import secret_key_path
from plex_manager.config import Settings, get_settings
from plex_manager.db import sync_database_url

logger = logging.getLogger(__name__)

_KEEP_COUNT: Final = 5
_BACKUP_SUBDIR: Final = "backups"
_PREFIX: Final = "pre-migrate-"
_DB_COPY_NAME: Final = "plex_manager.db"
_KEY_COPY_NAME: Final = "secret.key"
_MANIFEST_NAME: Final = "MANIFEST.txt"

# Forces a defensive backup when the migration head can't be determined (e.g.
# multiple heads, or the scripts directory is unreadable) -- never matches a
# real ``alembic_version`` row, so the "already at head" skip never fires.
_UNKNOWN_HEAD: Final = "__unknown__"


def _sqlite_file_path(settings: Settings) -> Path | None:
    """Return the on-disk SQLite file path, or ``None`` for a non-SQLite backend.

    Uses SQLAlchemy's URL parser rather than hand-stripping a ``sqlite:///``
    prefix so both absolute (``sqlite:////abs/path.db``) and relative
    (``sqlite:///./data/plex_manager.db``) URLs resolve correctly.
    """
    url = make_url(sync_database_url(settings.database_url))
    if url.get_backend_name() != "sqlite" or url.database is None:
        return None
    return Path(url.database)


def _current_revision(db_path: Path) -> str | None:
    """Read the ``alembic_version`` row from an existing SQLite file, read-only.

    Returns ``None`` when the table doesn't exist yet (a database file that
    predates Alembic, or a partially-initialized one) -- callers treat that the
    same as "base" (nothing applied).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = conn.execute("SELECT version_num FROM alembic_version LIMIT 1")
        row = cursor.fetchone()
        return str(row[0]) if row is not None else None
    except sqlite3.OperationalError:
        # e.g. "no such table: alembic_version" -- an unstamped/legacy DB file.
        return None
    finally:
        conn.close()


def _head_revision() -> str | None:
    """Return the migration head defined by the scripts on disk.

    Reads ``migrations/`` via Alembic's ``ScriptDirectory`` only -- no DB or
    ``env.py`` execution -- so this never touches the database itself.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        head = script.get_current_head()
    except Exception:
        logger.warning(
            "Could not determine the Alembic migration head (multiple heads or "
            "an unreadable scripts directory); forcing a defensive pre-migration "
            "backup.",
            exc_info=True,
        )
        return _UNKNOWN_HEAD
    return head


def _snapshot_sqlite(src: Path, dst: Path) -> None:
    """Write a WAL-consistent copy of ``src`` to ``dst`` via the SQLite backup API.

    ``sqlite3.Connection.backup`` (not a bare file copy) includes any
    uncommitted WAL frames, so a snapshot taken while the app is running does
    not silently drop recently-committed rows still sitting in ``-wal``.
    """
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_conn = sqlite3.connect(dst)
        try:
            with dst_conn:
                src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _copy_key(settings: Settings, dest_dir: Path) -> bool:
    """Copy the Fernet key file into the backup dir, preserving 0600. Returns
    whether a key file was found and copied (``False`` when the operator uses
    the ``PLEX_MANAGER_FERNET_KEY`` env override instead of a key file)."""
    key = secret_key_path(settings)
    if not key.exists():
        logger.info(
            "Encryption key is via PLEX_MANAGER_FERNET_KEY (env override) or "
            "absent at %s; this backup will not include it -- preserve that "
            "environment variable separately. The database and key are one "
            "recovery unit; a replacement key cannot decrypt existing ciphertext.",
            key,
        )
        return False
    dest = dest_dir / _KEY_COPY_NAME
    shutil.copy2(key, dest)
    os.chmod(dest, 0o600)
    return True


def _write_manifest(dest_dir: Path, from_rev: str | None, key_included: bool) -> None:
    """Write a human-readable restore runbook alongside the backed-up files."""
    timestamp = datetime.now(UTC).isoformat()
    from_label = from_rev or "base (no migrations applied yet)"
    key_line = (
        f"  - {_KEY_COPY_NAME} (mode 0600)"
        if key_included
        else "  - (no key file -- this install uses PLEX_MANAGER_FERNET_KEY; "
        "preserve that value separately)"
    )
    manifest = f"""\
Plex Manager -- pre-migration backup
=====================================

Created (UTC):        {timestamp}
Database revision:    {from_label}
Source database:      see the app's PLEX_MANAGER_DATABASE_URL at backup time
Source key:           <data_dir>/secret.key (or PLEX_MANAGER_FERNET_KEY override)

Contents of this directory:
  - {_DB_COPY_NAME} (WAL-consistent snapshot, taken before `alembic upgrade head`)
{key_line}

IMPORTANT: the database and the Fernet key are ONE recovery unit. A
replacement key cannot decrypt Plex tokens, service credentials, the
recovery API key, or encrypted magnet links already stored in the database.
Restoring the database file alone, without the matching key, leaves the
install unable to decrypt its own stored secrets.

Restore steps:
  1. Stop the container.
  2. Copy {_DB_COPY_NAME} back into the data directory, replacing the current
     database file (match the filename your PLEX_MANAGER_DATABASE_URL expects).
  3. If {_KEY_COPY_NAME} is present in this backup, copy it back into the data
     directory as secret.key, preserving mode 0600 (`chmod 600 secret.key`).
     If it is not present, restore your saved PLEX_MANAGER_FERNET_KEY value
     instead.
  4. Re-point the deployment at the older image tag that matches this
     database revision (same-schema rollback: just re-point; cross-migration
     rollback: this backup unit IS the recovery path -- see ADR-0021).
  5. Start the container and verify: sign in, and confirm a stored credential
     (e.g. a configured service) still decrypts correctly.

See docs/adr/0021-database-rollback-and-pre-migration-backup.md and the
README "Backup & recovery" section for the full policy.
"""
    (dest_dir / _MANIFEST_NAME).write_text(manifest, encoding="utf-8")


def _prune(backups_root: Path, keep: int = _KEEP_COUNT) -> None:
    """Keep only the most recent ``keep`` backup directories under ``backups_root``.

    Sorted by ``st_mtime``, not by name: the directory name is
    ``pre-migrate-<from-rev>-<timestamp>``, and ``<from-rev>`` is an arbitrary
    Alembic revision hash that sorts lexicographically ahead of or behind later
    revisions' hashes with no relation to time -- a pure name-sort is NOT
    reliably chronological. Each removal is isolated so one failure (e.g. a
    permissions fault) doesn't stop the rest from being pruned.
    """
    if not backups_root.is_dir():
        return
    candidates = sorted(
        (p for p in backups_root.glob(f"{_PREFIX}*") if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
    )
    for stale in candidates[:-keep] if keep > 0 else candidates:
        try:
            shutil.rmtree(stale)
        except OSError:
            logger.warning("Could not prune stale backup directory %s", stale, exc_info=True)


def create_pre_migration_backup(settings: Settings | None = None) -> Path | None:
    """Snapshot the DB + Fernet key as one unit when a migration is pending.

    Returns the created backup directory, or ``None`` when no backup was
    written (non-SQLite backend, no existing database, or already at head --
    every case is logged honestly so the operator knows why nothing happened).
    """
    settings = settings or get_settings()

    db_path = _sqlite_file_path(settings)
    if db_path is None:
        logger.info(
            "Non-SQLite database configured; automatic pre-migration file backup "
            "is not performed. Snapshot the database AND the encryption key "
            "externally before upgrades (see ADR-0021 / README Backup & recovery)."
        )
        return None

    if not db_path.exists():
        logger.info("No existing database at %s (fresh install); nothing to back up.", db_path)
        return None

    current = _current_revision(db_path)
    head = _head_revision()
    if current == head:
        logger.info(
            "Database already at head revision %s; no migration pending, "
            "skipping pre-migration backup.",
            current,
        )
        return None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    from_label = current or "base"
    dest = Path(settings.data_dir) / _BACKUP_SUBDIR / f"{_PREFIX}{from_label}-{timestamp}"
    dest.mkdir(parents=True, exist_ok=True)

    _snapshot_sqlite(db_path, dest / _DB_COPY_NAME)
    key_included = _copy_key(settings, dest)
    _write_manifest(dest, current, key_included)

    logger.info(
        "Pre-migration backup written to %s (from rev %s, key_included=%s) -- "
        "restore this DB+key unit if the upgrade must be rolled back.",
        dest,
        current,
        key_included,
    )

    _prune(dest.parent)
    return dest


def main() -> None:
    """Entrypoint hook: back up if needed, but NEVER fail the container start.

    The broad ``except Exception`` here is deliberate and honesty-compliant: it
    emits a loud ERROR (never a silent swallow) and always returns normally
    (exit 0), so a failed backup -- a full disk, a permissions fault, a locked
    file -- trades an *unprotected but running* startup for never bricking a
    container over a snapshot (the entrypoint runs under ``set -e``). See
    ADR-0021.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        create_pre_migration_backup()
    except Exception:
        logger.error(
            "Pre-migration backup FAILED -- proceeding with migration WITHOUT a "
            "fresh backup; restore from an older pre-migrate-* unit under "
            "<data_dir>/backups/ if this upgrade must be reverted.",
            exc_info=True,
        )


if __name__ == "__main__":
    main()
