# Operability beta — implementation blueprint

Third beta feature after TV (#22). Closes the "100% web-operable, never a terminal"
north star for **observability + self-maintenance**: a health dashboard, an
LLM-diagnosable log viewer, and watch-aware disk-pressure eviction. Stacked on
`feat/beta-tv` (branch `feat/beta-operability`); the Alembic head to chain off is
`d3a7077fdbcc` (TV).

Scope confirmed with the maintainer. Anime + policy-retention remain deferred.

---

## Component 1 — Health / status dashboard

**Goal:** one operator view answering "is every subsystem healthy, is the loop
running, how full is the disk" — without `docker logs`.

**Backend gap:** the reconcile loop records nothing about its own runs today.

- **Reconcile status (in-process):** add `app.state.reconcile_status`, a small
  mutable record `{last_run_at, last_ok_at, last_error_type, last_error_at,
  consecutive_failures}`. Set `last_run_at` at the top of `_reconcile_once`;
  on success set `last_ok_at` + clear error + reset the failure counter; in
  `_reconcile_loop`'s `except`, set `last_error_type`(=`type(exc).__name__`, never
  the message → no secret leak) + `last_error_at` + increment the counter. Mirrors
  the existing `app.state.sessionmaker`/`http_client` lifespan-state pattern.
- **Health service** (`services/health_service.py`): aggregates
  - per-upstream reachability — REUSE the `setup_validation.validate_*` probes
    (Plex `list_sections`, qBt login+info, Prowlarr `system/status`, TMDB light
    call) but **cache each result in `app.state` with a short TTL (~15s)** so a
    dashboard that polls every few seconds never hammers upstreams / burns the
    TMDB rate limit. A subsystem with no creds → `not_configured` (honest, not
    `down`).
  - disk usage per configured root — `available_bytes` + `shutil.disk_usage().total`
    → used%/free/total. Skip an unset root honestly.
  - DB ping (`SELECT 1`).
  - the reconcile status above.
- **Endpoint:** `GET /api/v1/ops/health` (authenticated; mirror `queue.py` router
  shape). Returns per-subsystem `{name, status: ok|degraded|down|not_configured,
  detail, checked_at}` + disk gauges + reconcile status. Keep the existing
  unauthenticated `GET /health` liveness probe untouched.
- **Frontend:** a `Status` route + nav entry (router.tsx + Layout.tsx NAV). Fan out
  a per-subsystem card (reuse the `HealthDot` three-state pattern), disk-usage bars
  per root, and a reconcile panel (last run / last error / N consecutive failures).
  New hook in `api/hooks.ts` + `queryKeys`, on a ~15s refetch. Regenerate the
  OpenAPI client.

## Component 2 — Log / console viewer (built for LLM diagnosis)

**Goal:** point an LLM at a complete, correlated trail and ask "why did this fail."
Today there is NO logging config — logs only reach `docker logs` (violates the
terminal-free north star).

- **Durable structured store:** new table **`log_events`** (migration off
  `d3a7077fdbcc`; mirror in `models.py`): `id`, `created_at` (indexed), `level`,
  `logger`, `message`, `context_json` (nullable — `request_id`/`download_id`/
  `tmdb_id` for correlation). Add `LogEvent` to `models.__all__`.
- **Capture pipeline:** a custom `logging.Handler` attached to the root logger in
  `create_app`/lifespan:
  - pushes EVERY record into an in-memory ring buffer (`deque(maxlen=~2000)`) for a
    live all-levels tail (sync-safe, lost on restart — acceptable for the live tail);
  - for INFO and above, enqueues onto an `asyncio.Queue`; a background drain task
    (sibling to the reconcile task in lifespan) batch-inserts into `log_events`.
    The handler is SYNC and the DB is ASYNC — never write to the DB from inside the
    handler; the queue+drain decouples them and avoids reentrancy/event-loop blocking.
  - wire `config.log_level` (currently defined but UNUSED) to actually set the root
    logger level at startup.
