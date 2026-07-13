# Contributing

Plex Manager is a personal/self-hosted project, but it is built to a consistent
standard. This guide covers the dev workflow.

## Prerequisites

- Python **3.12+**
- Node.js **22+** with npm
- FFmpeg (`ffprobe` is used to validate completed video files before import)
- Docker (for building/running the container)
- `make` (optional, for the shortcuts below)

## Setup

```bash
make install      # creates editable install with dev extras + installs pre-commit
make ui-install   # installs frontend dependencies used by make check
# equivalent to:
#   pip install -e ".[dev]"
#   pre-commit install
#   npm --prefix frontend ci
```

## Day-to-day

```bash
make lint     # ruff check
make format   # ruff format
make type     # pyright (strict)
make test     # pytest + coverage
make check    # backend + frontend gates — run this before pushing
make run      # run the app locally (http://localhost:8000)
```

`make run` starts the real first-run-capable server. First-run setup is claimed
by the first Plex server owner to sign in; the bootstrap token is optional
hardening, not a startup requirement. Set it only when you want setup to also
require `X-Setup-Token`:

```bash
python -c "import secrets; print('PLEX_MANAGER_SETUP_TOKEN=' + secrets.token_urlsafe(32))" >> .env
# or, for local API/docs probing only:
PLEX_MANAGER_DEV_AUTH_BYPASS=true make run
```

`PLEX_MANAGER_DEV_AUTH_BYPASS=true` is anonymous-admin, not just a token skip: it
bypasses browser sessions, CSRF, the setup-token, and role/ownership checks, and
setup completion skips the Plex-owner identity assertion. Keep it on the loopback
default (`make run` binds 127.0.0.1); never expose that process to the network.

After completing setup, the `/api/v1/setup/complete` response reveals the
install status only. Setup mints no app key. Browser access uses Plex sign-in
plus session cookies; generate the optional recovery/automation `X-Api-Key` from
Settings -> Access (`POST /api/v1/settings/app-key/rotate`) when you need API
automation or a break-glass credential.

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

### Release checklist

Run through this in order when cutting a real release (do not do this per
merge — only when actually preparing a promotion):

1. Curate `CHANGELOG.md`: move `[Unreleased]` to a new `## [x.y.z] - <date>`
   section, then restore an empty `## [Unreleased]` above it for the next cycle.
2. Bump the single version source: `src/plex_manager/__init__.py`'s
   `__version__` to `x.y.z`. This is the one place hatch (`pyproject.toml`),
   OpenAPI (`info.version`), and `events.current_build_id()`'s fallback all
   read from.
3. Run `make openapi` and commit the regenerated `docs/api/openapi.json` — its
   `info.version` must match the bump in step 2 (CI diffs this file).
4. Merge to `main`. CI builds `:edge` and the immutable `:edge-<sha>`; let the
   canary host prove the build.
5. Promote: run the **Promote to stable** workflow with that exact
   `edge-<sha>` and the same `x.y.z` from step 2. **The `x.y.z` you promote
   must equal the `__version__` baked into that `edge-<sha>` build** — until an
   automated image-label gate exists (tracked as a follow-up to #114), this is
   a human checklist step, not an enforced one. Getting steps 2–5 out of order
   is exactly how the app-reported version and the release tag end up silently
   disagreeing.
6. Remember: promotion re-tags an already-built image without rebuilding it, so
   the promoted image reports whatever `__version__` was baked in at *its*
   build time — bump the version (step 2) and merge it to `main` **before**
   the `:edge` build you intend to promote, not after.
