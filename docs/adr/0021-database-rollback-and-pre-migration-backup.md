# ADR-0021: Database rollback policy and automatic pre-migration backup

- **Status:** Accepted
- **Date:** 2026-07-13
- **Context builds on:** [ADR-0003](0003-docker-ghcr-packaging.md) (mounted
  volume, tag-based rollback), [ADR-0004](0004-edge-stable-release-channels.md)
  (`:edge`/`:stable` promotion by re-tag), [ADR-0007](0007-sqlite-alembic-migrations.md)
  (SQLite + Alembic migrations from day one).

## Context

ADR-0003 and ADR-0004 describe "rollback = re-point an older image tag" and say
updates/rollbacks "never touch user data." That is true for the mounted
volume's *persistence* — the files survive a restart — but it is not true for
*schema state*: every container start runs `alembic upgrade head`
(`docker/entrypoint.sh`) before serving traffic (ADR-0007), and Alembic stamps
the database with the revision it just applied.

An independent reproduction confirmed the gap directly: upgrading a database to
head, then running the entrypoint-equivalent `alembic upgrade head` with the
*previous* migration set (simulating "roll back one release") fails outright —
`Can't locate revision identified by 'c86212dad733'`. An older image's Alembic
scripts do not know a newer revision id exists, so it cannot even determine
what to do with the stamped database; it does not run, let alone downgrade
gracefully. Tag-based rollback, as ADR-0003/0004 describe it, **never invokes
Alembic's `downgrade` functions at all** — it is not even in the failure path.
Separately, several existing `downgrade()` scripts are not a general
non-destructive fallback even if invoked manually: one refuses to run against
persisted "stalled" rows, another coerces statuses and drops download scopes,
others discard newer columns/tables outright. A fresh-database full-chain
downgrade test (if one existed) would not prove production-data compatibility,
because production data hits exactly the coercion/refusal paths a fresh,
empty database never exercises.

Separately, restoring a database backup is only useful if the app can still
decrypt what's in it: secrets are Fernet-encrypted at rest (ADR-0005) with a
key stored *outside* the database, at `<data_dir>/secret.key` (or the
`PLEX_MANAGER_FERNET_KEY` env override). A database-only backup — the common
shape for a Postgres `pg_dump`, and an easy mistake for a partial volume
backup — silently omits the one file a restored database cannot function
without: `prepare_encryption`'s already-initialized path refuses to mint a
replacement key precisely because a new key cannot decrypt old ciphertext, so
a keyless restore trades one outage for a permanent one.

## Decision

**1. Default posture: forward-fix.** Schema state is owned by migrations
(ADR-0007) and moves forward. This project does not commit to supporting
arbitrary production-data downgrades via Alembic's `downgrade()` path.

**2. Same-schema rollback stays valid.** When no migration ran between the
version being rolled back *from* and the version being rolled back *to*,
ADR-0003/0004's "rollback = re-point a tag" is exactly correct and remains the
mechanism — nothing about this ADR changes that case.

**3. Cross-migration rollback is NOT achievable by re-pointing a tag.** When a
migration ran, recovery is: stop the container, restore the pre-migration
database + encryption-key backup unit (below) over the current (post-migration)
files, then start the older image tag against the restored, correctly-stamped
database.

**4. Automatic pre-migration backup.** `docker/entrypoint.sh` runs
`python -m plex_manager.db_backup` immediately before `alembic upgrade head`.
`plex_manager.db_backup.create_pre_migration_backup`:

- Compares the database's current `alembic_version` row against the migration
  head defined by the scripts on disk. If they already match (the common
  case — a plain restart with no pending migration), it does **nothing** —
  backing up on every restart would fill the volume and defeat pruning. If the
  head cannot be determined (e.g. multiple heads), it backs up defensively
  rather than silently skipping.
