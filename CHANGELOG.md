# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No released versions yet — no Git tags or GitHub releases exist, and package
metadata is still `0.0.0`. The request → watchable → correct loop for movies,
TV, and anime is feature-complete; a milestone canary/feature-freeze run
precedes the first stable promotion (see the "Version 1.0" milestone)._

### Added
- Typed React/Vite single-page app, contract-bound to the published OpenAPI
  document ([ADR-0009](docs/adr/0009-frontend-typed-spa.md)).
- Import pipeline with an honest two-phase availability contract, and positive
  Plex-video validation before a downloaded file enters a library
  ([ADR-0010](docs/adr/0010-import-pipeline-honest-availability.md),
  [ADR-0017](docs/adr/0017-plex-video-download-validation.md)).
- TV support: per-season lifecycle with a computed rollup, and episode-level
  fallback so a whole-season request can still complete when no acceptable
  season pack exists ([ADR-0011](docs/adr/0011-tv-season-episode-support.md),
  [ADR-0020](docs/adr/0020-episode-level-fallback-whole-season.md)).
- Optional anime library routing — anime imports route to dedicated roots when
  configured ([ADR-0015](docs/adr/0015-anime-library-routing.md)).
- Auto-grab worker: requests move unattended through search → decision → grab
  ([ADR-0013](docs/adr/0013-auto-grab-worker.md)).
- Correction verbs with no terminal required: report-issue (blocklist + purge +
  re-search), cancel, re-acquire, and relocate
  ([ADR-0014](docs/adr/0014-correction-verbs.md)).
- Operability surface: health/status dashboard, an LLM-diagnosable log store
  with export, and watch-aware retention/eviction
  ([ADR-0012](docs/adr/0012-operability-health-logs-eviction.md)).
- Authenticated realtime SSE invalidations layered over a permanent polling
  floor, so a dropped connection never desyncs the UI
  ([ADR-0019](docs/adr/0019-realtime-sse-invalidations-over-polling-floor.md)).
- Discover/Search library-state badges with one-click request from a tile.
- qBittorrent saves land directly under the mounted downloads root, with a
  host/container path-visibility probe and a relocate verb for drift.
- `plex_manager.db_backup`: an advisory, WAL-consistent snapshot of the SQLite
  database and the Fernet encryption key as one recovery unit, taken before a
  pending migration is applied at container start, pruned to the most recent 5
  ([ADR-0023](docs/adr/0023-database-rollback-and-pre-migration-backup.md)).
- Documented single-source app/package version (`plex_manager.__version__`,
  read by hatch, surfaced as OpenAPI `info.version`) and a release checklist in
  `CONTRIBUTING.md` so the app version and a promoted image tag cannot silently
  disagree.

### Changed
- Auth model: browser-side Plex owner sign-in with session cookies + CSRF is
  now the primary path ([ADR-0016](docs/adr/0016-plex-oauth-owner-sessions.md));
  `X-Api-Key` remains as an optional recovery/automation credential alongside
  it, not the primary auth mechanism it was in the alpha.
- Configured service URLs (Plex/Prowlarr/qBittorrent/TMDB) are origin-confined,
  and changing a service's destination requires explicit operator consent
  ([ADR-0018](docs/adr/0018-origin-confined-service-urls.md)).
- 17 Alembic migrations have shipped since the alpha's initial schema; every
  container start runs `alembic upgrade head` before serving. Rollback and
  backup expectations are now documented honestly rather than implied — see
  [ADR-0023](docs/adr/0023-database-rollback-and-pre-migration-backup.md) and
  the README "Backup & recovery" section.

### Fixed
- A broad honesty/resilience pass: no unhandled 500s on parse, settings
  writes, or startup; release matching & ranking fidelity; import robustness
  and a full-coverage requirement before a season is claimed available;
  request-row dedup healing (folds duplicates, self-heals false "available"
  claims); qBittorrent session reuse across polling cycles with stall healing;
  host/container path-visibility healing for library and download roots.

### Security
- Header-safe credential handling, atomic and symlink-safe encryption-key
  publication, and log-forgery hardening.
- SSRF hardening on configured service requests.
- XFF-aware sign-in throttling.
- An ownership-claim guard before deleting library files on eviction/correction.

### Deferred
- The host auto-updater mechanism (Watchtower vs. a systemd timer) is not
  bundled in the compose file yet — each host is designed to auto-pull its
  release channel, but wiring the updater itself remains an open decision.
