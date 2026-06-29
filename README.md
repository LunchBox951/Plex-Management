# Plex Manager

A self-hosted, unified media-request and automation service for Plex. It collapses
the traditional `Overseerr → Radarr/Sonarr → Prowlarr → qBittorrent` stack into a
single app — with two differences that define the project:

1. **Every failure has an in-app correction path** — never a terminal.
2. **The hard release-parsing/quality logic is borrowed, not reinvented**, so
   CAM/TS/telecast junk is *rejected outright* instead of slipping into your
   library.

> **Status:** foundation / design phase. This repository currently contains the
> design, the architecture decisions, the CI/security pipeline, and a minimal
> runnable skeleton. Feature work toward v1 begins next.

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
cp .env.example .env      # adjust bootstrap settings (port, image, database URL)
docker compose up -d      # pulls the image and starts the service
# then open http://<host>:8000 and complete the setup wizard
```

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
make check     # ruff (lint+format), pyright --strict, pytest
make run       # http://localhost:8000  (/health to verify)
```

Project layout and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Tech stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 + Alembic · pyright (strict)
· ruff · pytest · Docker / GHCR. See
[ADR-0002](docs/adr/0002-python-typed-stack.md).

## Documentation

- [Design overview](docs/design/overview.md)
- [Architecture Decision Records](docs/adr/)
- [Security policy](SECURITY.md)
- [Changelog](CHANGELOG.md)

## Acknowledgements

Metadata from [TMDB](https://www.themoviedb.org/) (this product uses the TMDB API
but is not endorsed or certified by TMDB). Integrates with
[Prowlarr](https://prowlarr.com/) and [qBittorrent](https://www.qbittorrent.org/).

## License

[MIT](LICENSE) © 2026 LunchBox951