- Otherwise, it snapshots the SQLite database via
  `sqlite3.Connection.backup()` — not a bare file copy, which would silently
  miss rows still sitting in the `-wal` file and produce a corrupt or
  stale-looking snapshot — into
  `<data_dir>/backups/pre-migrate-<from-rev>-<timestamp>/`, alongside a
  `MANIFEST.txt` restore runbook and — **in the key-file (default) disposition
  only** — a copy of the Fernet key (mode 0600 preserved), so the database and
  the key are backed up **as one unit**, addressing the recovery-unit gap
  directly (#221). A SQLite deployment using `PLEX_MANAGER_FERNET_KEY` instead
  gets a **database-only** backup directory: the env value is the key half of
  the recovery unit and is never embedded (any on-disk `secret.key` is absent
  or stale and is deliberately not copied — same reasoning as §6 below), and
  the `MANIFEST.txt` records which disposition applied. The backup directory
  is therefore self-contained only in the key-file disposition.
- Keeps **at most one backup per from-revision**: when a
  `pre-migrate-<from-rev>-*` directory already exists for the pending
  revision, no new snapshot is taken (and nothing is pruned) — the existing
  directory is logged as the recovery unit. This guards against the
  `restart: unless-stopped` retry loop of a migration that fails midway (some
  DDL applied, the version not yet stamped): every restart re-enters with the
  same from-revision, and a retry's snapshot would capture the
  **partially-migrated** database while looking newer than the genuinely
  clean first backup — which per-restart pruning would then eventually
  evict. The `MANIFEST.txt` restore runbook therefore ends by telling the
  operator to archive a used backup directory out of `backups/`, so a future
  upgrade attempt from that same revision takes a fresh snapshot.
- Prunes to the 5 most recent backup directories (sorted by modification time,
  not by directory name — the name embeds an arbitrary revision hash before
  the timestamp, which is not chronologically sortable). A defensive
  unknown-head backup never triggers this prune: no pending migration is
  confirmed in that state, so it must not be able to evict a genuine backup.
- Is **advisory and best-effort, not a substitute for an operator's own backup
  strategy** — see non-SQLite handling below.

**5. Fail-loud, never brick.** The entrypoint runs under `set -e`. A backup
failure (full disk, permissions fault, locked file) is caught, logged as a
loud `ERROR` naming exactly what happened and that the upgrade is proceeding
without a fresh snapshot, and then the process **returns normally (exit 0)** —
migrations and startup continue. This is a deliberate, documented trade-off:
an unprotected-but-running deployment beats a bricked one over a failed backup.
The broad `except Exception` in `db_backup.main()` exists for exactly this
reason and is not a silent swallow — it always emits the ERROR first.

**6. Non-SQLite (PostgreSQL) is out of scope for the automatic snapshot.**
`create_pre_migration_backup` detects a non-SQLite `database_url` and logs an
honest notice instead of silently doing nothing: the operator must snapshot
the database (e.g. `pg_dump`) AND separately preserve the key half of the
recovery unit themselves. Which key half depends on the deployment, exactly as
the SQLite path's `KeyDisposition` (§4) already distinguishes — an
unconditional "preserve `secret.key`" instruction is wrong for half of these
deployments:

- **Key file (the default):** the active key is `<data_dir>/secret.key` in the
  app container's data directory. Copy it alongside the `pg_dump` output and
  keep the two together.
- **`PLEX_MANAGER_FERNET_KEY` deployments:** the active key is the environment
  value, not a file. Any on-disk `secret.key` in this case is absent, or a
  stale leftover that no longer matches the active key — it must **not** be
  saved as the key half; doing so pairs the dump with the wrong key and
  produces an undecryptable restore. Preserve the `PLEX_MANAGER_FERNET_KEY`
  value from the environment/secret store instead.

Automating a Postgres snapshot from inside the app container is a larger,
credential-scoped feature and is deliberately deferred. See the README
"Backup & recovery" section for the exact commands for both cases.

## Alternatives considered

- **Backward-compatible migration window** (every migration stays
  forward/backward compatible for one release, à la some blue/green schemes) —
  rejected: this is a single-maintainer, home-server-scale project; the
  discipline tax on every migration is disproportionate to the risk it buys
  down, especially with the canary-first exercise (ADR-0004) already catching
  most migration bugs before any stable host sees them.
- **Fully supported production-data downgrades** (commit to every `downgrade()`
  script being safe to run against real data) — rejected: several already
  aren't (see Context), and retrofitting + maintaining that guarantee going
  forward is a standing tax this project's team size doesn't support. It would
  also require *running* Alembic downgrade against a database from a newer
  image, which the tag-promotion model (ADR-0004) doesn't naturally do anyway.
- **Forward-fix-only, no automatic snapshot** — rejected: it is honest but
  leaves the operator with no recovery unit at all when a migration does need
  reverting, which is precisely the gap #221/#222 identified. The backup is
  cheap (SQLite `backup()` is fast at this project's scale) and the win is
  large enough to justify the entrypoint's extra step.

## Consequences

**Positive**
- The rollback story documented to operators (README, overview) now matches
  what actually happens, closing the gap the independent reproduction found.
- A real recovery path exists for cross-migration rollback, not just same-schema.
- The database and its encryption key can no longer be backed up as two
  independently-drifting things by accident — the automatic path takes both
  whenever the key is a file (and records honestly, in `MANIFEST.txt`, when
  the key half instead lives in `PLEX_MANAGER_FERNET_KEY`), and the manual
  runbook (README) says so explicitly for Postgres.
- WAL-consistent snapshots mean the automatic backup reflects genuinely
  committed data, not a torn mid-write file.

**Negative / risks**
- The automatic backup is SQLite-only; Postgres deployments still depend on
  operator discipline for the pre-migration snapshot (mitigated by an honest
  log notice + README documentation, not silence).
- Disk usage: up to 5 backup generations live under `<data_dir>/backups/` —
  bounded by pruning, but a very large database multiplies that cost. Left as
  a known, bounded trade-off rather than tuned further at this scale.
- The broad `except Exception` in `db_backup.main()` is an intentional
  deviation from "no bare except" read narrowly — it is scoped to a single,
  well-understood advisory operation, always logs loudly, and never hides the
  failure from the operator (it hides it from `set -e` only, which is the
  entire point).
