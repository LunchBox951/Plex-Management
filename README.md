# Plex Manager

A self-hosted, unified media-request and automation service for Plex. It collapses
the traditional `Overseerr → Radarr/Sonarr → Prowlarr → qBittorrent` stack into a
single app — with two differences that define the project:

1. **Every failure has an in-app correction path** — never a terminal.
2. **The hard release-parsing/quality logic is borrowed, not reinvented**, so
   CAM/TS/telecast junk is *rejected outright* instead of slipping into your
   library.

> **Status:** backend alpha (the **request → search → grab** slice). The pure
> decision engine, persistence, the live Prowlarr/qBittorrent/TMDB adapters, the
> reconciler, and the REST API are built and tested; file import, Plex dedupe,
> retention, and the front-end are deferred. See
> [docs/design/alpha-plan.md](docs/design/alpha-plan.md).

## What works now (backend alpha)

- **First-run setup wizard** (`/api/v1/setup/*`): validate and store
  Plex/Prowlarr/qBittorrent/TMDB credentials — encrypted at rest, never logged.
- **API-key auth** (`X-Api-Key`) on every protected route; a setup guard blocks
  the API until the install is initialized.
- **TMDB discovery** → **request** a movie/show (anime auto-tagged).
- **`/api/v1/search-preview`** — the headline: searches Prowlarr, parses each
  release with `guessit`, runs the Radarr-style **ordered quality profile with a
  hard cutoff**, and returns ranked candidates with a per-release *rejection
  reason*. CAM/TS/TELECINE/WORKPRINT/DVD-screener releases are **rejected
  outright**, and "no acceptable release" is a real, retryable status — never a
  silent failure.
- **Grab** the chosen release into qBittorrent and **reconcile** its status; a
  single missed poll never falses a download, failures are blocklisted and
  re-searched, and the blocklist is operator-manageable.

The typed contract for all of this is published at
[`docs/api/openapi.json`](docs/api/openapi.json) (regenerate with `make openapi`).

**Deferred** (ports defined, adapters stubbed): file import (validate → rename →
route → Plex scan), Plex availability dedupe, disk-pressure eviction, retention,
Plex OAuth, and the front-end.

## Why

The previous prototype worked on the happy path but had no escape hatch when it
went wrong: wrong media, bad-quality files, or download-client/database drift all
required SSH access to delete files, hand-edit SQLite, or clear qBittorrent. v2 is
designed around correctability and standing on proven release-handling logic.

Read the full rationale in **[docs/design/overview.md](docs/design/overview.md)**
and the decisions in **[docs/adr/](docs/adr/)**.

## Architecture in one paragraph

An integrated app owns the entire "brain" (discovery, parsing decisions, quality
profiles, blocklist, import, retention, correction, and the web UI) and delegates
only the actual downloading to a pluggable **download client** (qBittorrent is the
v1 adapter). It is structured ports-and-adapters: a pure domain core talks to the
outside world (TMDB, Prowlarr, qBittorrent, Plex, filesystem) only through typed
interfaces. See [ADR-0001](docs/adr/0001-integrated-app-borrowed-brains.md).

## Deploying (operators)

Plex Manager ships as a Docker image on the GitHub Container Registry. Installing
is a one-time admin step; **everything after that — setup, configuration,
recovery, troubleshooting — happens in the browser** (see
[ADR-0005](docs/adr/0005-zero-terminal-web-operability.md)).

```bash
cp .env.example .env
python -c "import secrets; print('PLEX_MANAGER_SETUP_TOKEN=' + secrets.token_urlsafe(32))" >> .env
# adjust image, bind mounts, database URL, and host bind/port as needed
docker compose up -d
# then open http://127.0.0.1:8000 and enter the setup token from .env
```

Before starting the container, set `PLEX_MANAGER_MEDIA_ROOT` and
`PLEX_MANAGER_DOWNLOADS_ROOT` in `.env` to host directories that contain the Plex
libraries and qBittorrent downloads. They are mounted as `/media` and `/downloads`
inside the container; the setup wizard paths must use those in-container paths.
The stock compose file publishes only on `127.0.0.1` and requires
`PLEX_MANAGER_SETUP_TOKEN` so a fresh uninitialized install cannot be claimed
remotely. Use an SSH tunnel or reverse proxy for first setup; only set
`PLEX_MANAGER_HOST_BIND=0.0.0.0` when the host is intentionally exposed.

Each host is *designed* to auto-pull its release channel (the updater mechanism —
Watchtower vs. a systemd timer — is an open decision and is not bundled in the
compose file yet). Your config and database live in a mounted volume and are
untouched by updates. See
[ADR-0003](docs/adr/0003-docker-ghcr-packaging.md) and
[ADR-0004](docs/adr/0004-edge-stable-release-channels.md).

## Developing

Requires Python 3.12+.

```bash
make install   # editable install + dev tools + pre-commit
make migrate   # apply Alembic migrations (creates ./data and the schema)
make check     # ruff (lint+format), pyright --strict, pytest
make run       # http://localhost:8000  (/health to verify, /docs for the API)
make openapi   # regenerate docs/api/openapi.json from the live app
```

Project layout and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Tech stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) + Alembic · `httpx` ·
`guessit` (release parsing) · pyright (strict) · ruff · pytest · Docker / GHCR.
See [ADR-0002](docs/adr/0002-python-typed-stack.md) and
[ADR-0008](docs/adr/0008-release-parser-guessit.md).

## Documentation

- [Design overview](docs/design/overview.md)
- [Backend alpha scope & plan](docs/design/alpha-plan.md)
- [REST API contract (OpenAPI)](docs/api/openapi.json)
- [Architecture Decision Records](docs/adr/)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

## Acknowledgements

Metadata from [TMDB](https://www.themoviedb.org/) (this product uses the TMDB API
but is not endorsed or certified by TMDB). Integrates with
[Prowlarr](https://prowlarr.com/) and [qBittorrent](https://www.qbittorrent.org/).

## License

[MIT](LICENSE) © 2026 LunchBox951
