"""Advisory pre-migration backup of the SQLite database + Fernet key (ADR-0023).

Run as ``python -m plex_manager.db_backup`` from the container entrypoint,
*before* ``alembic upgrade head``. When a migration is actually pending, this
snapshots the SQLite database (WAL-consistent, via :func:`sqlite3.Connection.backup`
-- not a bare file copy, which would silently miss uncommitted WAL frames) and
the Fernet encryption key as **one recovery unit** into
``<data_dir>/backups/pre-migrate-<from-rev>-<timestamp>/``, alongside a
human-readable ``MANIFEST.txt`` restore runbook. Backups are pruned to the
most recent :data:`_KEEP_COUNT`, with two guards that both protect the same
thing -- the one genuinely clean pre-migration snapshot -- from
``restart: unless-stopped`` retry loops:

1. **At most one backup per from-revision.** A migration that fails midway
   (some DDL applied, the version not yet stamped) re-enters on every restart
   with the same from-revision; the first backup is the genuinely
   pre-migration snapshot, while a retry's snapshot would capture the
   partially-migrated database *and look newer*. Repeats are therefore
   skipped (and skip pruning) rather than snapshotted -- so retry backups can
   neither masquerade as clean recovery units nor feed :func:`_prune` until
   it evicts the real one.
2. **A defensive unknown-head backup never triggers a prune** (see
   :data:`_UNKNOWN_HEAD`): when the head itself cannot be determined, no
   pending migration is confirmed, so such a backup must not be able to evict
   a genuine one.

See :func:`create_pre_migration_backup`.

This is advisory, not a replacement for an operator's own backup strategy
(Postgres deployments are explicitly out of scope -- see :func:`_sqlite_file_path`)
and it is deliberately **fail-loud but never fatal**: :func:`main` always exits
0 so the entrypoint's ``set -e`` never bricks a container start over a failed
backup. See ADR-0023 for the policy this implements and the reasoning against
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
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
    """Return the on-disk SQLite file path, or ``None`` when there is none.

    ``None`` means "no on-disk SQLite database file to snapshot": a non-SQLite
    backend, or an in-memory SQLite database.

    Uses SQLAlchemy's URL parser rather than hand-stripping a ``sqlite:///``
    prefix so both absolute (``sqlite:////abs/path.db``) and relative
    (``sqlite:///./data/plex_manager.db``) URLs resolve correctly. SQLAlchemy's
    SQLite **URI form** (``sqlite:///file:/abs/path.db?uri=true``) is also
    handled: there the ``database`` component is itself a SQLite ``file:`` URI
    whose path SQLite percent-decodes before opening -- treating it as a
    literal filesystem path would point at a nonexistent ``file:...`` name, so
    the backup hook would log "No existing database" and silently skip the one
    pre-migration snapshot the deployment relies on. The path is therefore
    extracted from the URI (and percent-decoded) exactly as SQLite itself does.
    """
    url = make_url(sync_database_url(settings.database_url))
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    database = url.database
    if database.startswith("file:"):
        # SQLAlchemy keeps its own query separate (``uri=true``, ``mode=...``
        # land in ``url.query``), but honour a memory-mode param wherever it
        # appears: there is no file to back up for an in-memory database.
        if url.query.get("mode") == "memory":
            return None
        parsed = urlsplit(database)
        if "memory" in parse_qs(parsed.query).get("mode", []):
            return None
        database = unquote(parsed.path)
    if not database or database == ":memory:":
        return None
    return Path(database)


def _sqlite_ro_uri(path: Path) -> str:
    """Build a read-only SQLite ``file:`` URI for ``path``, percent-escaping it.

    A bare ``f"file:{path}?mode=ro"`` breaks when the filesystem path contains
    URI-reserved characters: a ``?`` in the path starts the query string early
    (so ``mode=ro`` is silently dropped and part of the filename becomes a bogus
    parameter) and a ``#`` truncates the path at the fragment -- SQLite then
    opens the WRONG file, or creates one. Python's ``sqlite3`` docs recommend
    percent-encoding the path portion of a file URI for exactly this reason;
    ``urllib.parse.quote`` keeps ``/`` intact (its default ``safe``) while
    escaping the reserved characters, and SQLite percent-decodes the path
    before opening it.
    """
    return f"file:{quote(str(path))}?mode=ro"


def _current_revision(db_path: Path) -> str | None:
    """Read the ``alembic_version`` row from an existing SQLite file, read-only.

    Returns ``None`` **only** when the ``alembic_version`` table does not exist
    yet (a database file that predates Alembic, or a partially-initialized one)
    -- callers treat that the same as "base" (nothing applied).

    A missing table is the ONLY "nothing applied" signal, and it is detected
    explicitly via ``sqlite_master`` rather than by broadly catching
    ``OperationalError`` around the ``SELECT version_num``. That same exception
    is *also* raised for an exclusive lock, a malformed/foreign
    ``alembic_version`` table, or a disk I/O error -- none of which mean "base".
    Mapping those to ``None`` would let :func:`create_pre_migration_backup`
    publish and prune around a misleading ``pre-migrate-base-*`` unit before
    Alembic itself later fails on the same database. Instead they propagate to
    :func:`main`, which logs loudly and takes the documented fail-loud /
    no-fresh-backup path (ADR-0023).
    """
    conn = sqlite3.connect(_sqlite_ro_uri(db_path), uri=True)
    try:
        table_present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'alembic_version'"
        ).fetchone()
        if table_present is None:
            # Unstamped/legacy DB file -- Alembic has never run against it.
            return None
        # The table exists: any read error now is a genuine failure (lock,
        # corruption, wrong schema), not a legacy DB, so let it propagate.
        row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        return str(row[0]) if row is not None else None
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
    src_conn = sqlite3.connect(_sqlite_ro_uri(src), uri=True)
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
  4. Copy {_KEY_COPY_NAME} from this backup back into the data directory as
     secret.key, preserving mode 0600 (`chmod 600 secret.key`)."""
    elif key_disposition is KeyDisposition.ENV_OVERRIDE:
        key_source = "PLEX_MANAGER_FERNET_KEY (env override -- NOT embedded in this backup)"
        key_line = (
            "  - (no key file -- this install's key is supplied via the\n"
            "    PLEX_MANAGER_FERNET_KEY environment variable/secret, which is NOT\n"
            "    embedded in this backup; preserve that value separately)"
        )
        key_restore = """\
  4. Ensure the SAME PLEX_MANAGER_FERNET_KEY value that was active at backup
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
  4. WARNING: this backup contains NO key and none was configured when it was
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
  3. Remove any <db>-wal and <db>-shm sidecar files sitting next to the
     database file you just restored (e.g. plex_manager.db-wal,
     plex_manager.db-shm), if present. {_DB_COPY_NAME} is a STANDALONE
     snapshot taken via the SQLite backup API -- it is already complete on its
     own. Leftover WAL frames from whatever database was running most
     recently belong to a DIFFERENT (newer) database file than the one you
     just restored; SQLite replays them on open, which can silently
     reintroduce the newer schema/data you are trying to roll back away from.
{key_restore}
  5. Restore file OWNERSHIP if you copied the files from the host (typically
     as root, for Docker named-volume restores): the official image runs the
     app as the non-root user `appuser` (UID 10001), which must be able to
     WRITE the database file and READ the key. Root-owned copies leave the
     restored install unable to write its own database (or read its key):
       chown 10001:10001 {_DB_COPY_NAME}          # and secret.key, if restored
     (keep secret.key at mode 0600; adjust the UID if your deployment
     overrides the container user).
  6. Re-point the deployment at the older image tag that matches this
     database revision (same-schema rollback: just re-point; cross-migration
     rollback: this backup unit IS the recovery path -- see ADR-0023).
  7. Start the container and verify: sign in, and confirm a stored credential
     (e.g. a configured service) still decrypts correctly.
  8. Move this backup directory out of <data_dir>/backups/ (keep it as an
     archive elsewhere). At most ONE backup is kept per from-revision -- so a
     failed migration's restart loop cannot bury the clean snapshot under
     partially-migrated retries -- which means a future upgrade attempt from
     this same database revision will find this directory and skip taking a
     fresh pre-migration snapshot until it is moved away.

See docs/adr/0023-database-rollback-and-pre-migration-backup.md and the
README "Backup & recovery" section for the full policy.
"""
    (dest_dir / _MANIFEST_NAME).write_text(manifest, encoding="utf-8")


def _prune(backups_root: Path, keep: int = _KEEP_COUNT, *, protect: Path | None = None) -> None:
    """Keep only the most recent ``keep`` backup directories under ``backups_root``.

    Sorted by ``st_mtime``, not by name: the directory name is
    ``pre-migrate-<from-rev>-<timestamp>``, and ``<from-rev>`` is an arbitrary
    Alembic revision hash that sorts lexicographically ahead of or behind later
    revisions' hashes with no relation to time -- a pure name-sort is NOT
    reliably chronological. Each removal is isolated so one failure (e.g. a
    permissions fault) doesn't stop the rest from being pruned.

    ``protect`` (the just-published backup) is ALWAYS kept and reserves one of
    the ``keep`` slots, *regardless of its mtime*. If ``backups_root`` already
    holds ``keep`` entries whose mtimes are newer than the fresh backup -- e.g.
    archived backups were copied back into the directory, or the system clock
    moved backward between starts -- a pure mtime sort would rank the fresh
    backup oldest and delete the very unit we just wrote (and logged) before the
    migration even runs. Excluding it by identity (not by mtime) closes that
    hole. Omitting ``protect`` preserves the original keep-N behaviour for other
    callers.
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
    # Resolve the protected dir once so the identity check is robust to symlinks
    # or path spelling; only honour it if it actually exists as a directory.
    protected = protect.resolve() if protect is not None and protect.is_dir() else None
    candidates = sorted(
        (
            p
            for p in backups_root.glob(f"{_PREFIX}*")
            if p.is_dir() and (protected is None or p.resolve() != protected)
        ),
        key=lambda p: p.stat().st_mtime,
    )
    # The protected fresh backup occupies one keep slot; keep ``keep - 1`` of the
    # rest. When keep is 0 (or the reserved slot consumes it) everything else goes.
    keep_others = max(keep - (1 if protected is not None else 0), 0)
    for stale in candidates[:-keep_others] if keep_others > 0 else candidates:
        try:
            shutil.rmtree(stale)
        except OSError:
            logger.warning("Could not prune stale backup directory %s", stale, exc_info=True)


def create_pre_migration_backup(settings: Settings | None = None) -> Path | None:
    """Snapshot the DB + Fernet key as one unit when a migration is pending.

    Returns the created backup directory, or ``None`` when no backup was
    written (non-SQLite backend, no existing database, already at head, or a
    backup for the same pending from-revision already exists and remains the
    recovery unit -- every case is logged honestly so the operator knows why
    nothing happened).
    """
    settings = settings or get_settings()

    db_path = _sqlite_file_path(settings)
    if db_path is None:
        logger.info(
            "No on-disk SQLite database file (non-SQLite backend, or an "
            "in-memory database); automatic pre-migration file backup is not "
            "performed. Snapshot the database AND the encryption key "
            "externally before upgrades (see ADR-0023 / README Backup & recovery)."
        )
        return None

    if not db_path.exists():
        logger.info("No existing database at %s (fresh install); nothing to back up.", db_path)
        return None

    current = _current_revision(db_path)
    head = _head_revision()
    is_defensive_unknown_head = head == _UNKNOWN_HEAD
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
    # pre-migration backup ADR-0023's recovery path relies on. Skip backup AND
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
            "at the image that matches revision %s (see ADR-0023).",
            current,
            current,
        )
        return None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    from_label = current or "base"
    backups_root = Path(settings.data_dir) / _BACKUP_SUBDIR

    # At most ONE backup per from-revision. Under ``restart: unless-stopped``,
    # a migration that fails midway (some DDL applied, the version not yet
    # stamped) re-enters this function on every restart with the SAME
    # from-revision. The FIRST backup is the genuinely pre-migration snapshot;
    # a retry's snapshot would capture the partially-migrated database while
    # looking newer -- and feeding one new backup per restart into _prune()
    # would eventually evict the only clean recovery unit. So when a backup for
    # this from-revision already exists, it IS the recovery unit: take nothing,
    # prune nothing, and say so honestly. (An operator who deliberately rolled
    # back and wants a fresh snapshot archives the old directory first -- the
    # MANIFEST's final restore step says exactly that.)
    if backups_root.is_dir():
        existing = sorted(p for p in backups_root.glob(f"{_PREFIX}{from_label}-*") if p.is_dir())
        if existing:
            logger.warning(
                "A pre-migration backup for revision %s already exists (%s): an "
                "earlier container start already snapshotted this migration's "
                "pre-state -- most likely the migration failed and the container "
                "is retrying. That EXISTING backup is the recovery unit; a fresh "
                "snapshot now could capture a partially-migrated database, so "
                "none is taken and nothing is pruned. If you deliberately "
                "restored/rolled back and want a fresh snapshot, move the "
                "existing pre-migrate-%s-* directories out of %s first.",
                from_label,
                existing[-1],
                from_label,
                backups_root,
            )
            return None

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

    if is_defensive_unknown_head:
        # Do NOT prune here. This backup was forced because the migration head
        # itself is undeterminable, not because a confirmed pending migration
        # was found -- so it must not be able to evict a genuine pre-migration
        # backup that guards the last successful migration. (The same-from-rev
        # dedupe above already bounds a restart loop to at most one such
        # defensive backup per from-revision.)
        logger.warning(
            "Migration head could not be determined; skipping the post-backup "
            "prune so this defensive backup cannot evict an existing genuine "
            "pre-migration backup. Prune %s manually if disk usage becomes a "
            "concern.",
            dest.parent,
        )
    else:
        # Protect the unit we just wrote: never let a mtime sort (skewed clock,
        # or archived backups copied back with newer mtimes) evict the fresh
        # pre-migration backup we just logged.
        _prune(dest.parent, protect=dest)
    return dest


def main() -> None:
    """Entrypoint hook: back up if needed, but NEVER fail the container start.

    The broad ``except Exception`` here is deliberate and honesty-compliant: it
    emits a loud ERROR (never a silent swallow) and always returns normally
    (exit 0), so a failed backup -- a full disk, a permissions fault, a locked
    file -- trades an *unprotected but running* startup for never bricking a
    container over a snapshot (the entrypoint runs under ``set -e``). See
    ADR-0023.
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
