# ADR-0012: Operability — health surface, durable log store, watch-aware eviction

- **Status:** Accepted
- **Date:** 2026-07-01
- **Context builds on:** [ADR-0005](0005-zero-terminal-web-operability.md)
  (100% web-operable, no terminal), [ADR-0011](0011-tv-season-episode-support.md)
  (per-season TV lifecycle).

## Context

The beta closes the request→watchable loop (movies then TV), but a self-hosted
operator still could not, from the web UI: see whether each subsystem is healthy,
read logs to diagnose a failure, or keep the disk from filling. Today logs reach
only `docker logs`, the reconcile loop records nothing about its own runs, and
there is no eviction — all three force a terminal, violating the north star.

## Decision

1. **Health is an aggregated, cached read.** A `GET /api/v1/ops/health` composes
   per-subsystem reachability (reusing the `setup_validation` probes, TTL-cached in
   `app.state` so a polling dashboard never hammers upstreams or the TMDB rate
   limit), per-root disk usage, a DB ping, and a new in-process
   `app.state.reconcile_status` (`last_run/ok/error`) the reconcile loop now
   maintains. An unconfigured subsystem reads `not_configured`, never `down`. The
   existing unauthenticated `/health` liveness probe is unchanged.

2. **Logs are durable and built for LLM diagnosis.** A custom logging handler feeds
   both an in-memory ring buffer (live all-levels tail) and — via a queue + async
   drain task — a durable `log_events` table (INFO+, with `request_id`/`download_id`/
   `tmdb_id` correlation and a web-editable retention cap). The handler is sync and
   never touches the DB directly (no event-loop blocking / reentrancy). Beyond
   view/filter, `GET /ops/logs/export` returns a **complete correlated trail** for a
   time window or one request/download so it can be pasted into an LLM to answer
   "why did this fail" — the primary design driver. Secrets are never persisted.

3. **Eviction is watch-aware, pressure-triggered, and pinnable — never a silent
   delete.** A title/season is an eviction candidate only when it is fully watched
   (Plex `viewCount`/per-season `viewedLeafCount`), its `lastViewedAt` is older than
   a web-editable **grace period** (default 30d), it is not pinned "keep forever",
   and nothing is in flight. Deletion fires only when a root crosses a web-editable
   **disk-pressure threshold**, evicting the stalest-`lastViewedAt` candidates until
   under a target floor (an optional, default-off proactive sweep evicts past-grace
   even without pressure). Candidate selection is pure (`domain/eviction.py`). Every
   eviction is logged (to `log_events` + `DownloadHistory`), deletes only within a
   configured root (new root-guarded `FileSystemPort.delete`), and flips the item to
   a new **non-terminal `evicted`** status that is **re-requestable** — so eviction
   is honest, reversible-by-re-request, and never touches unwatched or pinned content.

4. **Everything tunable is a web-editable setting**, not a constant: the disk
   threshold/target, grace days, eviction enable + interval + proactive toggle, and
   log retention live in `KNOWN_SETTING_KEYS` (ADR-0005), each with a safe default.

## Consequences

- One Alembic migration (off `d3a7077fdbcc`) adds `log_events`, a `library_path`
  breadcrumb on `MediaRequest`/`SeasonRequest` (the final placed path, stored — not
  reconstructed — so eviction knows what to delete), a `keep_forever` pin on
  `MediaRequest`, and admits the `evicted` status (a bare `VARCHAR`, like
  `partially_available`, so no CHECK migration); every delta is mirrored in
  `models.py`.
- `LibraryPort` gains `watch_state`; `FileSystemPort` gains a root-guarded `delete`.
- Eviction runs on its own periodic task (its own interval, not the 15s reconcile
  tick). For TV it is per-season: a watched season can be evicted while unwatched
  seasons of the same show remain, and the request rollup reflects it.
- Anime separation and policy-based retention remain deferred; this ships
  pressure-based eviction with the watch/grace/pin heuristic only.
