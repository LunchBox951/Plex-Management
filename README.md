# Plex Manager

A self-hosted, unified media-request and automation service for Plex. It collapses
the traditional `Overseerr → Radarr/Sonarr → Prowlarr → qBittorrent` stack into a
single app — with two differences that define the project:

1. **Every failure has an in-app correction path** — never a terminal.
2. **The hard release-parsing/quality logic is borrowed, not reinvented**, so
   CAM/TS/telecast junk is *rejected outright* instead of slipping into your
   library.

> **Status:** v1 beta, feature-complete. The full request → watchable → correct
> loop is built and tested for movies, TV, and anime — search, grab, reconcile,
> import, Plex scan, Plex availability dedupe, disk-pressure eviction,
> operability (health/logs/retention/eviction), auto-grab, and in-app correction
> surfaces. A 7-day live beta run is set to begin gathering real-world data ahead
> of a v1 stable promotion. Browser Plex sign-in/session auth is built; the
> bundled first-party container updater is available as an opt-in Compose
> profile.

## What works now (beta)

- **First-run setup wizard** (`/api/v1/setup/*`): validate and store
  Plex/Prowlarr/qBittorrent/TMDB credentials — encrypted at rest, never logged.
- **Plex browser sign-in** with HTTP-only session cookies for normal UI access,
  plus an optional `X-Api-Key` recovery/automation key on protected routes; a
  setup guard blocks the protected API until the install is initialized.
- **Web UI** for setup, discovery, requests, queue management, status, logs,
  settings, blocklist, and quality-profile inspection.
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
- **Import and Plex reconciliation**: validate completed downloads, place movie
  files and TV episodes under the configured library root, scan Plex, detect
  existing Plex availability, and surface import-blocked cases for correction.
- **Operability tools**: health checks, live logs, disk usage, retention
  settings, and automatic watched-media eviction (default-on, disk-pressure-
  triggered; see Deploying).
- **Container update controls**: an opt-in, digest-aware updater sidecar with
  app-owned scheduling, idle/drain coordination, health-gated replacement, and
  automatic rollback to the previous application image on failed startup.

The typed contract for all of this is published at
[`docs/api/openapi.json`](docs/api/openapi.json) (regenerate with `make openapi`).

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
# adjust image, bind mounts, database URL, and host bind/port as needed
docker compose up -d
# then open http://127.0.0.1:8000 and sign in with the Plex server owner
```

Before starting the container, set `PLEX_MANAGER_MEDIA_ROOT` and
`PLEX_MANAGER_DOWNLOADS_ROOT` in `.env` to host directories that contain the Plex
libraries and qBittorrent downloads. They are mounted as `/media` and `/downloads`
inside the container; the setup wizard paths must use those in-container paths.
`PLEX_MANAGER_DOWNLOADS_ROOT` additionally serves a second role: Plex Manager
sends its **literal host value** to qBittorrent as each torrent's save path, so
qBittorrent must be able to write that exact path — a host qBittorrent writes it
directly, a separate qBittorrent container must mount the same literal path (e.g.
`/srv/downloads:/srv/downloads`, not `/srv/downloads:/downloads`), and a remote
qBittorrent must expose it at the same absolute path. See `.env.example` for the
per-topology matrix.
The stock compose file publishes only on `127.0.0.1`. A default install claims
first-run setup when the first Plex server owner signs in; for extra hardening,
set `PLEX_MANAGER_SETUP_TOKEN` before starting and send it from the setup UI
(`X-Setup-Token`). Use an SSH tunnel or reverse proxy for first setup; only set
`PLEX_MANAGER_HOST_BIND=0.0.0.0` when the host is intentionally exposed.

### Optional automatic container updates

Automatic updates require two separate opt-ins: deploy the `auto-update`
Compose profile once, then enable the policy under **Settings → Automatic
updates**. Deploying the sidecar alone does not enable automatic installation.

Generate its private app-to-updater credential once and keep the source path in
`.env` so later Compose recreations use the same secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" > .plex-manager-updater-token
chmod 600 .plex-manager-updater-token
```

Then uncomment this existing line in `.env`:

```dotenv
PLEX_MANAGER_UPDATER_SECRET_SOURCE=.plex-manager-updater-token
```

Start the profile. Use the same command whenever you intentionally recreate or
restart the Compose application; Docker's `unless-stopped` policy also restarts
both containers after a normal Docker-host reboot.

```bash
docker compose --profile auto-update up -d
```

The updater is the only service that mounts the Docker socket. That socket gives
the sidecar effective control of the Docker host; it is mounted read/write
because a read-only bind flag does not limit Docker API methods. The public Plex
Manager process never receives the socket. The updater publishes no host port,
uses only the private Compose network and file-mounted bearer secret, and is
restricted to the fixed, labeled `plex-manager` container and configured image
reference. Rootless Docker and NAS installations whose socket is elsewhere can
set `PLEX_MANAGER_DOCKER_SOCKET` in `.env` before starting the profile.

