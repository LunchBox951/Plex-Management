# ADR-0013: Auto-grab worker — the background request→search→grab spine

- **Status:** Accepted
- **Date:** 2026-07-01
- **Context builds on:** [ADR-0001](0001-integrated-app-borrowed-brains.md)
  (borrowed decision brain + quality model), [ADR-0011](0011-tv-season-episode-support.md)
  (per-season TV lifecycle), [ADR-0012](0012-operability-health-logs-eviction.md)
  (health surface + background-loop health signals).

## Context

Creating a request persisted a `pending` `MediaRequest` (movie) or a set of
`pending` `SeasonRequest` rows (TV) and then stopped: `grab_service.grab` was
reached only from the manual "Grab" button (`web/routers/queue.py`) and
`decision_service.preview` only from that button and the search-preview endpoint.
The three background loops (reconcile, log-drain, eviction) never searched or
grabbed. So `pending` and `searching` (the state a failed download re-arms to)
were dead-end states with no consumer — the app genuinely "did nothing" on a
request until an operator manually opened the title and grabbed. This is the core
request→watchable loop, and it was absent (issue #27).

The prototype grabbed synchronously inside the HTTP request (slow, stranded on
restart) plus a nightly cron that gave up after 5 nights and collapsed "searched
OK, nothing acceptable", "couldn't search", and "download failed" all into
`failed` — the exact dishonesty north-star #3 forbids. Radarr does it well but
with machinery built for multi-tenant scale (RSS-feed matching to avoid per-title
searches across hundreds of titles × many indexers, per-indexer escalation, a full
scheduled-command bus) that a single-machine, single-Prowlarr beta does not need.

## Decision

1. **A 4th sibling background loop, reusing the existing brains.** `_autograb_loop`
   in `web/app.py` is a sibling to the reconcile/log-drain/eviction loops (own
   interval, own session per tick, one bad cycle never kills the loop). Each tick
   calls `auto_grab_service.run_grab_cycle`, which reuses `decision_service.preview`
   (indexer search → pure decision engine) then `grab_service.grab` **verbatim** —
   the SAME code path the manual Grab button uses, so manual and automatic grabs
   can never diverge. The worker writes **no new decision logic**; it resolves the
   media descriptor itself (never importing a `web/` router into the services
   layer, preserving hexagonal purity).

2. **Direct per-request search, not RSS sync.** At single-user scale a handful of
   pending scopes is cheap to search directly, and an on-demand request wants an
   immediate targeted search — not "wait for it to surface in a recent-releases
   feed". RSS-feed matching exists to avoid per-title searches at library scale we
   don't have, so it is deliberately **not** built for the beta. The base tick is
   ~60s; at most **5 actual Prowlarr searches per cycle** (a module constant),
   processed sequentially, protect the single Prowlarr. A per-scope backoff ladder
   keeps the tick cheap even as pending scopes accumulate.

3. **Per-scope escalating backoff (borrowed from Radarr, coarsened).** Two columns
   are added to **both** `media_requests` and `season_requests` (a TV grab is
   per-season): `search_attempts INT NOT NULL DEFAULT 0` and a nullable
   `next_search_at`. The worker scans requests/seasons in
   `{pending, no_acceptable_release, searching}` whose `next_search_at <= now`
   (`NULL` = due now, so a freshly created request is searched on the next tick —
   Radarr's "search on add" / Overseerr's `searchNow`). A search that finds nothing
   acceptable bumps `search_attempts` and schedules the next search on the ladder
   **`[10m, 30m, 1h, 3h, 6h, 12h, 24h]`, then 24h forever**.

4. **Never give up.** A nothing-acceptable search parks the scope at the honest,
   retryable `no_acceptable_release` state (the SAME `mark_no_acceptable_release`
   the manual path uses, keeping the never-un-terminate guard) and re-tries
   indefinitely — a new release may appear at any time. There is no
   dead-end-after-N. The manual "choose a specific release / grab anyway" button
   remains the operator override (north-star #1: a button, never a terminal).

5. **Honesty: park vs error are strictly distinct.** "searched OK, nothing
   acceptable" (→ park + backoff) is kept separate from "the search RAISED"
   (Prowlarr down / rate-limited — the `IndexerPort` contract raises rather than
   returning `[]`). A raise leaves the scope's state completely untouched (never
   falsely parked), aborts the rest of the cycle (so a down Prowlarr is not
   hammered with every due scope — this abort IS the short global cycle backoff),
   and is recorded on a new in-process `AutograbStatus` health record (mirroring
   `ReconcileStatus` from ADR-0012, surfaced in `GET /ops/health`) — so the
   operator sees *why* nothing is being grabbed, not requests silently stuck at
   `pending`.

6. **A master on/off switch.** A web-editable `auto_grab_enabled` setting (default
   ON) is re-read every tick and surfaced in the Settings UI. Turning the worker
   off is a settings toggle, never a terminal; the manual Grab button still works
   when it is off.

7. **Safe interaction with the other loops & concurrency.** The worker runs on its
   own session/interval and touches only request/season/download-grab state, never
   the reconciler's `importing` CAS. It skips any scope that already has an active
   download (`find_active_for_request`) **before** paying for a search, so it never
   races `grab_service`'s one-active guard (itself backstopped by the
   `uq_downloads_active_request` partial index). A grab refused for an expected
   edge/concurrency reason (a manual grab won the slot, no grab source, …) is
   logged and left for the next cycle, never a cycle crash. A successful grab never
   pushes `next_search_at` into the future, so a later failure-rearm to `searching`
   is immediately due rather than stuck behind a stale backoff.

## Consequences

- The request→watchable loop is now closed unattended: a `POST /requests` returns
  immediately as `pending` and the worker does the slow search/grab within ~60s.
  Because state lives in the DB (`status` + `next_search_at`), a restart mid-cycle
  simply resumes next tick — fixing the prototype's synchronous-grab stranding.
- Per-indexer escalation is **deferred**: there is one Prowlarr, which already
  rate-limits upstream, so a single global cycle-abort-on-raise plus the per-scope
  backoff is enough for the beta. If a multi-indexer future arrives, Radarr's
  per-indexer `EscalationBackOff` is the model to add (a new ADR).
- A future web-config knob for the interval / per-cycle cap / backoff table is a
  noted follow-up (they are module constants today, matching the reconcile and
  eviction interval constants).
- Schema change ships as Alembic revision `088a027cb4ec`
  (`search_attempts` + `next_search_at` on both tables); the contract change
  (`auto_grab_enabled` setting + `autograb` health panel) regenerates
  `openapi.json` + the typed client.
