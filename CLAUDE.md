# CLAUDE.md

Guidance for AI assistants (and humans) working in this repository. Keep it
short; the source of truth is the linked docs.

## What this is

Plex Manager **v2** — a self-hosted, unified media-request and automation service
that collapses the `Overseerr → Radarr/Sonarr → Prowlarr → qBittorrent` stack
into a single app. It is a ground-up rewrite of the `prototype/` service. Read
[`docs/design/overview.md`](docs/design/overview.md) first; every major decision
has an ADR under [`docs/adr/`](docs/adr/).

Status: v1 beta, feature-complete — the request → watchable → correct loop for
movies, TV, and anime is built and tested, and a 7-day live beta run is set to
begin. v1.x feature work is still gated on explicit tasking.

## North stars (don't violate these)

1. **Correction without a terminal.** Every failure mode has a first-class in-app
   correction path. Where the system can't self-heal, it hands the operator a
   *button*, never a terminal.
2. **The `:stable` deployment is 100% web-operable.** The terminal is an
   admin-only, install-time tool — never required for use, config, recovery, or
   troubleshooting.
3. **Honesty over silence.** Surface states, don't swallow them. No bare `except`
   that hides errors; "no acceptable release found" is a visible, retryable
   status, not a silent `failed`. Secrets are never logged.
4. **Borrow proven brains.** Do not re-derive release parsing or quality ranking;
   stand on a battle-tested parser + a Radarr-style quality model ([ADR-0001](docs/adr/0001-integrated-app-borrowed-brains.md)).

## Architecture

Ports-and-adapters (hexagonal). The `domain` core is pure and **never imports an
adapter**:

```
src/plex_manager/
  domain/    # pure core: decision engine, state machine, reconciler (no I/O)
  ports/     # interfaces the core depends on (MetadataPort, DownloadClientPort, …)
  adapters/  # implementations of ports (TMDB, Prowlarr, qBittorrent, Plex, FS)
  web/       # FastAPI app + REST/UI (an adapter)
  config.py  # pydantic-settings
  db.py      # SQLAlchemy declarative base
migrations/  # Alembic — the schema is owned by versioned migrations
```

## Working in this repo

- **Quality gates must pass before a change is done:** `make check` runs
  `ruff check`, `ruff format --check`, `pyright` (**strict**), and `pytest`. Code
  must be fully typed; annotate public functions.
- **Schema changes ship with an Alembic migration** — no exceptions. Generate the
  revision with `alembic revision --autogenerate`; apply migrations with
  `make migrate` (`alembic upgrade head`).
- **ADRs are immutable.** To change a decision, add a new ADR that supersedes the
  old one; don't rewrite an accepted ADR.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `chore:`, …). The maintainer
  owns the commit/release cadence — don't create commits unless asked.
- See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow.

## Release model

Two channels by image tag with a manual promotion gate ([ADR-0004](docs/adr/0004-edge-stable-release-channels.md)):

- `main` → CI builds and pushes `:edge` (+ immutable `:edge-<sha>`). The **canary
  (beta) fleet** auto-pulls `:edge`.
- Once an edge build proves itself, the maintainer **promotes** it by re-tagging
  that exact image (no rebuild) as `:stable` + `:x.y.z`. **Stable deployments**
  auto-pull `:stable`, so they run bit-identical bytes to what was tested.

## Reference repositories

`prototype/`, `ombi/`, `overseerr/`, `radarr/`, `sonarr/`, `prowlarr/`, `jackett/`
are read-only clones for reference (cloned by `init.sh`, gitignored). They are
**not** part of this project — study them, never edit them, and never treat their
files as ours.