The initial policy is disabled and preselects every weekday, 03:00–05:00, and
idle-only operation. Choose an explicit IANA timezone such as
`America/Toronto`; daylight-saving changes are calculated in that local zone.
Start and end must differ, and at least one starting weekday must remain
selected. A window that crosses midnight belongs to the weekday on which it
starts.

Idle means Plex Manager is not performing a critical mutation: grab handoff,
import/move/scan, correction/purge, eviction, or an administrative write. Plex
playback and qBittorrent transfers do not block an update. New requests remain
accepted during the short maintenance drain, while their critical work queues
until the lease ends. **Status → Container updater** shows the current and
available builds, channel, next window, blockers, updater availability, and the
last result. **Check now** and **Update when ready** are manual actions; the
latter bypasses the automatic schedule but still honors idle-only coordination.

`PLEX_MANAGER_IMAGE` is the only image repository/tag and release-channel
selector. The browser controls cannot switch between `:edge` and `:stable` or
target another container.

The updater retains the previous image and effective container configuration
until the candidate is healthy. A failed candidate is replaced by a clone using
the previous image/configuration, with the old image's migration entrypoint
bypassed so it does not reject the newer Alembic revision. Rollback never runs
`alembic downgrade`; releases offered for automatic installation must keep the
post-migration schema compatible with the immediately previous application
release. The mounted application data and updater state volumes persist across
replacement and sidecar restarts.

If Docker-socket authority is unacceptable, leave the profile disabled and use
the ordinary manual Compose flow instead:

```bash
docker compose pull plex-manager
docker compose up -d plex-manager
```

The manual flow does not provide app-owned scheduling, drain coordination, or
automatic rollback; pin and restore a known image tag yourself if recovery is
needed.

> **Heads-up — automatic watched-media eviction is ON by default.** To keep a
> library disk from filling, Plex Manager runs a background eviction sweep
> (default every 30 min). When a configured movie/TV/anime root crosses **90%**
> used, it **physically deletes** the library files of titles/seasons that are
> fully watched, last played more than the **30-day** grace period ago, not
> pinned, and not in flight, working down to **80%** used. Deleted items become a
> non-terminal `evicted` status and are **re-requestable** (playback disappears;
> reacquisition costs time/bandwidth). Unwatched, recently-watched, pinned, and
> in-flight content is never touched.
>
> Controls (all web, no terminal): **Settings → Eviction & logs** tunes the
> threshold/target percent, grace days, and check interval, toggles **Enable
> automatic eviction** off entirely, and toggles **Proactive eviction** — an
> opt-in mode that evicts every eligible title regardless of disk pressure.
> **Status** previews exactly what a sweep would remove per root and offers a
> manual **Free space now** sweep. A title's detail modal has a **Keep forever**
> pin that exempts it from eviction. See
> [ADR-0012](docs/adr/0012-operability-health-logs-eviction.md).

The opt-in first-party sidecar can automatically pull the configured release
channel as described above. Your config and database live in a mounted volume
and are untouched by container replacement. See
[ADR-0003](docs/adr/0003-docker-ghcr-packaging.md) and
[ADR-0004](docs/adr/0004-edge-stable-release-channels.md), with the update and
rollback boundary recorded in
[ADR-0023](docs/adr/0023-first-party-container-auto-updater.md).

## Developing

Requires Python 3.12+, Node.js 22+ with npm, and `ffprobe` (provided by the
FFmpeg package) for completed-video validation. The published Docker image
already includes it.

```bash
make install   # editable install + dev tools + pre-commit
make ui-install # install frontend dependencies
make migrate   # apply Alembic migrations (creates ./data and the schema)
make check     # backend + frontend lint, typecheck, tests, and build
make run       # http://localhost:8000  (/health to verify, /docs for the API)
make openapi   # regenerate docs/api/openapi.json from the live app
```

For short-lived local API/docs work only,
`PLEX_MANAGER_DEV_AUTH_BYPASS=true make run` grants every request an anonymous
administrator context — it bypasses browser sessions, CSRF, the setup-token, and
all role/ownership authorization, and setup completion skips the Plex-owner
identity check. Run it only bound to loopback (the default host); never combine it
with a shared or network-reachable listener. Otherwise the local server supports
the same first-run Plex sign-in flow as Docker; set `PLEX_MANAGER_SETUP_TOKEN`
only when you want the optional pre-init hardening token.

Project layout and conventions are in [CONTRIBUTING.md](CONTRIBUTING.md).

## Tech stack

Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) + Alembic · `httpx` ·
`guessit` (release parsing) · React · TanStack Query · Vite · pyright (strict) ·
ruff · pytest · Docker / GHCR.
See [ADR-0002](docs/adr/0002-python-typed-stack.md) and
[ADR-0008](docs/adr/0008-release-parser-guessit.md).

## Documentation

- [Design overview](docs/design/overview.md)
- [Historical backend-alpha plan](docs/design/alpha-plan.md)
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
