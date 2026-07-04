# Contributing

Plex Manager is a personal/self-hosted project, but it is built to a consistent
standard. This guide covers the dev workflow.

## Prerequisites

- Python **3.12+**
- Docker (for building/running the container)
- `make` (optional, for the shortcuts below)

## Setup

```bash
make install      # creates editable install with dev extras + installs pre-commit
# equivalent to:
#   pip install -e ".[dev]"
#   pre-commit install
```

## Day-to-day

```bash
make lint     # ruff check
make format   # ruff format
make type     # pyright (strict)
make test     # pytest + coverage
make check    # all of the above — run this before pushing
make run      # run the app locally (http://localhost:8000)
```

CI runs the same gates (`make check`), so green locally ≈ green in CI. The `ruff`
and `pyright` versions are pinned exactly in `pyproject.toml` and mirrored in
`.pre-commit-config.yaml`, so the hook, local, and CI agree. Dependabot does not
track pre-commit hook revisions — run `pre-commit autoupdate` periodically (and
re-pin `ruff` in `pyproject.toml` to match) to keep them current.

## Project layout

```
src/plex_manager/
  web/        # FastAPI app + REST/UI (an adapter)
  domain/     # pure core: decision engine, state machine, reconciler (no I/O)
  ports/      # interfaces the core depends on (MetadataPort, DownloadClientPort, …)
  adapters/   # implementations of ports (TMDB, Prowlarr, qBittorrent, Plex, FS)
  config.py   # settings (pydantic-settings)
  db.py       # SQLAlchemy declarative base (engine/session added in v1)
migrations/   # Alembic migrations (schema is owned by versioned migrations)
tests/
docs/design/  # design overview
docs/adr/     # architecture decision records
```

The architecture is ports-and-adapters: the `domain` core never imports an
adapter directly. See [docs/design/overview.md](docs/design/overview.md).

## Conventions

- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, …).
- **Branches:** short-lived feature branches; open a PR into `main`.
- **Types:** code must pass `pyright --strict`. Annotate public functions.
- **Errors:** no bare `except:` that swallows errors; surface failures as explicit,
  visible states (see the north stars in the design overview).
- **Schema changes:** every model change ships with an Alembic migration.
- **Significant decisions:** add an ADR under `docs/adr/` (copy the format of an
  existing one). ADRs are immutable; supersede rather than edit.
- **Logging request-derived values:** never log a request-derived value
  (`tmdb_id`, `request_id`/`media_request_id`, `download_id`, …) raw. Pass it
  through a `plex_manager.logsafe` barrier — `safe_int` for ids/counts,
  `safe_text` for strings — whether the value goes in the message args OR in
  `extra={...}`, e.g.
  `_logger.warning("availability check failed", extra={"tmdb_id": safe_int(tmdb_id)})`.
  CodeQL's `py/log-injection` taints values inside `extra=` exactly as it taints
  message args, so merely moving an id into `extra=` does **not** clear the alert
  (that was the lesson of issue #23 / alert #238); the barrier belongs at the log
  site regardless of where the value lands. The barriers are honest, not
  cosmetic: `safe_int` re-coerces with `int()` (a no-op for a real int, a hard
  type enforcement + analyzer taint barrier otherwise) and `safe_text` collapses
  CR/LF so a string cannot forge a second log record. `extra=` remains the home
  for correlation ids: `LOG_EVENT_CORRELATION_KEYS` (see ADR-0012) picks those
  structured fields up for the cross-request trail surfaced by
  `GET /ops/logs/export`. Only values that trace from an HTTP request need the
  barrier; a value read straight off a SQLAlchemy row (a DB-generated id, a
  stored column) is already trusted and does not.

## Release flow

`main` → CI builds `:edge` → the canary host runs it → once proven, promote the
*same image* to `:stable` (no rebuild) via the **Promote to stable** workflow. See
[ADR-0004](docs/adr/0004-edge-stable-release-channels.md).
