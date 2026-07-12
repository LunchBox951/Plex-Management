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

import enum
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
# Prefix for an in-progress backup that has not yet been published. Chosen so it
# never matches the ``pre-migrate-*`` glob: a partial (crashed) attempt is thus
# invisible to _prune's keep-count AND to recovery, until it is atomically
# renamed to its final ``pre-migrate-*`` name once every file is written.
_TMP_PREFIX: Final = ".tmp-"
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


def _revision_in_graph(revision: str) -> bool:
    """Whether ``revision`` is a node in THIS image's Alembic migration graph.

    A ``current`` revision that the on-disk ``migrations/`` do not contain means
    the database was stamped by a DIFFERENT (newer) image than the one now
    starting -- e.g. an older image accidentally launched against a
    already-upgraded volume. Alembic will then refuse to upgrade from an unknown
    revision, so there is no forward migration to guard and any snapshot taken
    here would capture the already-migrated, unusable-for-rollback DB. Reads
    ``migrations/`` via ``ScriptDirectory`` only (no DB or ``env.py`` execution);
    any failure to resolve the revision is reported as "not in graph".
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        script = ScriptDirectory.from_config(Config("alembic.ini"))
        script.get_revision(revision)
    except Exception:
        return False
    return True


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


def _fernet_env_override_configured(settings: Settings) -> bool:
    """Whether ``PLEX_MANAGER_FERNET_KEY`` supplies the active key.

    Mirrors ``adapters.encryption._fernet_override``: the override is in effect
    iff ``settings.fernet_key`` is set to a non-empty value (an empty override is
    treated as unset, exactly as the encryption layer does). Kept as a local
    mirror rather than importing that private helper so the backup path -- which
    must agree with how :func:`~plex_manager.adapters.encryption.get_fernet`
    actually resolves the key -- does not reach across a module's private API.
    """
    if settings.fernet_key is None:
        return False
    return bool(settings.fernet_key.get_secret_value())


class KeyDisposition(enum.Enum):
    """How the ACTIVE encryption key relates to a backup (drives the manifest).

    Three honestly-distinct states -- a bare "key included yes/no" conflated the
    env-override case (the key exists, in the operator's environment) with the
    lost-key case (no key exists ANYWHERE), and the manifest then told a
    key-less install to "restore your PLEX_MANAGER_FERNET_KEY value" that never
    existed.
    """

    FILE_INCLUDED = enum.auto()  # secret.key was copied into the backup dir
    ENV_OVERRIDE = enum.auto()  # key lives in PLEX_MANAGER_FERNET_KEY; not embedded
    MISSING = enum.auto()  # no key anywhere -- this is a DB-only backup


def _copy_key(settings: Settings, dest_dir: Path) -> KeyDisposition:
    """Copy the ACTIVE Fernet key into the backup dir, preserving 0600. Returns
    where the active key actually lives (:class:`KeyDisposition`).

    Mirrors the encryption layer's key resolution (:func:`get_fernet` /
    :func:`ensure_secret_key`): a configured ``PLEX_MANAGER_FERNET_KEY`` override
    ALWAYS wins and the on-disk ``secret.key`` is never consulted. So when the
    override is set we must NOT copy that file -- it may be a stale leftover that
    no longer matches the active key. Copying it would put the WRONG key in the
    backup while the manifest claimed the recovery unit was complete; restoring
    that unit after a rollback would leave the database undecryptable. In that
    case the active key lives only in the operator's environment/secret store and
    is recorded (not embedded) as such. When NO key exists anywhere the backup is
    DB-only and both the log and the manifest say so loudly instead of pointing
    at an env value that does not exist."""
    if _fernet_env_override_configured(settings):
        logger.warning(
            "Encryption key is provided via PLEX_MANAGER_FERNET_KEY (env "
            "override), which the app uses in preference to any on-disk "
            "secret.key; that file (if present) is NOT the active key and is "
            "deliberately NOT copied into this backup. Preserve the "
            "PLEX_MANAGER_FERNET_KEY value separately -- it and the database are "
            "one recovery unit; a replacement key cannot decrypt existing ciphertext."
        )
        return KeyDisposition.ENV_OVERRIDE
    key = secret_key_path(settings)
    if not key.exists():
        logger.warning(
            "NO encryption key found: no key file at %s and no "
            "PLEX_MANAGER_FERNET_KEY override. This is a DATABASE-ONLY backup. "
            "If this install stores encrypted data, the original key must be "
            "recovered separately -- the database and key are one recovery unit; "
            "a replacement key cannot decrypt existing ciphertext.",
            key,
        )
        return KeyDisposition.MISSING
    dest = dest_dir / _KEY_COPY_NAME
    shutil.copy2(key, dest)
    os.chmod(dest, 0o600)
    return KeyDisposition.FILE_INCLUDED


def _write_manifest(dest_dir: Path, from_rev: str | None, key_disposition: KeyDisposition) -> None:
    """Write a human-readable restore runbook alongside the backed-up files.

    The key section distinguishes THREE states honestly (see
    :class:`KeyDisposition`): key file included / key is env-resident (restore
    the ``PLEX_MANAGER_FERNET_KEY`` secret) / NO key found (DB-only backup, the
    original key must be recovered separately) -- so the runbook never tells a
    key-less install to restore an env value that does not exist.
    """
    timestamp = datetime.now(UTC).isoformat()
    from_label = from_rev or "base (no migrations applied yet)"
    if key_disposition is KeyDisposition.FILE_INCLUDED:
        key_source = "<data_dir>/secret.key (key file, included in this backup)"
        key_line = f"  - {_KEY_COPY_NAME} (mode 0600)"
        key_restore = f"""\
  3. Copy {_KEY_COPY_NAME} from this backup back into the data directory as
     secret.key, preserving mode 0600 (`chmod 600 secret.key`)."""
    elif key_disposition is KeyDisposition.ENV_OVERRIDE:
        key_source = "PLEX_MANAGER_FERNET_KEY (env override -- NOT embedded in this backup)"
        key_line = (
            "  - (no key file -- this install's key is supplied via the\n"
            "    PLEX_MANAGER_FERNET_KEY environment variable/secret, which is NOT\n"
            "    embedded in this backup; preserve that value separately)"
        )
        key_restore = """\
  3. Ensure the SAME PLEX_MANAGER_FERNET_KEY value that was active at backup
     time is configured on the deployment (this backup contains no key file;
     the env secret IS the key half of this recovery unit)."""
    else:  # KeyDisposition.MISSING
        key_source = "NONE FOUND at backup time (no secret.key, no PLEX_MANAGER_FERNET_KEY)"
        key_line = (
            "  - !! NO KEY: no secret.key file and no PLEX_MANAGER_FERNET_KEY\n"
            "    override existed when this backup was taken. This is a\n"
            "    DATABASE-ONLY backup -- it is NOT a complete recovery unit."
        )
        key_restore = """\
  3. WARNING: this backup contains NO key and none was configured when it was
     taken. Encrypted columns in the restored database can only be decrypted
     with the ORIGINAL key -- recover it separately (a prior backup, your
     secret store, an old volume). Do NOT mint a new key and expect existing
     secrets to work."""
    manifest = f"""\
