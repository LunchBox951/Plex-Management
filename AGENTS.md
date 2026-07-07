# AGENTS.md

Guidance for coding agents working in this repository. Keep this aligned with
`CLAUDE.md` and the linked docs.

## Project

Plex Manager v2 is a self-hosted, unified Plex request and automation service.
Read `docs/design/overview.md` first, then the ADRs in `docs/adr/` for durable
decisions.

Current status: v1 beta, feature-complete for the request -> watchable ->
correct loop across movies, TV, and anime. Browser access uses Plex sign-in with
session cookies; `X-Api-Key` is an optional recovery/automation credential.

## Architecture

The codebase is ports-and-adapters:

```text
src/plex_manager/
  domain/       pure core; no I/O, web, ORM, adapter, or repository imports
  ports/        protocols the core/services depend on
  adapters/     TMDB, Prowlarr, qBittorrent, Plex, parser, filesystem
  services/     orchestration and application behavior
  repositories/ SQLAlchemy persistence implementations
  web/          FastAPI app, schemas, dependencies, routers, SPA serving
  config.py     pydantic-settings bootstrap config
  db.py         SQLAlchemy base/session helpers
migrations/     Alembic migrations; schema is owned by versioned migrations
frontend/       typed React SPA generated from the OpenAPI contract
```

The `domain` package must stay pure. When domain code needs enum-like values,
duplicate string values locally rather than importing ORM models.

## Workflow

- Prefer existing patterns and helpers over new abstractions.
- Keep changes scoped to the requested behavior or verified documentation drift.
- Preserve unrelated local changes; do not revert user work.
- Do not create commits unless explicitly asked. When committing is requested,
  use Conventional Commits (`docs:`, `fix:`, `chore:`, etc.).
- Every model/schema change needs an Alembic migration.
- Backend API shape changes require `make openapi` and `make gen-client`.
- ADRs are durable decision records. Supersede decisions with a new ADR; only
  edit existing ADRs to correct factual drift or historical notes.

## Verification

Use the narrowest checks that cover your change, then run broader gates when the
blast radius warrants it:

```bash
make lint
make type
make test
make ui-check
make check
```

If the system Python lacks `pip`, use the repo-compatible `uv` path:

```bash
uv run --python /home/lunchbox/.local/bin/python3.12 --extra dev pytest
```

For docs-only changes, at minimum run a fast grep/format sanity check and any
targeted tests needed to prove referenced behavior.

## Security And Logging

- Never log raw request-derived values. Use `plex_manager.logsafe.safe_int` or
  `safe_text` before values enter message args or `extra={...}`.
- Never log credentials, tokens, passwords, API keys, or full secret-bearing
  URLs. The log capture pipeline stores already-formatted messages.
- First-run setup is claimed by the first Plex server owner sign-in.
  `PLEX_MANAGER_SETUP_TOKEN` is optional hardening and is sent as
  `X-Setup-Token` when configured.
