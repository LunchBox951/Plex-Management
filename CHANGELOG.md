# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Capture-only section entitlements (PR-3 of the auth-revalidation design):
  the share sweep and a detached post-sign-in step now record each shared
  user's Plex section entitlements (no enforcement yet — that is PR-5), with
  capture counters, a `capture_unavailable` / `capture_anchor_blocked` signal
  on `GET /api/v1/ops/health`, and a missing-anchor backfill when Plex
  settings are re-saved. Ships two migrations
  (`c8198009583d`, `c5cf0a125f5f`) (#484, #560).
- Admin-readable "Automatic sign-outs" audit surface
  (`GET /api/v1/auth/sign-outs` + a Settings section) answering "why was this
  account signed out?" from the durable audit record, and honest SSE close
  reasons: the realtime stream's final frame now names why it closed
  (share revoked vs sign-in expired vs idle vs absolute expiry, and more),
  with truthful sign-in-screen wording (#556, #567).
- Entitlement/share-state schema and a `plex_access_service` module extracting
  the plex.tv share-verdict ladder (schema and ladder extraction only — no
  loop, no enforcement yet); PR-1 of the auth-revalidation design (#555).
- Periodic Plex share-revalidation sweep: signed-in users are re-checked
  against plex.tv on a bounded interval (default 6h, admin-editable), and a
  confirmed share loss now signs the user out and closes their realtime
  stream instead of waiting out session expiry (7-30 days). Owner/admin
  accounts are exempt from sweep-driven revocation, and the sweep withholds
  revoked-share sign-outs during a server-identity anchor mismatch
  (stale-token verdicts still sign out — the credential is dead regardless
  of which server is anchored). Status surfaces on `GET /api/v1/ops/health`
  and the Status page as `share_sweep` (#557, PR-2 of the auth-revalidation
  design).

### Changed
- CI: parallelized the Python quality-gate test run, and eliminated pytest
  warnings under Python 3.12 and 3.14 (#543, #544).
- Repo docs: archived the completed v1 planning docs under `docs/archive/`,
  removed the now-unused `init.sh` reference-clone bootstrap script,
  refreshed the post-1.0 status banners across README, CLAUDE.md, and
  AGENTS.md, and annotated CONTRIBUTING.md's release checklist (the
  changelog-cut step may defer to promotion-day cleanup) (#542, #551, #553).
- `python-deps` group bump: fastapi, uvicorn, alembic, guessit, ruff, mako,
  cffi, starlette, websockets (#550).
- `frontend-deps` dev-dependency group bumps: `@types/react`,
  `@types/react-dom`, `@vitejs/plugin-react`, `jsdom`, `vite` (#545), then
  `@testing-library/user-event`, `globals`, `typescript-eslint`, `vite`
  (#561).

### Fixed
- Eviction: operator corrections and pressure sweeps are now serialized by a
  per-root pressure-exclusion lease — a correction beginning inside a sweep's
  await windows defeats the sweep (corrections never wait), enforced down to
  the delete boundary before the durable marker arms; denied or defeated
  manual sweeps are visible in `POST /ops/evict` and on the Status page
  (#526, #568).
- Updater: non-2xx coordinator responses now log the request path, status,
  and either the exact app error code (allowlisted) or an opaque-body
  summary (allowlisted media type, byte length, fingerprint) instead of a
  bare `coordinator_unavailable` — the next #539 recurrence is attributable
  from the sidecar log alone, and nothing arbitrary can reach the log (#566).
- Eviction: walk-skipped candidates now carry an explicit `None` size
  sentinel instead of a fabricated `0.0`, and eviction/retention sweeps now
  log their duration on completion — including the common below-pressure
  tick that previously returned in total silence — with walked-vs-skipped
  counts included once candidate assembly has occurred (#554).
- Grab now refuses, rather than transparently restarts, an unsafe
  attachment-loss recovery (a blocklisted release, a settled request or
  season, or a torrent removal that is in-flight or already completed) —
  the prior client re-add machinery could strand or delete torrents, across
  four review rounds. Ownership is re-proven at the client and is never
  carried across a removal (#532, #472).
- Share-revalidation sweep: the token guard is now atomic with the
  revocation itself (a correlated `EXISTS` over the stored ciphertext
  instead of a separate compare-then-update), and an unmapped `check_share`
  exception now persists a failed-attempt stamp so a crashing cohort can no
  longer starve the rest of the sweep's backlog (#559).

### Security
- Bumped `cryptography` to 50.0.0, resolving GHSA-g6cj-pr64-35w5 (a
  Bleichenbacher timing oracle in PKCS#7 decryption affecting
  `cryptography` `>=44,<50`). Only Fernet is used in this codebase
  (`adapters/encryption.py`), so this is a supply-chain hygiene bump rather
  than an exploitable path here. **Not
  included in the released `1.0.0` image** — the promoted `:stable` build is
  bit-identical to the canary-proven `edge-c1bf4eb` image, which predates
  this bump (#548).
- Bumped the transitive `js-yaml` dev dependency to 4.3.1, resolving a
  quadratic-CPU DoS in `!!omap` resolution (GHSA-5p4m-2wfm-xmqj) (#547).

## [1.0.0] - 2026-08-09

_Package metadata is `1.0.0` (see `src/plex_manager/__init__.py`). The
canary-proven `edge-c1bf4eb` image was promoted to `:stable` / `1.0.0` on
Aug 9, 2026 by re-tag (no rebuild — ADR-0004). The 1.0.0 runbook (issue #3)
deliberately departed from CONTRIBUTING.md's checklist ordering here: the
`## [1.0.0]` changelog cut lands in promotion-day cleanup after the re-tag,
so the promoted bytes are exactly the renewed-soak canary build at the cost
of the cut not being baked into that image. The request → watchable →
correct loop for movies, TV, and anime is feature-complete; the 7-day live
canary run (Jul 25 - Aug 1, 2026) completed, fixes from it landed in
`:edge` on Aug 2, and the renewed soak of the promoted build ran clean from
Aug 3 (see the "Version 1.0" milestone)._

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
- 28 Alembic migrations have shipped since the alpha's initial schema; every
  container start runs `alembic upgrade head` before serving. Rollback and
  backup expectations are now documented honestly rather than implied — see
  [ADR-0023](docs/adr/0023-database-rollback-and-pre-migration-backup.md) and
  the README "Backup & recovery" section.
- Runtime container base migrated from Debian `python:3.14-slim` to a
  digest-pinned Wolfi/glibc base, shrinking the base-OS vulnerability surface
  while preserving the Python 3.14, glibc-wheel, `ffprobe`, and numeric-UID
  production contract ([ADR-0027](docs/adr/0027-wolfi-container-base.md),
  #18).

### Fixed
- A broad honesty/resilience pass: no unhandled 500s on parse, settings
  writes, or startup; release matching & ranking fidelity; import robustness
  and a full-coverage requirement before a season is claimed available;
  request-row dedup healing (folds duplicates, self-heals false "available"
  claims); qBittorrent session reuse across polling cycles with stall healing;
  host/container path-visibility healing for library and download roots.
- Canary-soak eviction/correction-order hardening: recovery now finishes
  marker-owned movie correction purges left incomplete across replacement
  statuses, defers recovery while a correction purge is still active, and
  recovers advanced marker-owned purges instead of stranding them; the
  incomplete-delete outcome is persisted on the eviction claim row itself so
  a later sweep can't re-derive stale state (#540, #524, #519, #525, #495).
- Purge probe-lifecycle races: correction-path probes are isolated from
  eviction's own probes, a probe's own deadline cancellation is distinguished
  from an external cancel, and `remove_torrent`'s local mount-sensitive
  reads run on the same abandonable probe substrate so a wedged or
  unresponsive mount can't strand a purge past the filesystem-probe
  deadline (qBittorrent API calls keep their own adapter timeout)
  (#518, #522, #493).
- Filesystem publish-lock and containment: stale publish locks are reclaimed
  on rollback and the remaining reclaim races closed, xattrs survive the
  cross-device copy fallback, and import publication is anchored to
  no-follow descriptors inside the configured library root instead of
  trusting a path string (#521, #541, #500, #499).
- Import/availability/grab race closures: the import finalize path locks its
  parent before scope bookkeeping and re-reads scopes before terminal
  finalize so a late same-hash attach still imports; availability promotion
  binds its CAS to the actually-observed completion and guards against a
  correction re-arm mid-promotion; `mark_available`'s CAS boolean return is
  honored instead of assumed; a same-hash torrent removal verifies scope
  ownership before its lost-CAS cleanup; and admin cancel now serializes
  with the per-media lock (#498, #492, #523, #489, #508, #491, #367).
- Health/updater/logs operability: shared health-probe lifecycle gaps are
  closed, promotion log extras are sanitized and cite the actual probe
  bound, the import-cycle download ID is normalized in logs, and partial
  eviction outcomes are surfaced instead of collapsing to a single
  pass/fail (#473, #509, #520, #517, #527).

### Security
- Header-safe credential handling, atomic and symlink-safe encryption-key
  publication, and log-forgery hardening.
- SSRF hardening on configured service requests.
- XFF-aware sign-in throttling.
- An ownership-claim guard before deleting library files on eviction/correction.

### Deferred
- The updater sidecar's self-recreation (ADR-0025 Stage 1, tracked by #390).
  The first-party container updater itself *is* bundled as an opt-in Compose
  profile (`--profile auto-update`) with Stage 0 detect-and-surface behavior;
  only its ability to recreate itself after an update remains deferred.
