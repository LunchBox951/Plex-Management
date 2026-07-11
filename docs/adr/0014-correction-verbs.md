# ADR-0014: Correction verbs — report-issue (blocklist + purge + re-search) and cancel

- **Status:** Accepted
- **Date:** 2026-07-01
- **Context builds on:** [ADR-0001](0001-integrated-app-borrowed-brains.md)
  (borrowed brains: Radarr-style blocklist + quality model),
  [ADR-0005](0005-zero-terminal-web-operability.md) (100% web-operable, no
  terminal), [ADR-0011](0011-tv-season-episode-support.md) (per-season TV
  lifecycle), [ADR-0012](0012-operability-health-logs-eviction.md) (the
  root-guarded eviction purge primitive + `library_path` breadcrumb this reuses).

## Context

The request→watchable loop and the disk-pressure eviction sweep both exist, but
once a title was **imported** there was still no in-app way to say either "this
file is bad, redo it" or "I don't want this anymore." Recovery meant SSH + a
manual file delete + hand-editing the DB + re-requesting — the exact terminal the
north stars forbid. Two independent gaps made this worse:

1. **A seeding leak.** `POST /queue/{id}/mark-failed` and the reconcile-driven
   `_handle_failed` path blocklisted a release and re-armed its request, but
   **never removed the torrent**. A blocklisted download kept seeding and holding
   disk forever. v2 removed a torrent in exactly one place (grab-race rollback).
2. **No imported-media correction.** The reference stack splits correction into
   two decoupled stages (Radarr's queue-remove vs a separate file-delete, plus
   Overseerr's human "issue" ticket that waits for an admin). None of them offer a
   single self-healing "this imported file is bad, fix it" action — and a ticket
   that waits for a human defeats an unattended single-machine beta.

## Decision

Two correction verbs, mirroring the two lifecycle stages, each made **self-healing**
so no human ticket is ever needed. Nothing is re-derived: both compose primitives
that already exist (the blocklist + two-tier identity, the eviction purge, the
decision engine, `grab_service.grab`).

### 1. Fix the seeding leak (both fail paths)

`mark_failed` (operator) and `_handle_failed` (reconcile-driven) now call
`qbt.remove(hash, delete_files=True)` **best-effort**: a failure is logged (never
silent — the leak is made visible), never raised (the committed blocklist/re-arm
must stand regardless of a client hiccup), and removing an already-gone hash is a
no-op success (qBittorrent's `/torrents/delete` tolerates an unknown hash). The
operator endpoint exposes `?remove_torrent=true` (default on).

### 2. report-issue — one self-healing action

`POST /requests/{id}/report-issue` (body: `reason` ∈ operator-choosable
`BlocklistReason` values; optional `season`) composes existing primitives **in
order**: (a) blocklist the culprit release (source_title/indexer/hash resolved from
the imported download's history, scoped by `tmdb_id` + media namespace); (b) remove
the torrent WITH data; (c) purge the library file via the shared root-guarded purge
primitive; (d) trigger a Plex scan; (e) re-arm the request/season to `searching` and
clear the `library_path`/`completed_at` breadcrumbs; (f) write an audit
`download_history` row (`reported`); (g) **synchronously** run the SAME
decision-engine → `grab_service.grab` path the grab endpoint uses.

**The re-search is synchronous, not a loop.** The reconcile loop only
reconciles/imports/confirms/evicts — it never grabs a `searching` request. So
re-arming alone would do nothing until an operator grabbed. report-issue runs the
re-grab inline (as the grab endpoint does) and returns the updated state; it rides
the existing cadence, adds no interval/backoff. The blocklist written in (a)
guarantees the decision engine picks a **different** release; if nothing is
acceptable it lands on the existing honest, retryable `no_acceptable_release` park.
That synchronous re-grab **is** the auto re-search AND the undo (the content comes
back), which is why the beta ships **no recycle bin** — a mistaken report self-heals,
and `DELETE /blocklist/{id}` un-blocklists if the replacement is worse.

### 3. cancel — the honest opposite

`POST /requests/{id}/cancel`, for a **not-yet-imported** request
(`pending`/`searching`/`no_acceptable_release`/`downloading`): remove any active
torrent(s) WITH data (best-effort) and settle the request — and, for TV, every
tracked season — to a new terminal `cancelled` status. The row is kept for history;
nothing is re-grabbed. A request past this stage is refused (409); report-issue
owns the imported stage instead.

### Purge semantics + the hardlink reasoning

The delete + scan + root-containment guard + hardlink-aware freed-bytes accounting
are factored out of `eviction_service._evict_one` into a shared
`purge_service` (`purge_library_path` + `trigger_library_scan` + `remove_torrent`),
used by eviction, report-issue, and cancel — one place for the safety guard.
`fs.delete` fails **closed** outside every configured library root (raises, never
mis-deletes) and treats an already-gone in-root path as an idempotent no-op.

**Hardlink caveat.** A same-filesystem import `hardlink_or_copy`-links the library
file to the download client's seed copy, so they can share an inode. Purging the
library file **alone frees nothing** (the seed link keeps the bytes) and the torrent
still holds the bad content. So report-issue removes **both** the torrent-with-data
**and** the library file — never just one.

**Foot-gun failsafe** (mirroring Radarr's `MediaFileDeletionService`): before
touching anything, report-issue verifies the media root is mounted and non-empty. An
unmounted drive would make `fs.delete` a silent no-op on a not-really-gone file, and
we would have blocklisted the good release and re-grabbed a duplicate. A missing root
aborts the whole verb (409), never fires against content that is still there.

### Schema

`RequestStatus.cancelled` and `DownloadHistoryEvent.reported`/`.cancelled` are added
to the Python enums. **No migration is required:** these columns are portable
`VARCHAR` (`native_enum=False` → no CHECK constraint, per the `import_blocked`
precedent), the new values fit the existing length, and `cancelled` is SETTLED — it
is excluded from `uq_media_requests_active`'s inclusion-list predicate **by
omission** (exactly like `available`/`failed`/`evicted`), so a later fresh request
for the same media is allowed. `season_rollup` folds `cancelled` alongside `evicted`
as a "gone" season (all-cancelled → `cancelled`).

### Auto-grab re-arm

report-issue's re-arm point resets the auto-grab backoff
(`search_attempts`/`next_search_at`) for the movie row or the reported TV season.
An operator-triggered re-search must not inherit the failed culprit's accrued
backoff; if it later parks again at `no_acceptable_release`, it starts the ladder
over from the first delay.

## Consequences

- Every post-grab failure now actually reclaims disk: no blocklisted/cancelled
  torrent seeds forever (north star #3: honesty over silence — the one place the
  prototype's reflex was safer than v2 is closed).
- "This imported file is bad" is a single button, and "I don't want this" is its
  honest opposite — both web-operable, no terminal (north stars #1/#2).
- The purge safety guard + hardlink accounting live in exactly one place, shared by
  three callers.
- Deferred (a later ADR if wanted): a configurable trash-dir / recycle bin. The
  synchronous auto re-grab is the beta's undo, so it is not needed now.