- **Retention:** the drain task periodically prunes `log_events` older than
  `log_retention_days` (web-editable, default 7). A max-row cap for bounded
  high-volume growth still needs acceptance criteria (issue #152).
- **Endpoints** (authenticated `ops` router):
  - `GET /api/v1/ops/logs` — paginated, filter by `level`/`since`/`logger`/
    correlation id, from `log_events`.
  - `GET /api/v1/ops/logs/tail` — the live ring buffer (all levels).
  - `GET /api/v1/ops/logs/export` — **the LLM affordance**: a text or JSON bundle
    for a time window OR one `request_id`/`download_id`'s FULL trail, downloadable /
    copyable so it can be pasted into an LLM.
- **Correlation:** at key decision points (reconcile failure, adapter outage, import
  block, grab failure) log at WARNING/ERROR WITH context (download_id/tmdb_id) so
  the export assembles a coherent story. (Honesty over silence already argues for
  these logs; this just makes them structured + queryable.)
- **Frontend:** a `Logs` route + nav entry: level filter, search, a live-tail toggle,
  and a prominent **"Copy / download for diagnosis"** button (hits `export`).

## Component 3 — Disk-pressure eviction (watch-aware, pinnable)

**Goal:** automatically maintain the library — evict content the user has watched
and won't rewatch, when the disk is under pressure, never touching pinned or
unwatched content. Everything logged + re-requestable.

**Policy (confirmed):** pressure-triggered, `lastViewedAt`-ordered, grace floor.

- **New port method** `FileSystemPort.delete(path)` + `LocalFileSystem` impl:
  delete a file/dir tree, but ONLY within a configured library root (reuse the
  existing root-escape guard from `local.py`); a path outside any root is refused
  (honesty; never delete arbitrary paths). Missing path = no-op, not an error.
- **Library breadcrumb:** add `library_path: str | None` to **`MediaRequest`**
  (movie) and **`SeasonRequest`** (tv season), set at import/availability time to
  the final library destination the importer placed content into. Store it (do NOT
  reconstruct from naming at eviction time — fragile). Migration + models.
- **Keep-forever pin:** add `keep_forever: bool` (default false) to `MediaRequest`.
  Pinned title = NEVER evicted (show or movie granularity). Toggle endpoint
  `POST /api/v1/requests/{id}/keep-forever` + a control in the title detail modal.
- **Watch state:** new library adapter method
  `watch_state(tmdb_id, media_type, season=None) -> {watched: bool, last_viewed_at}`
  via Plex `viewCount`/`lastViewedAt`/per-season `viewedLeafCount` vs `leafCount`.
  Movie watched = `viewCount>0`; season watched = all episodes viewed
  (`viewedLeafCount == leafCount`); `last_viewed_at` = the item's/season's
  `lastViewedAt`. (New port method on `LibraryPort` + PlexLibrary impl + FakeLibrary.)
- **Eviction candidate selection (pure domain):** `domain/eviction.py` —
  `select_evictions(candidates, used_pct, threshold_pct, target_pct, grace_cutoff)`:
  eligible = status in {available, partially_available} AND watched AND
  `last_viewed_at < grace_cutoff` AND NOT keep_forever AND NOT in-flight; order by
  `last_viewed_at` ascending (stalest first); pick until projected used% ≤ target.
  Pure + unit-tested (no IO).
- **Eviction service** (`services/eviction_service.py`) + a **sibling periodic task**
  in lifespan (its OWN interval, NOT the 15s reconcile tick — default every 30 min,
  web-editable). Each pass: if `eviction_enabled` (default **true**) and a root's
  used% ≥ `disk_pressure_threshold_percent` (default 90): resolve watch-state for
  candidates, run `select_evictions`, and for each: `fs.delete(library_path)`, flip
  the title/season status to `evicted`, log to `log_events` + `DownloadHistory`.
  TV is per-season (evict a watched season, keep unwatched ones → rollup reflects).
  Optional `eviction_proactive_enabled` (default **false**): also evict past-grace
  watched+un-pinned even without pressure.
- **New status `RequestStatus.evicted`** — a bare VARCHAR value (like
  `partially_available`, `native_enum=False`, no CHECK migration), NON-terminal so
  the title is **re-requestable** (a re-request re-grabs it); excluded from
  active-dedup as appropriate (review the partial-index predicates). Surfaced
  honestly in the UI with a "request again" affordance.
- **Endpoints:** `GET /api/v1/ops/disk` (usage per root + a preview of the current
  eviction candidates, ranked), and `POST /api/v1/ops/evict` — a manual operator
  trigger of a pressure sweep (the north-star #1 button: free space on demand).
- **Frontend:** disk usage on the Status page (candidate preview + a "Free space"
  button → `evict`); the keep-forever toggle on the title modal; the `evicted`
  status rendered honestly with re-request.

## Settings (web-editable — `KNOWN_SETTING_KEYS` + Settings UI, NOT config.py)

`disk_pressure_threshold_percent` (90), `disk_pressure_target_percent` (80),
`eviction_grace_days` (30), `eviction_enabled` (true), `eviction_proactive_enabled`
(false), `eviction_interval_minutes` (30), `log_retention_days` (7). Each read via
the existing `SettingsStore`/`get_*_optional` pattern with a safe default.

## One Alembic migration (off `d3a7077fdbcc`), mirrored in `models.py`

`log_events` table; `media_requests.library_path`, `media_requests.keep_forever`;
`season_requests.library_path`; any index-predicate updates to admit the `evicted`
status. Generated via `alembic revision --autogenerate`; models updated FIRST so
autogenerate sees them (tests build schema via `Base.metadata.create_all`).

## Build layers (dependency-ordered — the workflow's phases)

1. **Docs** — this blueprint + ADR-0012 (operability decisions), committed.
2. **Domain** — `domain/eviction.py` (pure candidate selection) + tests; any pure
   health/log value objects. (No adapter/IO imports.)
3. **Schema/migration** — models deltas + the Alembic migration + schema tests.
4. **Ports/repos** — `FileSystemPort.delete`, `LibraryPort.watch_state`; a
   `LogEvent` repository; `library_path`/`keep_forever` on the request/season repos.
5. **Adapters** — `LocalFileSystem.delete` (root-guarded), `PlexLibrary.watch_state`
   (+ FakeLibrary), + adapter tests.
6. **Services** — `health_service`, log capture handler + drain + retention,
   `eviction_service`; wire `app.state.reconcile_status`; the lifespan sibling tasks.
7. **Web** — `ops` routers (health, logs, disk, evict), keep-forever endpoint,
   logging-handler wiring in `create_app`, register routers before `mount_spa`.
8. **OpenAPI** — regenerate `openapi.json` + typed client.
9. **Frontend** — Status page, Logs page (+ export/copy), keep-forever toggle,
   `evicted`/disk UI, hooks + queryKeys + nav.
10. **Verify** — `make check` + `make ui-check` fully green.

## Invariants the build must hold

- Domain purity: `domain/eviction.py` imports no adapter/IO/ORM.
- Honesty: eviction NEVER silently deletes — every eviction is logged + flips to a
  visible `evicted` state + is re-requestable; `keep_forever` and unwatched content
  are never touched; the log store never records a secret.
- Movie/TV behaviour from #22 stays intact; eviction is per-season for TV.
- The log capture must never block the event loop or recurse (sync handler → queue →
  async drain), and must be resilient (a DB write failure in the drain must not kill
  logging or the app).
- Operator policy knobs are web-editable settings. Internal safety/backpressure
  constants for the log pipeline (ring size, queue size, drain/prune cadence) and
  the beta telemetry retention floor remain code constants unless a later issue
  defines an operator-facing contract for them.
