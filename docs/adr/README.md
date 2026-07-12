# Architecture Decision Records

Each ADR captures **one** significant, hard-to-reverse decision: its context, the
choice, the consequences, and the alternatives we rejected. ADRs are immutable
once accepted — to change a decision, add a new ADR that supersedes the old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-integrated-app-borrowed-brains.md) | Integrated app with *borrowed brains* (Option C) | Accepted |
| [0002](0002-python-typed-stack.md) | Strictly-typed Python stack | Accepted |
| [0003](0003-docker-ghcr-packaging.md) | Ship as a Docker image via GHCR | Accepted |
| [0004](0004-edge-stable-release-channels.md) | `:edge` / `:stable` channels by tag promotion | Accepted |
| [0005](0005-zero-terminal-web-operability.md) | Zero-terminal, web-operable release deployment | Accepted |
| [0006](0006-download-client-port-qbittorrent.md) | `DownloadClient` port; qBittorrent as v1 adapter | Accepted |
| [0007](0007-sqlite-alembic-migrations.md) | SQLite + Alembic migrations from day one | Accepted |
| [0008](0008-release-parser-guessit.md) | `guessit` parses; the quality model stays ours | Accepted |
| [0009](0009-frontend-typed-spa.md) | Frontend is a typed SPA (Vite + React + TS), contract-bound | Accepted |
| [0010](0010-import-pipeline-honest-availability.md) | Import pipeline + honest two-phase availability contract | Accepted |
| [0011](0011-tv-season-episode-support.md) | TV support — per-season lifecycle with a computed rollup | Accepted |
| [0012](0012-operability-health-logs-eviction.md) | Operability — health surface, log store, watch-aware eviction | Accepted |
| [0013](0013-auto-grab-worker.md) | Auto-grab worker — background request→search→grab spine | Accepted |
| [0014](0014-correction-verbs.md) | Correction verbs — report-issue (blocklist + purge + re-search) and cancel | Accepted |
| [0015](0015-anime-library-routing.md) | Anime library routing (optional anime roots, routing only) | Accepted |
| [0016](0016-plex-oauth-owner-sessions.md) | Plex-first setup + browser-side Plex sign-in (single verify endpoint, session cookie) | Accepted |
| [0017](0017-plex-video-download-validation.md) | Positive Plex-video validation before library import | Accepted |
| [0018](0018-origin-confined-service-urls.md) | Origin-confined service URLs and explicit changed-destination credential consent | Accepted |
| [0019](0019-realtime-sse-invalidations-over-polling-floor.md) | Realtime admin SSE invalidations over a permanent polling floor | Accepted |
| [0020](0020-episode-level-fallback-whole-season.md) | Episode-level fallback for whole-season TV requests | Accepted |

ADRs 0001–0007 were accepted on **2026-06-29** during the v2 brainstorming
session; 0008 during the first backend-alpha session; 0009 during the
first frontend-alpha session; 0010–0020 across the subsequent beta feature
sessions (each ADR's own header carries its date). Full context:
[`docs/design/overview.md`](../design/overview.md).
