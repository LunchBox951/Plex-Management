# ADR-0007: SQLite + Alembic migrations from day one

- **Status:** Accepted — 2026-06-29
- **Deciders:** LunchBox951 (owner)

## Context

The prototype used SQLite that **auto-initialized** with no migration system, so
there was no safe upgrade path for schema changes — the single biggest plumbing
gap called out in its own limitations. v2 ships continuously to two hosts at
different maturities (see [ADR-0004](0004-edge-stable-release-channels.md)), so
schema must evolve safely across versions and channels.

## Decision

Use **SQLite** as the v1 database, accessed through **typed SQLAlchemy 2.0**, with
**Alembic migrations from the very first commit**. The schema is owned by
versioned migrations, never auto-created in place. The data layer is written so
that moving to **PostgreSQL** is a connection-string/config change, not a rewrite
(avoid SQLite-only SQL).

Migrations run on container start before the app serves traffic; combined with
the channel model, a migration always executes on the canary (`:edge`) host
before it can reach a stable host.

## Consequences

**Positive**
- Safe, reviewable, reversible schema evolution across releases.
  > **Qualified by [ADR-0021](0021-database-rollback-and-pre-migration-backup.md):**
  > "reversible" describes the forward migration chain being reviewable and
  > incremental, not that Alembic's `downgrade` scripts are a general,
  > non-destructive path back on **production data** — several refuse or
  > coerce persisted rows. Tag-based rollback never invokes `downgrade` at all.
- The promotion gate guarantees migrations are exercised on the canary first.
- Postgres is available later without a data-layer rewrite.

**Negative / risks**
- Every schema change must ship with a migration — enforced by review/CI.
- SQLite has concurrency limits; acceptable at home-server scale and revisited if
  multi-user/Postgres lands.

## Alternatives considered

- **Auto-initialized SQLite, no migrations (prototype)** — rejected as the known
  root cause of having no upgrade path.
- **PostgreSQL from day one** — unnecessary operational weight for v1's scale;
  deferred behind a clean data layer instead.