Plex Manager -- pre-migration backup
=====================================

Created (UTC):        {timestamp}
Database revision:    {from_label}
Source database:      see the app's PLEX_MANAGER_DATABASE_URL at backup time
Source key:           {key_source}

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
{key_restore}
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
    # Sweep abandoned in-progress backups (a crash between ``mkdir`` and the
    # final publish rename). They never match the ``pre-migrate-*`` glob below
    # (distinct ``.tmp-`` prefix), so they neither count toward ``keep`` nor
    # shadow a real backup -- but they must not accumulate across failed starts.
    for partial in backups_root.glob(f"{_TMP_PREFIX}{_PREFIX}*"):
        if not partial.is_dir():
            continue
        try:
            shutil.rmtree(partial)
        except OSError:
            logger.warning(
                "Could not remove abandoned partial backup directory %s", partial, exc_info=True
            )
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

    # Guard against an OLDER image started against a NEWER-stamped database: the
    # stamped revision is then absent from this image's migration graph. Alembic
    # will refuse to upgrade from it, so backing up here would only snapshot the
    # already-migrated (useless-for-rollback) DB -- and, under
    # ``restart: unless-stopped``, every restart would _prune() away the genuine
    # pre-migration backup ADR-0021's recovery path relies on. Skip backup AND
    # prune, and log the mismatch honestly. (When head itself is undeterminable
    # we cannot trust the graph read, so fall through to a defensive backup.)
    if current is not None and head != _UNKNOWN_HEAD and not _revision_in_graph(current):
        logger.warning(
            "Database is stamped at revision %s, which is NOT in this image's "
            "migration graph -- most likely an OLDER image was started against a "
            "database already migrated by a NEWER one. Alembic cannot upgrade from "
            "an unknown revision, so there is no forward migration to guard; "
            "skipping the pre-migration backup AND prune so a genuine pre-migration "
            "backup is neither overwritten nor pruned away. Re-point the deployment "
            "at the image that matches revision %s (see ADR-0021).",
            current,
            current,
        )
        return None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    from_label = current or "base"
    backups_root = Path(settings.data_dir) / _BACKUP_SUBDIR
    final_name = f"{_PREFIX}{from_label}-{timestamp}"
    dest = backups_root / final_name
    # Build into a ``.tmp-`` staging dir and publish it via an atomic rename only
    # once the DB snapshot, key, and manifest are ALL written. A failure part-way
    # therefore never leaves a partial ``pre-migrate-*`` dir that _prune would
    # count toward its keep-limit or that recovery could mistake for a complete
    # restore unit; the staging dir is removed on failure and swept by _prune.
    staging = backups_root / f"{_TMP_PREFIX}{final_name}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        _snapshot_sqlite(db_path, staging / _DB_COPY_NAME)
        key_disposition = _copy_key(settings, staging)
        _write_manifest(staging, current, key_disposition)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    os.replace(staging, dest)

    logger.info(
        "Pre-migration backup written to %s (from rev %s, key=%s) -- "
        "restore this DB+key unit if the upgrade must be rolled back.",
        dest,
        current,
        key_disposition.name,
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
