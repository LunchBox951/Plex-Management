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

### TLS-terminating reverse proxies

For direct HTTP/LAN access, leave `PLEX_MANAGER_AUTH_COOKIE_SECURE` unset so
cookies follow the direct request scheme. If a reverse proxy terminates TLS and
forwards HTTP to Plex Manager, set this before startup:

```dotenv
PLEX_MANAGER_AUTH_COOKIE_SECURE=true
```

This explicit setting is mandatory for that topology. Plex Manager itself never
reads `X-Forwarded-Proto`: when the variable is unset, the Secure flag follows
whatever request scheme the ASGI server reports, and uvicorn's own
forwarded-header handling trusts loopback peers only by default — so scheme
inference behind a TLS terminator is topology-dependent. Choose the explicit
value for your trusted proxy/browser topology instead of relying on it
(`PLEX_MANAGER_AUTH_COOKIE_SECURE=false` likewise forces non-Secure cookies for
an unusual direct-https setup). Preserve the original `Host` and configure
`PLEX_MANAGER_ALLOWED_HOSTS` for a public proxy hostname as described in
`.env.example`.

### Optional automatic container updates

Automatic updates require two separate opt-ins: deploy the `auto-update`
Compose profile once, then enable the policy under **Settings → Automatic
updates**. Deploying the sidecar alone does not enable automatic installation.

Generate its private app-to-updater credential once and keep the source path in
`.env` so later Compose recreations use the same secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))" | sudo install -o 10001 -g 0 -m 440 /dev/stdin .plex-manager-updater-token
```

Compose file secrets preserve the host file's ownership and mode: root-owned `600`
would leave the app (uid 10001) and cap-dropped root updater unable to read it.

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

The executor negotiates Docker Engine API 1.41–1.47. Dynamic host ports
(`HostPort: 0` and publish-all) are materialized before replacement so they do
not silently change. Per-network MAC preservation on a multi-network container
requires Engine API 1.44 or newer; an older daemon fails the update before
stopping Plex Manager rather than recreating it inaccurately. A configured
Compose `stop_grace_period` is honored up to 300 seconds. Negative, indefinite,
or longer values are rejected before cutover so a sidecar request cannot hang
without a bounded recovery window.

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

If the candidate's migration is partially applied or the advertised N/N-1
compatibility guarantee is wrong, the rollback clone may also fail its health
gate. In that case the updater leaves its durable state and retained previous
container intact; it does not report a successful rollback or delete recovery
evidence. Restore the pre-migration recovery unit using **Backup & recovery**
below, then restart the pinned older image.

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
channel as described above. Your config and database live in a **mounted
volume, which persists them across restarts and container replacement — but the
volume is not a backup.** Every ordinary container start runs the pre-migration
backup hook and then `alembic upgrade head` before serving traffic. See
[ADR-0003](docs/adr/0003-docker-ghcr-packaging.md) and
[ADR-0004](docs/adr/0004-edge-stable-release-channels.md), with the general
database recovery policy in
[ADR-0023](docs/adr/0023-database-rollback-and-pre-migration-backup.md) and the
automatic-update boundary in
[ADR-0024](docs/adr/0024-first-party-container-auto-updater.md).

**Rollback:** if no migration ran between the two versions (same schema),
rollback is simply re-pointing the older image tag. If a migration *did* run,
an older image generally **cannot start** against a database already stamped
with a newer revision it doesn't know — rolling back across a migration means
restoring the pre-migration backup (below), then running the older tag. See
[ADR-0023](docs/adr/0023-database-rollback-and-pre-migration-backup.md) for the
full policy and why Alembic's `downgrade` scripts are not treated as a general,
non-destructive rollback path.

The opt-in automatic-update train is a deliberately narrower exception recorded
by ADR-0024: only moving-tag releases certified to keep the post-migration
schema usable by N-1 are eligible. For those releases, the sidecar can restore
the retained N-1 image/configuration without running Alembic downgrade or
restoring database bytes. The pre-migration backup is still created and remains
the manual recovery unit if a migration is only partially applied, the
compatibility assertion is wrong, or the rollback clone cannot become healthy.

### Backup & recovery

The database and the Fernet encryption key at `<data_dir>/secret.key` (or the
`PLEX_MANAGER_FERNET_KEY` env override, for k8s-style deployments) are **one
recovery unit**. A replacement key cannot decrypt Plex tokens, service
credentials, the recovery API key, or magnet links already stored in the
database — losing the key without a copy of it makes an otherwise-intact
database backup useless for recovering those secrets.

On the **SQLite** deployment, every container start that finds a pending
migration automatically snapshots this unit *before* applying it, into
`<data_dir>/backups/pre-migrate-<from-rev>-<timestamp>/` (the database file, the
key file when one exists, and a `MANIFEST.txt` restore runbook), pruned to the
most recent 5. This is advisory and best-effort — fail-loud but never fatal to
startup — not a replacement for your own backup strategy. Know its limits:

- **`PLEX_MANAGER_FERNET_KEY` deployments:** the automatic backup is
  **database-only**. The active key lives in your environment/secret store, so
  no key file is written into the backup (a stale on-disk `secret.key`, if one
  is left over, is deliberately *not* copied — it is not the active key). You
  must preserve the `PLEX_MANAGER_FERNET_KEY` value yourself; it is the key
  half of the recovery unit, and the `MANIFEST.txt` records this.
- **PostgreSQL deployments:** **no automatic backup is taken at all.** Take
  your own `pg_dump` (below) *and* save the key before upgrading — do not rely
  on a `pre-migrate-*` directory existing.

**SQLite (the default, named-volume) deployment:**
```bash
# Stop the container, then snapshot the database via the SQLite backup API --
# a bare `cp` of plex_manager.db can miss committed rows still sitting in the
# -wal sidecar (e.g. after a crash/kill; this is why the automatic backup
# uses the same API):
sqlite3 /path/to/volume/plex_manager.db ".backup './backup/plex_manager.db'"
# Key-file (default) deployments: the key is the other half of the recovery
# unit; keep it with the snapshot (mode 0600 must be preserved on restore):
cp /path/to/volume/secret.key ./backup/
chmod 600 ./backup/secret.key
```

The key half follows the same disposition rules as the PostgreSQL section
below: on `PLEX_MANAGER_FERNET_KEY` deployments, skip the `secret.key` copy
and preserve the env value instead — any leftover `secret.key` in the volume
is stale and must **not** be saved as the key half.

**Restoring** a SQLite backup: after copying `plex_manager.db` (and `secret.key`,
per the key-half rules above) back into the volume, remove any stale
`plex_manager.db-wal` / `plex_manager.db-shm` sidecar files left next to it
*before* starting the older image. The backup is a standalone snapshot;
leftover WAL frames belong to whatever database was running most recently
(likely a newer one) and SQLite replays them on open, which can silently
reintroduce the very data/schema you are trying to roll back away from.
If you copied the files from the host as root (typical for named-volume
restores), also restore ownership — the image runs the app as the non-root
`appuser` (UID 10001), which must be able to *write* the database and *read*
the key: `chown 10001:10001 plex_manager.db secret.key` (keep the key at mode
0600).

**PostgreSQL deployment:** `pg_dump` backs up the database only — the Fernet
key is never in Postgres. Where the key half lives depends on your deployment:

- **Key file (the default):** the active key is `secret.key` in the app
  container's data directory — copy it alongside the dump and keep the two
  together:
  ```bash
  pg_dump -Fc plexmanager > plexmanager.dump
  cp /path/to/data_dir/secret.key ./backup/secret.key
  chmod 600 ./backup/secret.key
  ```
- **`PLEX_MANAGER_FERNET_KEY` deployments:** the active key is the environment
  value, and any on-disk `secret.key` is absent or stale — it is **not** the
  active key, so do not save it as if it were. Preserve the
  `PLEX_MANAGER_FERNET_KEY` value from your environment/secret store together
  with the dump.

**Restore verification:** after restoring, start the container, sign in, and
confirm a configured service (Plex/Prowlarr/qBittorrent/TMDB) still shows its
stored credential correctly — if it doesn't decrypt, the key and database are
out of sync.

## Developing

Requires Python 3.12+, Node.js **22.13+ on the 22 line, or Node.js 24+** (Node
24 LTS recommended) with npm, and `ffprobe` (provided by the FFmpeg package)
for completed-video validation. The published Docker image already includes
it. The floor is set by the locked frontend toolchain (ESLint 10.6, jsdom
29.1, the Vite 8.1 React plugin) — plain Node 22.0–22.12 will fail to install
or run it; see `frontend/package.json`'s `engines` field, which enforces this
range.

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
