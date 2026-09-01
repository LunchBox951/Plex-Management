# ADR-0029: Durable pre-add intent for the grab window

- **Status:** Proposed
- **Date:** 2026-08-10
- **Proposes to resolve:** [issue #477](https://github.com/LunchBox951/Plex-Management/issues/477)
  (in-flight grabs are invisible to the park/eviction guards) and
  [issue #481](https://github.com/LunchBox951/Plex-Management/issues/481)
  (a crash inside the grab window strands a client-only torrent with no in-app
  correction path). Both were deferred out of the 1.0 milestone on 2026-08-02 as
  one post-1.0 design effort; this is that design.
- **Numbering note:** `0021` is legitimately used twice
  ([Plex watchlist automation](0021-plex-watchlist-request-automation.md) and
  [Trusted-Host setup hardening](0021-trusted-host-setup-hardening.md)) — those
  are not renumbered. `0028` is informally earmarked by the #484 auth series
  (its PR-6 extends [ADR-0016](0016-plex-oauth-owner-sessions.md)), so this ADR
  takes the next free number, `0029`. The abandoned PR #538 carried a stub draft
  also numbered `0028`; that number is deliberately **not** reused here, and this
  ADR supersedes nothing — the #538 draft was never merged.
- **Qualifies:** [ADR-0006](0006-download-client-port-qbittorrent.md) — the
  `DownloadClient` port contract gains a two-phase add (`prepare_add` /
  `add_prepared`) with `add` retained as a compatibility composition. ADR-0006's
  decision (a client-neutral port with qBittorrent as the v1 adapter) is
  unchanged; only the add surface widens.
- **Context builds on:** [ADR-0006](0006-download-client-port-qbittorrent.md)
  (the `DownloadClient` port is the stable cross-boundary contract),
  [ADR-0007](0007-sqlite-alembic-migrations.md) (SQLite single-writer
  serialization; schema owned by migrations),
  [ADR-0012](0012-operability-health-logs-eviction.md) (the eviction claim CAS),
  [ADR-0013](0013-auto-grab-worker.md) (the background search→grab spine),
  [ADR-0014](0014-correction-verbs.md) /
  [ADR-0022](0022-claim-before-purge-correction-order.md) (correction verbs and
  the claim-before-purge ordering rule this ADR extends one step earlier),
  [ADR-0024](0024-first-party-container-auto-updater.md) (the expand-only N/N-1
  schema rule), and [ADR-0026](0026-redact-at-rotation-historical-log-secrets.md)
  (secret-material handling).
- **Relates to:** [PR #524](https://github.com/LunchBox951/Plex-Management/pull/524)
  (merged — the delete-authorized vs. delete-started marker boundary, the direct
  analogue of this design one layer down) and
  [issue #526](https://github.com/LunchBox951/Plex-Management/issues/526)
  (closed by [PR #568](https://github.com/LunchBox951/Plex-Management/pull/568),
  commit `881b8cbe` — the per-root pressure-exclusion lease; the landed
  ordering is recorded below).

## Context

### The window

`grab_service.grab()` resolves a release source and awaits `qbt.add(...)`
**before** it persists anything durable about the grab. The `downloads` row, its
`download_scopes`, its `download_coverage_claims`, and the append-only
`download_history` `grabbed` event are all created after the client has already
been mutated, and committed after that. The module docstring records the
ordering in its own words (`src/plex_manager/services/grab_service.py:71`):

> `grab` awaits `qbt.add(...)` BEFORE it moves the request/season to
> `downloading`, and a concurrent writer … can commit a new status during that
> await.

That comment exists because the post-add status move already had to become a
compare-and-swap against exactly the status the decision observed. The CAS
handles *statuses* racing across the await. It does not, and cannot, handle the
two problems below, because both are about state that **does not exist yet**.

### #477 — the physical-coverage guard is not held during the add

Issue #456 made "at most one active download covering `(request, season)`" a
database invariant via `download_coverage_claims` and the partial unique index
`uq_download_coverage_claims_active`. That invariant is real — from commit
onward. During the add await there is no claim row and no `downloads` row, so
every guard that reads committed state reads "unclaimed":

- **Auto-grab's park CAS.** `require_no_active_coverage`
  (`src/plex_manager/repositories/season_requests.py:540-552`) appends
  `~EXISTS(coverage claim JOIN a non-terminal download)` to the UPDATE
  predicate. Neither side of that join exists mid-add, so the predicate is
  trivially satisfied and a season whose torrent is being submitted right now
  can be parked `no_acceptable_release` — or folded to `completed` by the
  episode-fallback branch (`services/auto_grab_service.py:680`, `:909`).
- **Eviction.** `_coverage_claim_active`
  (`src/plex_manager/services/eviction_service.py:391`) reads the same committed
  table immediately before deleting, precisely so a pack's scopeless ride-along
  season is protected. A ride-along covered by an in-flight pack reads as
  unclaimed and can have its library files deleted while the pack is fetching
  it.

Outcomes are bounded and self-healing — a false park is retryable, a raced
eviction re-downloads, no data is lost — and the Jul 25 – Aug 1 canary soak
logged zero occurrences. That is why #477 is post-1.0, not why it is acceptable
forever: the guard the #456 design exists to provide is structurally absent for
the duration of every grab.

### #481 — a crash in the window strands an invisible torrent

qBittorrent accepts the add; the process dies before the commit. On restart
there is no `downloads` row. `queue_service.reconcile_and_list`
(`src/plex_manager/services/queue_service.py:1299-1312`) *deliberately* scopes
its client snapshot to exactly the tracked rows' hashes via
`get_statuses_for_hashes` — issue #216, a cost bound so the frequent reconcile
poll stays cheap against a shared qBittorrent with a large unrelated inventory.
Every reader downstream of that snapshot is therefore addressed by hashes
derived from rows that do not exist. The torrent downloads, completes, and seeds
indefinitely, consuming disk and bandwidth, invisible to the queue, the request,
health, and every correction verb.

The only ways to find and remove it are the qBittorrent Web UI or a shell. That
is a direct violation of **north star 1** ("every failure mode has a first-class
in-app correction path … a *button*, never a terminal") and, on `:stable`, of
**north star 2**. The 2026-08-02 triage acknowledged the conflict explicitly.
It is also self-concealing: the Aug 2 canary checkpoint found zero suggestive
log lines, with the honest caveat that *the missing telemetry is itself part of
the gap* — there is no signal to grep for, because nothing in the app knows the
torrent exists.

### Why these are one design

#477 is closed by making the window **claimable** (durable state committed
before the add). #481 is closed by making the window **recoverable** (durable
state committed before the add, plus a way to see what the client actually
holds). The same write is the foundation of both. The disagreement between the
cheap fix and the full fix is only about how much has to be built *around* that
write.

### Why the ordering cannot simply be inverted

"Commit first, then add" is not a reordering. The `downloads` row is
hash-keyed: `torrent_hash` is globally unique, `get_by_hash` is the idempotency
anchor, `uq_downloads_active_request` and the same-hash re-grab guard both key
off it, and the entire reconciler is hash-addressed. A magnet carries `btih`; an
opaque `.torrent` URL that the client fetches on our behalf does not, and
`AddResult.torrent_hash` is documented as possibly empty for exactly that case.
Persisting before the add therefore *requires* the info-hash to be knowable
before the client is touched — which requires splitting source resolution out of
the add call. That is why the port contract is part of this ADR and not an
implementation detail.

## Constraints any solution must preserve

- **C1 — No committed gap.** At every interruptible point, the
  `(request, season)` footprint a submitted-or-about-to-be-submitted torrent
  covers is either claimed in the database or provably not submitted. The window
  may move; it may not remain unclaimed.
- **C2 — Ownership proven, never inferred.** Nothing may remove a torrent (with
  data) from a shared qBittorrent unless ownership is proven by an app-written
  identity token or an explicit operator adoption. Hash coincidence, category
  coincidence outside our own namespace, and title matching are never proof.
- **C3 — Every residual state is a button.** Any intent or client torrent the
  system cannot resolve automatically must appear in the app with an operator
  action attached (north stars 1 and 2).
- **C4 — Honest, bounded, isolated recovery.** A per-item recovery failure must
  not abort the reconcile cycle, must not degrade into a perpetual rewrite loop,
  and must not be silently swallowed (north star 3).
- **C5 — Expand-only, N/N-1 compatible schema.** ADR-0024's rule: the release
  that can be rolled back to must be able to read — and must not be corrupted
  by — what the newer release wrote.
- **C6 — No regression of the #216 scoped snapshot.** Widening reconciliation
  must not restore whole-inventory polling at the reconcile cadence on a shared
  client.
- **C7 — Coexistence, not replacement.** `download_coverage_claims`,
  `uq_downloads_active_request`, `uq_download_scopes_active_scope`, ADR-0022's
  claim-before-purge ordering, PR #524's delete-authorized/delete-started
  boundary, and #568's per-root pressure-exclusion lease (#526) all keep their
  current semantics.
- **C8 — Single-writer SQLite.** Correctness arguments may lean on ADR-0007's
  serialization the way ADR-0022 does, and must say so where they do.

## Options considered

### (a) Full durable pre-add-intent stack *(recommended)*

A durable intent row committed before `qbt.add`, a port split that makes the
info-hash knowable beforehand, and a recovery + client-only correction surface.
Detailed in **Decision** below.

### (b) Placeholder claim row before `qbt.add` *(the narrow fallback)*

Keep `add()` un-split. Immediately before the add await, insert and commit a
claim row into the existing coverage-claim namespace owned by a placeholder
rather than by a `Download`; after the add returns, create the real row and swap
the claim's owner.

- **Closes #477 for TV.** The park CAS and the eviction read consult the same
  table; a claim committed before the add is visible to both. Movies need
  additional work — nothing writes or reads a movie claim today (see *Movies*
  under **Interaction**), so they are scoped as I1b rather than assumed.
- **Does not touch #481 at all.** With no hash resolved before the add, the
  placeholder cannot name the torrent, so a crash in the window still leaves a
  client-only torrent that nothing can identify, adopt, or remove. North star 1
  stays violated for that path.
- **Adds one new residue of its own:** a crash now also strands a placeholder
  claim that blocks the season until something reaps it. That reaper needs a
  staleness rule (age- or process-generation-based) — a heuristic, tolerable
  only because reaping a claim is non-destructive.
- **Rejected as the complete answer, adopted as increment 1** — see the staging
  in Decision. Framed correctly, (b) *is* (a)'s first layer with the hash and
  the client-facing halves deferred, so a partial landing is a step rather than
  a fork.

### (c) Category-scoped reconciliation sweep + adopt/remove UI only

No intent row, no schema on the grab path: periodically call
`get_all_statuses(category="plex-manager")`, diff against tracked hashes, and
give the operator adopt/remove buttons for the remainder.

- Genuinely the cheapest north-star-1 patch for #481's *visibility* half, and
  needs no port change.
- **Rejected as the complete answer** on three counts: it does nothing for #477
  (the guard window stays wide open); with no intent row the app cannot
  distinguish a crash orphan from a torrent an operator categorized
  `plex-manager` themselves, so every adoption is manual guesswork with no
  request association to offer; and removal-with-data would rest on inferred
  ownership, which C2 forbids.
- **Its sweep is adopted** as increment 3 of (a), where an intent row gives each
  observation a provenance.

### (d) Status quo plus telemetry

Log the race and the orphan so they become observable; defer the fix.

- **Rejected.** The Aug 2 checkpoint already demonstrated the problem: there is
  nothing to instrument, because in the #481 case the app has no knowledge of
  the torrent to log about. "We would see it if it happened" is a monitoring
  posture, not a correction path (north star 1). Useful telemetry does exist —
  counting pre-add commits, activations, and recoveries — but only *after* the
  intent row exists to count, so it rides on increment 1 rather than replacing
  it.

### (e) Serialize all grabs behind a global lock

- Closes #477 by construction: no concurrent actor can observe the window.
- **Rejected.** It does nothing for #481 (a crash still strands the torrent), it
  converts a bounded race into a throughput ceiling on the auto-grab worker, and
  a process-local lock is not durable across exactly the restart #481 is about.

## Decision (recommended)

Adopt **option (a)**, the three-part durable pre-add-intent stack, with four
load-bearing refinements over the shape PR #538 attempted (each traced to a
lesson in *What PR #538 taught*):

- **R1 — one claim namespace, two possible owners.** The intent does not get its
  own parallel claim table. `download_coverage_claims` gains a nullable
  `intent_id` alongside a now-nullable `download_id`, with exactly one owner set.
  Activation is an **owner swap on the same row**, never a release-and-retake.
  This removes the *cross-table* hole — the collision between an intent-owned and
  a download-owned claim becomes a database fact, keyed by the existing
  `uq_download_coverage_claims_active` (`models.py:974-981`), with no second
  constraint to add and no second table to join.
  **It does not remove all read-side discipline.** Because a grab now creates a
  claim it will later own itself, every conflict *read* must exclude the caller's
  own intent — see *Self-exclusion* below. The honest claim is: one namespace
  eliminates the "two tables, no constraint" defect class (lesson L2) and
  replaces it with a narrower, enumerable obligation (self-exclusion at eight
  known call sites), not with nothing.
- **R2 — no `submitted` state.** `prepared` means "the client may or may not
  hold this hash — ask." Only a *successful* probe returning absent is proof of
  absence; a raised client error means unknown, retry.
- **R3 — the hash is the ownership token; the category corroborates.** The
  info-hash `prepare_add` derived **locally, before the client was touched**, and
  committed to the intent row is the primary identity. Submissions use a single
  fixed `plex-manager-intent` category (not per-intent — see Part 3), and
  activation recategorizes to `plex-manager` *after* commit. Categories are
  mutable and therefore corroborating evidence only, never sole proof; the tiered
  rule for what each level of evidence authorizes is in *Ownership model* below.
- **R4 — per-item isolated recovery, behind reconciliation.** Each intent
  commits independently, with backoff; no per-item outcome may propagate as a
  client-wide or cycle-fatal failure; a parked intent is **inert in every guard**
  and **visible in the UI**.

### Self-exclusion: the obligation R1 creates

Today `_active_conflict_for_targets` (`grab_service.py:671`) answers "is a
**different** active download holding any of these seasons?", and it distinguishes
*different* from *mine* by comparing `torrent_hash`. Once a grab commits its own
intent-owned claims before the add, that comparison no longer identifies the
caller, and the failure is not theoretical:

- The pre-add guard at `grab_service.py:1302` runs only when `known_hash is not
  None`, precisely because a hashless candidate makes `active.torrent_hash !=
  known_hash` trivially true for **every** active row. An intent whose hash is
  still NULL (increment I1) is exactly that case: it would 409 every grab.
- The post-add sites pass the real `torrent_hash`, but an I1 intent has no hash
  to match, so the grab's *own* claim reads as a foreign conflict — and the
  handlers there call `_remove_torrent_if_added`, **deleting the torrent the
  grab just added**.
- Concretely: `grab_service.py:1740`'s `_claim_covered_seasons` →
  `ensure_coverage_claim` (`repositories/downloads.py:757-798`) looks up an
  existing claim by `(download_id, media_request_id, season_number)`. The grab's
  own claim is intent-owned, so `download_id` does not match, the lookup misses,
  and it INSERTs — colliding with its own claim on
  `uq_download_coverage_claims_active`. The `IntegrityError` handler at `:1748`
  then reads the collision as foreign and removes the torrent.

**Therefore:** `_active_conflict_for_targets` takes an explicit
`exclude_intent_id` (the caller's own intent), and every one of its **eight**
call sites must pass it — `grab_service.py:926, 992, 1302, 1372, 1458, 1497,
1590, 1748`. Correspondingly, `_claim_covered_seasons` / `ensure_coverage_claim`
must treat "a claim for this season already owned by *my* intent" as the
**owner-swap path**, not as an insert. Self-exclusion by intent id (not by hash)
is required because the hash is NULL for the whole of I1 and is unknown in the
ambiguous-add window even in I2.

### Ownership model (what each level of evidence authorizes)

Categories are mutable by the operator and by any other tool pointed at the same
qBittorrent, so a category is **not** a tamper-evident token. Tampering is
one-directional in both directions and both directions matter: relabelling a
foreign torrent *into* our namespace would let the app authorize a
delete-with-data on someone else's data, and relabelling one of ours *out* of it
strands our own in-flight torrent. The rule is therefore tiered by consequence:

| Evidence | Authorizes |
|---|---|
| Hash matches a `prepared` intent this app committed **before** the add | Tracking: activate, associate to the request, reconcile it |
| The above, **plus** `created=True` recorded from `add_prepared` | Destructive: remove-with-data on cancellation or cleanup |
| Explicit operator adoption in the UI | Both, for anything the above cannot prove |
| Category alone (`plex-manager` / `plex-manager-intent`) | Nothing on its own — a corroborating hint and a sweep filter only |

The middle row is the load-bearing one and it reuses a distinction the port
already draws: `AddResult.created` is documented as false when the client
reported the torrent **already present**, expressly so a failed grab never
removes a pre-existing torrent it did not create. When a crash loses it — the
`created` flag was never written — removal is **operator-gated**, not inferred.

**The gate is staged, because enforcing it before the UI exists would break
working buttons.** I2 writes `client_created` but the adopt/remove confirmation
path is I3 work, and on upgrade *every* pre-existing `Download` migrates to
NULL. A gate that hard-refuses on NULL would therefore make cancel and
report-issue unusable for the entire existing library the moment I2 lands, with
no in-app way through — a north-star-1 regression introduced by a north-star-1
feature. So the gate is enforced by value, not all at once:

- **`false`** (positively known not-created) → **gated from I2**. This value is
  only ever written by I2-and-later activation, so gating it immediately
  regresses nothing that previously worked.
- **NULL** (unknown, including every migrated legacy row) → **warn-only until
  I3**. Behaviour is exactly today's: the removal proceeds, and the UI states
  plainly that ownership could not be proven. When I3 lands the confirmation
  path, NULL is promoted to requiring explicit adoption.

The warn-only interval is not a gap in safety relative to today — it *is*
today's behaviour, surfaced honestly for the first time (north star 3).

**The flag must outlive the intent.** Activation deletes the intent row, so a
flag stored only on `download_add_intents` would evaporate one transaction after
the torrent became tracked — and the supersession rule below deliberately routes
`created=False` torrents through activation. `downloads` therefore gains its own
nullable `client_created`, written by activation from the intent's value, and
**that column is the gate for every remove-with-data in the codebase** — not
only intent cleanup, but ADR-0014's report-issue purge and the cancel verb,
which today remove with data unconditionally. A `Download` whose
`client_created` is false or NULL is removable from the *client* only after
explicit operator adoption; its library file, which the app did place, is
unaffected. Pre-existing rows migrate to NULL, and NULL must therefore be
treated as "unknown → ask", never as "false → silently skip cleanup": the
difference is visible in the UI, per north star 3.

**The gate proves a torrent *instance*, not a hash.** `client_created=True` is
evidence about the torrent that existed when `add_prepared` returned — and a
torrent is not its info-hash. An operator can delete our torrent and later
re-add the same release from another location; the historical `Download` row
survives with its flag intact, and report-issue would then remove **the
operator's replacement, with data**, on evidence that no longer refers to
anything. Two rules close it:

- **Proven absence invalidates the flag.** When reconciliation observes the
  hash absent in the client (the existing `ClientMissing` signal), the gate is
  cleared — it no longer describes any live torrent.
- **Instance identity is recorded and compared.** Activation persists the
  client's own creation timestamp for the torrent (qBittorrent's `added_on`,
  reported alongside the `category` field this ADR already adds to
  `DownloadStatus`), and destructive removal re-proves it matches before acting.
  A torrent re-added in a later second carries a new `added_on`, so it fails
  the comparison even when the hash and the flag both look right. The marker is
  coarse, though: qBittorrent reports `added_on` in Unix-epoch **seconds**
  (`adapters/qbittorrent/adapter.py:105-115`), so a delete-and-re-add that
  completes within the same second is indistinguishable by `added_on` alone.
  That collision is Open Question 17: the comparison may only *authorize*
  removal when the marker can distinguish instances, and must degrade to an
  operator-gated removal when it cannot. **This applies to every destructive
  removal, without exception** — report-issue, eviction cleanup, and the
  `cancel_requested` recovery path alike. The cancel path is not a special case
  that may remove on `client_created` alone: it removes with data, so it
  re-proves instance identity first.

The first rule alone is insufficient — a delete-and-re-add that completes
between two polls is never observed as absent — which is why the instance
marker, not the absence sweep, is the actual proof. The absence rule remains as
defence in depth and as the cheaper signal.
This is the named residual: the app may end up *tracking* a torrent an operator
had already added for the same release, which is benign and already true today;
it will not *destroy* one.

### Part 1 — the durable intent (schema)

New table `download_add_intents`:

| Column | Notes |
|---|---|
| `id` | PK |
| `state` | plain `String`, no CHECK constraint (mirrors `download_scopes.status`, so no column migration to add a state) |
| `torrent_hash` | lowercased info-hash resolved **before** the add. Nullable in increment 1, `NOT NULL` from increment 2. Uniqueness is **partial** — see *Hash uniqueness* below |
| `source_kind` | `magnet` \| `torrent_url` \| `torrent_file` |
| `source_ref` | the replayable source, **encrypted at rest**; credential-bearing indexer URLs are never persisted verbatim |
| `save_path`, `category` | the directed path (issues #133/#157) and the submitted category (the fixed `plex-manager-intent`) |
| `client_created` | nullable bool — `AddResult.created` as reported by `add_prepared`, written post-add. The proof that authorizes remove-with-data (see *Ownership model*); NULL means "unknown, operator-gated" |
| `submitting_since` | nullable timestamp — the **submission lease**. Stamped in the same pre-add commit; cleared on a proven outcome, **held under an extended expiry on an ambiguous one**. A live lease forbids the absent-transition from deleting the row (see *The submission lease*) |
| `media_request_id` | FK → `media_requests`, `ON DELETE SET NULL` |
| `observed_request_status`, `observed_season_status` | the premise the grab decision observed — the CAS operands at activation, exactly as `grab()` uses them today |
| `release_json` | the `ScoredRelease` fields needed to rebuild scopes and claims — **the release's** `target_seasons`/`covered_seasons`, not the request's stored episode filters (see lesson L10) |
| `attempts`, `last_error_code`, `needs_attention_reason` | bounded, non-secret, operator-legible |
| `created_at`, `updated_at`, `next_attempt_at` | `next_attempt_at` is the backoff anchor required by C4 |

New sidecar `download_add_intent_scopes` — one row per season in the intent's
**physical** footprint, i.e. `_active_guard_seasons`' full set (targets *and*
ride-alongs):

| Column | Notes |
|---|---|
| `id` | PK |
| `intent_id` | FK, `ON DELETE CASCADE` |
| `media_request_id`, `season_number` | scope identity; `NOT NULL` so NULLs cannot bypass the unique key below |
| `role` | `target` (will be imported) vs. `covered` (ride-along) — the same distinction `download_scopes` and `download_coverage_claims` already draw |
| `episodes_json` | episode filter, `target` rows only |

Unique key `(intent_id, media_request_id, season_number)`: one row per season
per intent. A retried intent construction or a supersession that re-attaches
scopes must therefore upsert rather than append, so activation never receives
duplicate `target`/`covered` rows for one season.

Changes to `download_coverage_claims` (R1):

- `download_id` becomes nullable; new nullable `intent_id` FK → `download_add_intents`
  (`ON DELETE CASCADE`); exactly one of the two is set.
- `uq_download_coverage_claims_active` keeps its key shape and predicate and
  therefore **already spans both owners** — an intent-owned claim and a
  download-owned claim for the same `(request, season)` collide at the database,
  with no new constraint to add and no second table to join. It does **not**
  remove read-side work: see *Self-exclusion* above for the obligation it
  creates in exchange.
- **Movies need a non-NULL discriminator *and* a writer and a reader.** The
  existing index keys on `season_number`, and SQLite (like Postgres) treats
  NULLs as distinct in unique indexes, so two movie claims with `season_number
  IS NULL` would not collide; the uniqueness key needs a non-null scope
  discriminator (a reserved sentinel, or an expression index over
  `COALESCE(season_number, -1)`). But the index is the *smallest* part of the
  movie story: nothing writes or reads movie claims today at all. See *Movies*
  under **Interaction** below — the sentinel alone closes nothing.

**States and transitions.** `prepared` is the only state in which the client may
hold an unknown torrent, and it is deliberately ambiguous by design (R2):

```
(none)           --create (before qbt add)-->      prepared
prepared         --present in client, category matches, premise holds-->
                                                   [activate: row deleted]
prepared         --proven absent, premise holds-->  prepared (re-submit)
prepared         --proven absent, premise stale-->  [claim released, row deleted]
prepared         --source unresolvable / hash mismatch / premise conflict
                   / foreign-category hash-->       needs_attention
prepared         --operator cancel-->               cancel_requested
cancel_requested --proven absent AND no live submission lease-->
                                                    [row deleted]
cancel_requested --proven absent BUT lease live-->  cancel_requested (wait for
                                                    the submitter to resolve)
cancel_requested --present and owned-->             remove, then [row deleted]
cancel_requested --client unavailable-->            cancel_requested (retry;
                                                    UI reports cleanup deferred)
needs_attention  --a NEW intent resolves the same hash, AND the park's
                   reason is self-inflicted (duplicate-add / resolved
                   premise) -- NOT foreign-category or hash-mismatch-->
                                                    [superseded: row deleted,
                                                     torrent adopted by the new
                                                     intent]
needs_attention  --operator adopt-->                [row deleted, torrent
                                                    tracked]
needs_attention  --operator discard, claims NOT retained-->
                                                    [row deleted]
needs_attention  --operator discard, claims RETAINED-->
                                                    [refused until the operator
                                                     picks remove / adopt /
                                                     prove-absent]
```

`activated` is not a resting state: activation is one transaction that creates
the `Download`, swaps every claim row's owner from `intent_id` to `download_id`,
attaches `download_scopes` for `target` scopes, writes the `grabbed`
`download_history` event, and deletes the intent. Because the claim rows are
*swapped* rather than released and retaken, C1 holds structurally: there is no
instant at which the footprint is unclaimed.

**Activation must converge on an existing same-hash `Download`, never retry
into it.** The intent and `downloads` are separate tables with no constraint
between them (R1 unifies the *claim* namespace, not the row namespace), so a
second intent can activate — creating a `Download` for the same hash — in the
window between this grab's early known-hash check and its own intent commit.
Nothing collides at that point; the two records simply coexist. Activation then
tries to `create()` a `Download` whose `torrent_hash` is globally unique, fails,
and — if it treats that as a transient conflict — retries forever, re-failing
every cycle on a row that will never go away.

So activation's first step is a **re-read of `downloads` by hash**, and its
outcome is a convergence decision, not a retry: adopt the existing row if it
serves this intent's request/scope (attaching scopes and swapping claim owners
onto it), or release this intent and let the existing row stand if it does not.
This is the same shape `grab_service._resolve_same_hash_owner` already
implements for the download-vs-download case, and it should reuse that helper's
logic rather than grow a parallel one. A uniqueness violation on `torrent_hash`
during activation is therefore always a **bug or a lost race to be converged**,
never a condition to retry unchanged.

**The client recategorize is deliberately NOT in that transaction.** Setting the
torrent's category is an irreversible external side effect, and putting it
inside a rollbackable transaction inverts ADR-0022's rule — and contradicts this
ADR's own lesson L7. A crash after the recategorize but before the commit would
leave the intent `prepared` while its torrent now sits under `plex-manager`; a
category-only ownership test would then read the app's *own* torrent as foreign,
park it, release its claims, and let the request grab a duplicate — reproducing
#481 exactly. So: **commit first, then recategorize as reported best-effort**
(per L7 — a committed command never fails on cleanup, and reports what it
deferred). A recategorize that does not land leaves the torrent under
`plex-manager-intent`, which the sweep and the recovery probe both still
recognize as ours; the next reconcile retries it.

Consequently the recovery probe's category test **accepts either**
`plex-manager-intent` or `plex-manager` as ours-by-corroboration, because those
are precisely the two states the post-commit ordering can leave behind.

**A deferred recategorize needs a durable trigger, and one reader must be able
to see the category.** "Best-effort, retried next cycle" is not free: once the
intent row is deleted, nothing records that the recategorize is still owed. A
crash between the activation commit and the `set_category` call — or a
`set_category` that simply fails — leaves the torrent under
`plex-manager-intent` permanently. Neither existing reader can repair it:
tracked reconciliation works from `DownloadStatus`, which **exposes no category
field at all**, and the I3 sweep subtracts tracked hashes, so an activated
torrent is excluded from the very sweep that would notice its category. The
result is a silently mislabeled torrent that also violates the sweep's premise
that `plex-manager-intent` means "not yet activated".

Two additions close it. First, `downloads` gains a `pending_recategorize` flag,
set inside the activation transaction and cleared only on a **confirmed**
`set_category`; the reconcile loop, which already iterates tracked rows, retries
any row still carrying it. That is the durable trigger, and it survives the
intent's deletion because it lives on the row that outlives it. Second,
`DownloadStatus` gains a `category` field — qBittorrent's `/torrents/info`
already returns it, and this ADR is already qualifying the port — so the retry
can *verify* rather than blind-write, and an externally recategorized torrent
becomes detectable instead of invisible. The flag is the requirement; the field
is what makes the repair honest.

**The submission lease.** R2 makes `prepared` deliberately ambiguous — "the
client may or may not hold this hash, ask" — and the recovery algorithm resolves
that ambiguity by probing. But a probe cannot distinguish *"never submitted"*
from *"a submitter is inside `add_prepared` right now and the client has not
finished accepting it"*. Without a durable record of submission-in-progress,
cancellation observes the hash absent, deletes the intent, and the in-flight call
then creates a torrent with **neither an intent nor a `Download`** behind it —
#481 reproduced by the very path meant to prevent it. A CAS on the intent row
cannot close this: the race is against an external await that no committed state
describes.

So the pre-add commit — the one that already writes the intent and its claims —
additionally stamps `submitting_since`. **No absent-transition may delete or
release an intent while its lease is live**; the cancel path leaves the row in
`cancel_requested` and retries on a later cycle, when the submitter will have
recorded its outcome. The lease carries a timeout because a crashed submitter
would otherwise pin the intent forever — and expiry is safe precisely because a
crashed process has no in-flight call: after the timeout the probe-based logic
is sound again. The timeout must exceed the adapter's own add timeout, and its
expiry is a surfaced event, not a silent reclaim (north star 3). This costs no
extra commit; it adds one column to a write that already happens.

**When the lease clears depends on which kind of outcome `add_prepared`
returned** — "resolves either way" is too coarse, and it reintroduces the race
for the case that matters most. The port's failure taxonomy (Part 2, obligation
5) already separates *proven rejection* from *ambiguous*, and the lease must
honour that split:

| `add_prepared` outcome | Lease |
|---|---|
| Proven success (a hash came back) | Cleared; activation proceeds |
| **Proven rejection** (a response shape that conclusively proves the client did not accept) | Cleared immediately — nothing was submitted |
| **Ambiguous** (proxy 502/504, 5xx after mutation, unparsable) | **Held**, under an extended expiry |

The ambiguous row is the point. An interposed proxy can return 504 while the
upstream `POST /torrents/add` is still in flight, and the torrent may appear
*after* the call returned. Clearing the lease there would let an immediate
probe — which correctly reports the hash absent, because it has not landed
yet — free the intent moments before the torrent materialises: the original
orphan, reached by a narrower door. So an ambiguous outcome keeps the lease and
extends it, trading a bounded delay in cancellation for the guarantee that no
absent-transition can outrun a submission that is still settling. The extended
window must exceed the plausible upstream-settling time, and, as above, its
expiry is surfaced rather than silent.

**Superseding a parked intent (the re-park loop).** A parked intent releases its
ownership tokens (L3) but its client torrent may still exist under our intent
category. A later grab of the same release resolves the *same* hash, and
`add_prepared` is then a qBittorrent duplicate-add no-op that returns
`created=False` and leaves the existing category untouched. Without an explicit
rule the new intent probes, finds a torrent it did not create, parks — and the
cycle repeats forever. The rule: **an identical hash is identical content**, so
when a new intent resolves a hash already held by a parked intent's torrent, the
new intent **supersedes** the parked row — its torrent is adopted by the
successor, which proceeds to activation normally.

**Supersession transfers claims; it must not delete-then-reacquire.** A parked
intent that retained its claims (the liveness rule above) still owns them under
`uq_download_coverage_claims_active`, so a successor cannot acquire its own —
and deleting the parked row first would `ON DELETE CASCADE` those claims out of
existence, momentarily unclaiming the footprint of a torrent that is still
downloading. Both orderings are wrong. Supersession is therefore a single
transaction that **owner-swaps every retained claim from the parked intent to
the successor** and then deletes the now-claimless parked row — the identical
move activation makes when swapping `intent_id` to `download_id`, applied one
step earlier. C1 holds for the same structural reason: the claim row never stops
existing, it only changes owner.
Because `client_created` is false or NULL on that path, the successor may track
and import the torrent but may not remove it with data without operator
adoption (see *Ownership model*), and that restriction now travels onto the
`Download` row.

**Supersession is gated on the park's reason, not on hash identity alone.**
`needs_attention` has four entry causes, and they do not all mean the same
thing. A park caused by *our own* duplicate-add no-op — the loop described above
— is a self-inflicted bookkeeping problem and is exactly what supersession
exists to clear. A park caused by the **foreign-category** finding is the
opposite: a positive, evidence-based refusal to touch a torrent this app did not
create (C2, lesson L6). Superseding on hash identity alone would silently
reverse that refusal the moment anyone re-grabbed the same release — the app
would adopt the foreign torrent it had just declined to adopt, and, once the
`created` gate is satisfied by some later path, could destroy it. So the parked
row carries its `needs_attention_reason`, and supersession applies **only** to
self-inflicted causes (duplicate-add, and premise-conflict parks whose premise
has since resolved). Foreign-category and hash-mismatch parks stay
operator-gated and are surfaced, never auto-cleared.

**`needs_attention` is two states, not one.** L3's rule — a parked intent is
inert in every guard — was derived from parks that are *historical*: the
incident is over, nothing of ours is running in the client, and retaining
tokens would permanently block parking and eviction for the title. But two park
causes leave **live client mutations this app created**: a hash-mismatch /
re-submit-cap park may have leaked one or more torrents that are downloading
right now, and a premise-conflict park may have an owned torrent still running.
Releasing coverage for those would make our own live torrents invisible to the
park and eviction guards and admit a replacement grab — recreating exactly the
unclaimed-coverage window of #477 that this ADR exists to close.

So parks are classified on **two independent axes**, and the reason code carries
both:

| Park cause | Supersedable? (round-3 rule) | Claims on park |
|---|---|---|
| `duplicate_add` — our own duplicate-add no-op | **Yes** | Per the liveness rule below |
| `premise_conflict` — request/season moved on mid-flight | Yes, once the premise resolves | Per the liveness rule below |
| `hash_mismatch` / `resubmit_cap` — we created torrents we cannot address | No | **Retained** |
| `foreign_category` — the hash is under a category that is not ours | No (a provenance refusal) | Per the liveness rule below — **not** automatically released |
| `source_unresolvable` — never submitted | Yes, once a source resolves | Released (inert) |

**The liveness axis keys on evidence of an app-created torrent, never on the
category.** An earlier draft released claims for every `foreign_category` park
on the reasoning that "the hash belongs to someone else". That is wrong
whenever `client_created` is true: `add_prepared` positively reported creating
this torrent, and a category is mutable, so an *externally relabelled*
app-created torrent lands in the `foreign_category` branch while still being a
live mutation this app performed. Releasing its coverage would reopen the very
#477 gap this design closes. The rule is therefore evaluated per row, not per
cause:

> **Retain claims whenever the intent carries evidence of a live app-created
> torrent** — `client_created` is true, or it is NULL *and* the client has not
> proven the hash absent. Release only when creation is positively disproven
> (`client_created` false) or absence is proven.

`foreign_category` still governs the *provenance* decision — the app does not
adopt, recategorize, or destroy — but provenance and liveness are independent
questions, and only the second one decides claims. `source_unresolvable` is the
one cause that is inert by construction: it parks before any submission, so
there is nothing live to protect.

Retention is **conditional and terminating**, which is what reconciles it with
L3: a retained claim is held only until its torrent is removed, adopted, or
proven absent, and each of those is a button in the I3 surface. It is not the
indefinite hold L3 warned about — that defect was a *permanent* block with no
exit. What must never happen is the other failure: releasing tokens for a
torrent that is still moving bytes. Where the two rules would conflict, **the
liveness axis wins**, because an invisible live torrent is the more dangerous
state.

**Operator discard is qualified by the same axis.** A bare "discard" on a park
that retains its claims would delete the row and cascade the claims away —
exactly the unclaimed-live-torrent state the retention exists to prevent, only
reached by a button instead of a bug. So discard is offered unconditionally only
for inert parks. For a claim-retaining park the UI does not offer a bare
discard: it requires the operator to first resolve the torrent — **remove** it
(subject to the destructive gate), **adopt** it into a request, or **prove it
absent** (a probe confirming the client no longer holds it) — after which the
park is inert and discard proceeds. This keeps north star 1 satisfied (there is
always a button) without letting the button reintroduce the defect.

In every case the `torrent_hash` ceases to *reserve* the hash against a future
grab (see *Hash uniqueness*), and the row is retained with its reason and
surfaced in the UI. Partial release is still the defect L3 records; the fix is
to release the right things, not fewer things.

**Hash uniqueness is partial, not absolute.** Three requirements collide if
`torrent_hash` carries a plain `UNIQUE`: the column is `NOT NULL` from I2, a
parked intent must stop reserving its hash (L3), and supersession must still
find the parked row *by* that hash. Nulling the column on parking would satisfy
the second at the cost of the first and third. The resolution is to **retain the
value and drop it from the uniqueness scope** — a partial unique index over
`torrent_hash` restricted to the live states (`prepared`, `cancel_requested`),
exactly the shape `uq_download_coverage_claims_active` already uses to exclude
`released` claims, and supplied per-dialect for the same reason. A parked row
keeps a queryable hash, reserves nothing, and cannot collide with the successor
that supersedes it.

### Part 2 — the `DownloadClientPort` split

`add()` today does three things in one await: resolve the source (possibly an
HTTP fetch), submit it, and report the resulting hash. The split makes the first
two separable:

```python
class PreparedAdd(BaseModel):  # frozen, like AddResult / DownloadStatus
    torrent_hash: str  # lowercased, non-empty — the whole point
    payload_ref: ...  # opaque handle to the resolved payload
    save_path: str
    category: str


async def prepare_add(self, magnet_or_url: str, save_path: str, category: str) -> PreparedAdd: ...
async def add_prepared(self, prepared: PreparedAdd) -> AddResult: ...
async def add(
    self, magnet_or_url: str, save_path: str, category: str
) -> AddResult: ...  # compatibility composition
```

Contract obligations, all of which belong in the port's docstrings because the
port is ADR-0006's stable cross-boundary contract:

1. **`prepare_add` performs no client mutation.** It may perform network I/O.
2. **`prepare_add` must derive a non-empty hash or fail** — this is the property
   that makes hash-keyed persistence-before-add possible at all.
3. **`prepare_add` failures are per-source, never client-wide.** A distinguishable
   source-error type is required so a single expired one-shot indexer URL cannot
   be misread as a qBittorrent outage (lesson L4).
4. **v2-only magnets must not regress.** A magnet carrying `xt=urn:btmh:` with no
   `btih`, for a candidate that also supplies the indexer's `infoHash`, is
   grabbable today via the post-add fallback. `prepare_add` must either derive
   the v2 hash from `btmh` or accept the caller-supplied candidate hash;
   rejecting it before submission removes a supported route.
5. **`add_prepared` is the only mutating call, and its failure taxonomy must
   separate *proven rejection* from *ambiguous*.** A 502/504 from an interposed
   proxy, or a 5xx from qBittorrent after it mutated, means the torrent may
   exist. Only the adapter sees the response shape, so only the adapter can draw
   that line — and the caller releases the intent **only on proven rejection**.
   `add_prepared` continues to report `AddResult.created`, and the caller
   persists it onto the intent (`client_created`) as soon as it returns: that
   flag is the only thing that later authorizes remove-with-data (see *Ownership
   model*), and it cannot be reconstructed after a crash.
6. **No credential-bearing source is retained in `PreparedAdd`,** and the
   persisted `source_ref` is encrypted (ADR-0026 posture: secrets are never
   logged and, here, never stored in the clear).
7. **An adapter that cannot satisfy obligation 2 keeps using the compatibility
   `add()`,** and its deployments honestly retain today's window rather than
   pretending to a guarantee they cannot make.

### Part 3 — reconciliation widening and the correction surface

The reconcile loop gains two readers around the existing one:

1. **Intent recovery** (per cycle, scoped). For each `prepared` /
   `cancel_requested` intent whose `next_attempt_at` is due, probe the client for
   that exact hash via `get_statuses_for_hashes` — scoped, so C6 holds. Then:
   - present **and** category is `plex-manager-intent` or `plex-manager` →
     activate. For `cancel_requested`: if `client_created` is true, remove with
     data and **then delete the intent row**; otherwise surface it for operator
     adoption and **retain the row**, which is the operator's only handle on the
     torrent just surfaced — deleting it here would strand the very thing the
     surfacing exists to expose;
   - present but category is anything else → **do not adopt.** On a shared
     qBittorrent the hash may belong to a pre-existing torrent this app never
     created; recategorizing and later removing it destroys someone else's data
     (C2, lesson L6). Park `needs_attention` and surface it;
   - proven absent (a successful probe returning nothing) → re-submit if the
     premise still holds, otherwise release the claim and delete. **Re-submits
     are counted and capped** (`attempts`); on exceeding the cap the intent
     parks `needs_attention` instead of submitting again;
   - client raised → leave the state alone, back off, retry.

   **Why the re-submit branch needs that cap.** The probe asks about *our*
   hash, so a client that resolved a **different** hash than `prepare_add`
   derived is indistinguishable from "never added" — the probe returns nothing
   and the naive branch re-submits, leaking one torrent per attempt forever.
   Absence alone therefore cannot authorize unbounded re-submission. The cap
   bounds the leak to a small constant and converts the condition into a
   surfaced, operator-actionable state; the I3 sweep independently catches the
   leaked torrents, which sit under `plex-manager-intent` and match no tracked
   or intent hash. Where the sweep's unmatched set is already available, the
   re-submit branch should consult it first and park immediately rather than
   burning the cap.

   Each intent commits independently; nothing here may raise into the cycle
   (C4, lessons L4/L5).
2. **Tracked-download reconciliation** — unchanged, still `get_statuses_for_hashes`
   over tracked rows.
3. **Client-only observation sweep** (its own slow cadence, *not* the ~15 s
   reconcile). Two exact-match `get_all_statuses(category=...)` calls — one for
   `plex-manager`, one for `plex-manager-intent` — diffed against tracked hashes
   ∪ intent hashes. **"Tracked" here means every `downloads` row's hash, not
   just the active ones**: an imported torrent is terminal in our state machine
   but frequently still present in the client and seeding, and deriving the
   subtraction set from `list_active()` would misclassify every one of them as
   client-only and offer the operator a remove button for a healthy seeding
   import. The remainder is recorded as bounded observations in
   `client_only_torrents`. Its cadence is deliberately decoupled from the
   reconcile poll so C6 holds, and it is **not** a category-free inventory: an
   operator's unrelated torrents are none of the app's business.

   **Why the category is fixed and not per-intent.** qBittorrent's
   `/torrents/info` category filter is **exact-match with no wildcard**
   (`adapters/qbittorrent/adapter.py:1540-1544` passes `category` straight
   through), so a `plex-manager-intent-{id}` scheme could not be swept boundedly
   at all: the app would have to enumerate every id it ever issued, and
   discarded/deleted intents forget theirs, so the set is unrecoverable. Worse,
   qBittorrent *persists* categories, and nothing in that scheme ever deletes
   them — a shared client would accumulate one dead category per grab forever.
   A single fixed `plex-manager-intent` category makes the sweep two bounded
   exact-match calls, needs no registry, and creates exactly one extra category
   over the lifetime of the install. Per-intent identity is carried by the hash
   (R3), which is where it belongs. **Category cleanup** is therefore reduced to
   a one-line obligation: the app creates at most the two categories it uses and
   removes neither, so there is nothing to garbage-collect.

   **Named residual — a torrent relabelled out of our namespace is invisible to
   this sweep.** Category-filtered discovery cannot, by construction, see a
   torrent whose category was changed away from ours (C2's cost, paid
   knowingly). Rather than pay whole-inventory polling on every cycle to close
   it, I3 offers an operator-triggered **deep scan**: one unfiltered
   `get_all_statuses()` on demand, matched against intent and tracked hashes,
   presented as observations. Bounded, explicit, and a button rather than a
   background cost (north star 1). If a future adapter offers server-side prefix
   or tag filtering, the deep scan can become cheap enough to schedule.

**Correction UI** (C3, north stars 1 and 2). A "Client-only torrents" surface
lists each observation — hash, name, size, category, why it is unmatched — with
two verbs:

- **Adopt into request** — the operator picks the request (and season); the app
  creates the `Download`, scopes, and coverage claims and recategorizes to
  `plex-manager`. Always operator-directed; **never** title-matched, never
  automatic (C2).
- **Remove (with data)** — offered directly where `client_created` proves this
  app created the torrent. Where creation cannot be proven (a crash lost the
  flag, or the add resolved to a pre-existing torrent), the verb is still
  offered but as an **explicit operator adoption-then-remove**, labelled with
  what the app does and does not know, rather than silently performing a
  destructive action on inferred ownership (C2). A torrent under a foreign
  category is shown observation-only, with the qBittorrent-side action named
  honestly rather than omitted.

A parallel "Needs attention" list carries parked intents with adopt/discard, so
every `needs_attention` residue from Part 1 terminates at a button.

### Interaction with the existing claim machinery

- **Coverage claims (#456).** Key shape unchanged; the semantic widens from "a
  live download covers this season" to "a live download **or** an unresolved
  intent covers this season". `require_no_active_coverage` and
  `_coverage_claim_active` change from an inner join on `downloads` to "owner is
  a non-terminal download **or** owner is a live intent". This single change is
  what closes #477 **for TV**; the movie half needs I1b (next bullet).
- **Read-side guards.** `_active_conflict_for_targets` and `_active_guard_seasons`
  must consult intent-owned claims at **both** the pre-search and the pre-add
  points, not only at the park CAS. Shipping the intent-aware predicate on one
  path only is precisely the hole PR #538 left (lesson L2). Every one of the
  eight `_active_conflict_for_targets` call sites additionally passes
  `exclude_intent_id` — see *Self-exclusion* above; this is the single largest
  mechanical risk in I1 and needs a test per call site, not one shared test.
- **Movies — a real gap, not a sentinel problem.** The claim machinery is
  **TV-only today**, end to end: `_claim_covered_seasons` skips `None` seasons
  (`grab_service.py:718-719`), `find_active_coverage_owner` and
  `find_active_coverage_title` both return `None` for `season is None`
  (`repositories/downloads.py:907-920, 936-950`), and eviction's
  `_coverage_claim_active` returns `False` for anything that is not a
  `_SeasonPending` (`eviction_service.py:399-401`) — the movie eviction
  predicate (`eviction_service.py:1135-1142`) consults only
  `find_active_for_request(..., season=None)` and never looks at claims at all.
  So **no download ever writes a movie claim**, and adding a movie sentinel to
  the unique index in isolation buys only intent-vs-intent collision — which is
  not what #477 is about. Closing the movie half requires the download side to
  write request-level claims *and* the movie eviction guard to positively
  consult intent-owned claims. That is named work, scoped in *Staging* below,
  not a schema detail.
- **ADR-0022 (claim before purge).** That ADR established: any step that can
  still fail on a uniqueness collision must run strictly before irreversible
  external side effects, because a transaction cannot roll those back. `qbt.add`
  **is** such a side effect. This ADR is the completion of ADR-0022's rule one
  layer earlier — *claim before the client mutation, not only before the
  filesystem delete* — not a new principle. C8 applies the same way: ADR-0022's
  step 7 leans on SQLite's single-writer serialization to hold a claim through
  commit, and so does activation's owner swap.
- **PR #524 (merged).** Its distinction between *delete-authorized* and
  *delete-started*, stamped durably at a `before_delete` boundary immediately
  before the destructive worker, is the same shape one layer down. Its
  marker-gated recovery re-checks "active download/coverage" before force-purging
  an intact tree; that recheck must be widened to intent-owned claims, or #524's
  preserved-eligibility guarantee silently excludes in-flight grabs.
- **Issue #526 (closed by PR #568, commit `881b8cbe`).** The per-root
  pressure-exclusion lease landed before this ADR, so the ordering is no longer
  a choice: a pressure-triggered `run_eviction_sweep` now acquires a
  root-scoped lease before its first disk probe and holds it across recovery,
  candidate assembly, every claim, and every delete. **For the TV eviction
  path, I1 acquires the intent claim *inside* that serialization, not
  alongside it** — otherwise the sweep's fresh disk probe and a pre-add commit
  can still interleave, the same snapshot-then-await shape #568 closed for
  correction purges. The sweep's per-candidate claim check
  (`_coverage_claim_active`) runs under the lease; once I1 widens it to
  intent-owned claims, a live intent is active-download-equivalent to a running
  sweep without a second rule. The lease itself samples nothing about claims.
  It has two halves, and both matter here: **acquire**
  (`acquire_pressure_exclusion`) refuses a sweep that is about to start when a
  purge path is already registered under the root, and **revoke**
  (`revoke_pressure_exclusions`) is a latch that defeats every lease already
  held over a path, which the running sweep re-reads before each victim and at
  its `before_delete` boundary. Leases do not exclude one another, and
  registration alone does nothing to a sweep that already holds its lease, so
  covering one half covers only one ordering. A correction covers both in one
  await-free step (`begin_purge` then `revoke_pressure_exclusions`,
  `correction_service.report_issue`). The pre-add
  commit must do the equivalent: either register *and* revoke as a correction
  does (which stands a running sweep down on every pre-add, a cost I1 must
  weigh), or extend the lease so that *lease* acquisition is refused at sweep
  start while a pre-add claim is in flight *and* claim acquisition refuses or
  waits while a lease is held. Which of those I1 chooses is an implementation
  choice; the obligation is that the sweep's per-candidate claim check and the
  pre-add commit never interleave in either order. One TV eviction mode sits
  outside that serialization entirely: a **proactive** sweep (the opt-in
  `eviction_proactive_enabled` setting) takes no lease, because it has no
  pressure reading a correction could invalidate, yet it still runs the same
  per-candidate claim check and delete with suspension points between them.
  Neither option above touches it, so #568 settles the ordering for
  pressure-triggered sweeps only; the proactive gap is the part of Open
  Question 6 that remains open.

### Crash-recovery matrix

| Crash point | Durable state on restart | Client state | Recovery action | Guard during the window |
|---|---|---|---|---|
| Before `prepare_add` | none | untouched | none needed | no grab exists |
| After `prepare_add`, before the intent commit | none | untouched | none needed — `prepare_add` mutates nothing, so a lost prepare loses only computation | no grab exists |
| **After the intent commit, before `add_prepared`** | `prepared` + claims | untouched | probe by hash → proven absent → re-submit if the premise holds, else release the claims and delete | **claimed** — park and eviction refused |
| **Inside `add_prepared` (ambiguous 5xx / proxy / unparsable)** | `prepared` + claims + a live `submitting_since` | unknown | probe by hash; category-verified present → activate; proven absent → re-submit. The intent is released only on *proven rejection*. A crash here strands a **stale lease**, which expires on timeout — safe, because a crashed process has no in-flight call | **claimed**; the lease additionally blocks a concurrent cancel from deleting the row |
| **After `add_prepared` returns, before the activation commit — the #481 window** | `prepared` + claims (+ `client_created`) | torrent exists under `plex-manager-intent` | probe → present + category is one of ours → activate in one transaction (create `Download`, swap claim owners, attach scopes, write `grabbed` history, delete intent) | **claimed** |
| **After the activation commit, before the best-effort recategorize** | `Download` + claims + history + `pending_recategorize` | torrent still under `plex-manager-intent` | the row is tracked and reconciles normally; the reconcile loop retries the recategorize **because `pending_recategorize` is set** — without that durable flag nothing would ever notice, since `DownloadStatus` carries no category and the sweep excludes tracked hashes. This row exists *because* the recategorize was moved out of the transaction | claim owned by the download |
| After the activation commit and the recategorize | `Download` + claims + history | torrent under `plex-manager` | ordinary tracked reconciliation | claim owned by the download |
| Hash mismatch (client resolved a different hash than `prepare_add` derived) | `prepared` + claims | a torrent under our intent category with an unexpected hash | The probe asks about *our* hash, so this is **indistinguishable from "never added"** and the intent re-submits. The re-submit cap bounds the leak and then parks `needs_attention`; the I3 sweep is what actually surfaces the leaked torrents. **Never** a silent adopt | **claimed throughout** — this park class **retains** its claims, because the leaked torrents may still be downloading (see the park taxonomy) |
| Any of the above on a release rolled back to N-1 | `prepared` rows present, unactivatable | as above | N-1 cannot activate. See the honest residual under *Consequences* and Open Question 18 | see residual |

### Staging

Three increments separated by **release boundaries**, each independently
valuable, independently observable on the canary, and independently revertible.
This staging is itself a lesson from #538 (L1).

- **I1 — the claim, and the owner swap.** Claim-owner columns, the intent table
  with a nullable hash, the pre-add commit, **the owner-swap activation as the
  normal post-add success path**, intent-aware guard predicates at every read
  site, and `exclude_intent_id` threaded through all eight
  `_active_conflict_for_targets` call sites. **Closes #477 for TV.** Ships and
  soaks alone. This is option (b) expressed as (a)'s first layer, and it is the
  honest **stopping point** if I2/I3 prove too heavy: a complete answer to
  #477's TV half and a documented, visible, partial answer to #481.

  **The owner swap is not deferrable to I2.** A first cut of this ADR put
  activation in I2 and left I1 as "claim only". That does not ship: the grab
  would collide with its *own* intent-owned claim — `ensure_coverage_claim`
  inserting against it, the `IntegrityError` handler reading it as foreign, and
  `_remove_torrent_if_added` deleting the torrent the grab had just added (see
  *Self-exclusion*). Whatever creates a claim before the add must, in the same
  increment, know how to take ownership of it afterwards. What I2 defers is the
  *hash* and the *client-facing recovery*, not the swap.
- **I1b — the movie half of #477** (small, sequenced with or just after I1):
  request-level claims written on the download side for movies, and the movie
  eviction predicate widened to consult claims (both download- and
  intent-owned). Tracked separately because it is a genuine behavior change to a
  path that has never had claims, not a widening of an existing one.
- **I2 — the port split and activation.** `prepare_add` / `add_prepared`, the
  hash becomes `NOT NULL`, intent recovery and activation. Closes the #481 crash
  window for torrents the app itself submitted.
- **I3 — the correction surface.** The category-scoped client-only sweep, the
  observation table, and the adopt/remove UI. Closes #481's north-star-1
  obligation for anything the app cannot re-derive, and gives every I2
  `needs_attention` residue a button.

## What PR #538 taught

[PR #538](https://github.com/LunchBox951/Plex-Management/pull/538) was a full
implementation attempt of this design, closed by its author as unsalvageable.
**Its review record does not invalidate the shape of the design — it invalidates
the way the shape was delivered, and it names, concretely, the traps.** Each
lesson below is a rule the next attempt must satisfy, with the evidence that
produced it.

**L1 — The staged "inert substrate" did not stay inert.** The PR opened as
"Part 1 of 3 … inert until activated by the next PR in the stack —
`grab_service` stays on the legacy direct-add path" and closed at **+7,438 /
−712 across 56 files**, with four migrations and changes to `correction_service`,
`request_service`, `queue_service`, `auto_grab_service`, `eviction_service`, the
web routers, the OpenAPI contract, and `TitleDetailModal.tsx`. A substrate that
nothing calls cannot be proven correct; every attempt to prove it pulled its
callers in. → *Increments must be separated by a release boundary, or they are
one PR. Never claim inertness for a layer whose only proof of correctness is its
callers.* This is why the staging above is release-separated and why I1 is a
complete answer to a real issue rather than a substrate.

**L2 — Two claim namespaces with no cross-table constraint is a defect
generator.** Codex P1 *"Exclude intent-owned scopes from new grabs"*: an intent
scope and a `Download` could both own a season because nothing in the database
forbade it, and the intent-aware predicate had been added to the park CAS but
not the accepted-release grab path — so auto-grab could add a second torrent for
a claimed scope. Every guard site is a place to forget. → **R1**: one claim
namespace with two possible owners, so the collision is a database fact rather
than a discipline.

**L3 — Ownership tokens must be released together and completely.** Two
mirror-image findings: P2 *"Exclude parked intent scopes from active guards"* —
parking cleared `active_scope_key` but retained `scope_key`, which the `EXISTS`
still tested unconditionally, so a parked incident **permanently** blocked
parking and eviction for that title; and P1 *"Do not treat parked hash history as
an active grab"* — a parked row still owned the globally unique `torrent_hash`,
so `try_create()` returned `None`, auto-grab read the resulting
`AlreadyDownloadingError` as an active disposition, stopped trying lower-ranked
releases, cleared its backoff, and looped forever. → *A parked intent is inert in
**every** guard and visible in the UI. Columns retained for history must never be
load-bearing in a predicate.*

**L4 — A per-item recovery failure is not a client-wide failure.** P1 *"Isolate
unrecoverable source errors per intent"*: a `QbittorrentSourceError` from an
expired one-shot HTTP source during recovery was classified as a qBittorrent
outage, so `_reconcile_once_leased` skipped queue reconciliation **and imports**
on every subsequent cycle while the intent stayed recoverable. P1 *"Park stale
intent premises instead of aborting reconciliation"*: a bare `RuntimeError` on a
lost premise CAS bypassed the conflict handler and aborted every cycle. Both
were amplified by recovery running **before** reconciliation in `web/app.py`, so
one poisoned intent halted the whole pipeline. → **R4**: per-item isolation, each
committing independently, positioned so recovery cannot gate reconciliation.

**L5 — "Recoverable forever" is a loop, not a safety property.** P2 *"Avoid
reporting unchanged attention rows as mutations"*: unresolved intents were
reprocessed every ~15 s, rewrote identical state, counted as `changed`, and
emitted realtime invalidations perpetually. → *Every non-progressing intent needs
a backoff schedule (`next_attempt_at`), a "mutation" must mean an actual state
transition, and a permanently unresolved intent is a UI item, not a retry item.*

**L6 — Ownership must be proven before touching a shared client's torrent.** P1
*"Verify the intent category before adopting a present torrent"*: finalizing
purely by hash could adopt — and a later cancellation could then remove, with
data — a pre-existing torrent in a shared qBittorrent that this app never
created. → **C2** and **R3**. Note this ADR does *not* adopt #538's answer to
the finding: #538 made the per-intent **category** the proof token, but a
category is mutable and therefore not tamper-evident, and a per-id scheme is
also unsweepable and unbounded (Part 3). The token here is the locally-derived
hash plus the recorded `client_created` flag, tiered by what each authorizes;
the category is a filter and a hint. A foreign-category hash is still parked,
never adopted.

**L7 — A command that has committed must not fail, and must not overstate.** P2
*"Do not fail cancellation after committing it"*: cancellation committed the
request, seasons, intent state, and history, then re-raised on a transient
client error — returning `qbittorrent_unavailable` for an operation that had
*succeeded*, and un-retryable because the request was already `cancelled`. The
mirror image recurred twice across review rounds as *"Report deferred cleanup
when qBittorrent is unconfigured"*: with `qbt is None` the cancel committed
`cancel_requested`, skipped cleanup, and reported unqualified success with
`cleanup_deferred` false, even though an owned torrent might still be running.
→ *Post-commit cleanup is best-effort and reported: success plus an explicit,
surfaced deferred flag. Never fail a committed command; never claim cleanup you
did not do.*

**L8 — Reservation retirement is structural, not per-branch.** P1 *"Retire
reservations after losing post-add races"* was raised, addressed, and then raised
**again** for the secondary pack-target path, where a non-initiating target
becoming available or cancelled during `add_prepared` rolled back and removed
the torrent but left the separately-committed intent `prepared` — so the next
recovery re-submitted the unwanted torrent before rediscovering the stale
premise. That the same class recurred *after* a targeted fix is the signal: the
branch-by-branch structure was wrong. → *Structure the activation transaction so
every exit path — including secondary-target CAS losses — passes through one
retirement point.*

**L9 — Cancellation racing recovery is a first-class case, both ways.** P1
*"Recheck the successor when an intent vanishes during cancel"*: recovery
converted an intent into a `Download` between the cancel's read and its CAS; the
cancel treated the vanished intent as success and committed `cancelled` without
terminalizing or removing the new torrent, so cancelled content kept downloading
and could import. P1 *"Prevent recovery from submitting after cancellation"*: the
reverse — cancel committed `cancel_requested` while recovery awaited
`prepare_add`, and recovery submitted anyway from the stale record. → *The intent
row is the single serialization point; cancel and recovery both CAS on it, and a
losing CAS must re-read the **successor** (download or intent) rather than treat
a vanished row as done.*

**L10 — The blast radius reaches unrelated invariants; budget for it.** P1
*"Derive TV intent scopes from the selected release"* — the intent was built from
the request's stored episode filters rather than the release's `target_seasons`
/ `covered_seasons`, so recovery created false coverage claims that blocked
legitimate grabs, or omitted real targets so seasons stayed pending. P1
*"Correlate active downloads to the current show"* — an uncorrelated `EXISTS`
meant any show's season-1 download blocked parking and eviction for season 1 of
**every** show. P1 *"Lock a same-hash download before attaching recovered
scopes"* — a snapshot status check let scopes and claims be reactivated on a row
the importer had just terminalized. P2 *"Check known hashes before resolving the
source"* — an idempotent re-grab of an already-tracked torrent now paid an HTTP
source timeout before the cheap same-hash check. Plus two P1 log-safety
violations (`safe_int` / `safe_text` bypassed, AGENTS.md §Security And Logging).
→ *This path touches the most heavily-invariant code in the repository. Assume
every read-side predicate near it needs re-derivation, not extension.*

**L11 — The proximate cause of closure was process.** The author closed the PR
with *"Mistakingly left codex on a goal. This PR is fucked."* An automated
reviewer left running against a moving branch across 12 commits produced
overlapping findings re-litigated on superseded code, and the branch became
unreviewable regardless of the merit of any individual finding. → *Land against
a frozen commit, in increments small enough to review once, and drive automated
review deliberately rather than continuously.*

## Consequences

- **The grab path gains a commit before the client call.** On ADR-0007's
  single-writer SQLite that is a real cost, paid on every grab attempt including
  ones that end in rejection. The write is small and the grab path is not hot,
  but it is not free and should be measured on the canary during I1.
- **Coverage-claim semantics widen.** "A live download covers this season"
  becomes "a live download **or** an unresolved intent covers this season".
  Every reader of `download_coverage_claims` must be audited — not only the two
  named in #477 — and #524's marker-gated recovery recheck is one of them.
- **A grab can now conflict with itself, and the codebase must say so
  everywhere.** Self-exclusion by `exclude_intent_id` is a new, permanent
  obligation on eight call sites; forgetting it at any one of them does not fail
  loudly but *deletes a torrent the grab just added*. This is the single
  highest-risk mechanical change in the design and the reason I1 is sized
  Medium rather than Small.
- **#477's movie half is not closed by I1.** The claim machinery is TV-only
  today at every layer; movies need I1b. Until that lands, the ADR's claim is
  explicitly "#477 for TV", and the movie window remains open and documented.
- **The shipped correction verbs gain an ownership gate.** `downloads` acquires
  `client_created`, and ADR-0014's report-issue purge and the cancel verb —
  which today remove with data unconditionally — become conditional on it, with
  operator adoption as the escape hatch. This is a behavior change to
  already-shipped, well-tested paths, and it is the part of this design most
  likely to surprise: a rollback that reintroduces unconditional removal, or a
  migration that defaults existing rows to `false` instead of NULL, silently
  changes what those buttons do. The gate is **staged by value** — `false`
  enforced from I2, NULL warn-only until I3's confirmation path exists — so
  landing I2 alone never strands an operator without a way to cancel or report
  an existing download.
- **Category tampering is an accepted, named residual.** Because a category is
  mutable, a relabelled-out torrent is invisible to the filtered sweep and a
  relabelled-in torrent is refused rather than adopted. The tiered ownership
  model bounds the *consequence* (no destructive action on unproven ownership),
  and the on-demand deep scan gives the operator a way to find the former. The
  design does not claim to prevent tampering — only to fail safe under it.
- **An invisible client-only torrent is traded for a visible durable row.** That
  is the point (north star 3, honesty over silence), but it makes the correction
  surface load-bearing: **I3 is not optional polish.** Until it ships, every
  `needs_attention` intent is a row an operator can see and a torrent they still
  cannot act on from the app.
- **ADR-0006's port contract widens** to a two-phase add every future
  download-client adapter must reckon with. An adapter that cannot derive a hash
  before submitting keeps the compatibility `add()` and honestly retains today's
  window rather than claiming a guarantee it cannot make.
- **A new secret-bearing column.** The encrypted `source_ref` inherits ADR-0026's
  posture and its rotation obligation.
- **N/N-1 rollback residual, stated honestly (C5).** The migrations are
  expand-only, and N-1 is not corrupted by anything a newer release writes — but
  N-1 cannot *activate* an intent. Rolling back from I1 to the release before it
  leaves claim rows whose `download_id` is NULL; the older
  `require_no_active_coverage` predicate inner-joins `downloads` on that column,
  so those claims are simply invisible and the guard reverts to today's
  behavior. Nothing is corrupted, but the protection silently lapses. Rolling
  back from I2 additionally leaves `prepared` intents that the older code will
  never activate and whose client torrents it cannot see — which is exactly the
  #481 state, restored. That lapse is *not* acceptable under ADR-0024 for a
  release published to an updater-consumed moving tag: N-1 must keep providing
  its supported behavior against the migrated database, and the guard *is*
  supported behavior. Release-note disclosure does not satisfy the rule.
  **Open Question 18 records the resulting constraint and is I1's release
  gate**: the migration may not reach an updater-consumed tag until an
  N-1-compatible guard or an expand/contract sequence exists.
- **The testing burden is the crash matrix.** Every row above is a required
  regression test, plus: the cancel↔recovery race in both directions (L9); the
  parked-intent-is-inert property across *every* guard (L3); the
  foreign-category refusal (L6); per-item isolation, i.e. one poisoned intent
  must not stop a cycle (L4); no-perpetual-mutation under a permanently
  unresolved intent (L5); single-retirement-point coverage including secondary
  pack targets (L8); the ambiguous-`add_prepared`-response retention rule;
  **self-exclusion at each of the eight call sites individually** (a shared test
  would pass while one site was still wrong); the crash-between-commit-and-
  recategorize row; the parked-intent supersession rule under a duplicate-add
  `created=False`, **and its refusal to fire on a foreign-category park**;
  `client_created` surviving activation and gating report-issue and cancel, with
  NULL treated as ask-not-skip; the partial hash-uniqueness index permitting a
  parked row and its successor to coexist; the re-submit cap parking instead of
  leaking under a hash mismatch; **a cancel racing an in-flight `add_prepared`,
  which must not delete the intent while the lease is live** (and must proceed
  once it expires); a retained-claim park keeping its coverage until its torrent
  is removed, adopted, or proven absent; and `pending_recategorize` surviving a
  crash and being retried to a confirmed category. Added by the final review
  round: an externally relabelled `client_created=True` torrent **retaining**
  its claims through a `foreign_category` park; a delete-and-re-add of the same
  hash in a later second failing the instance-marker comparison before a
  destructive removal, and a same-second re-add never being removed on the
  marker alone (Open Question 17 decides whether it fails a stronger marker or
  degrades to the operator-gated path);
  activation converging on a same-hash `Download` created by another intent
  rather than retrying `create()`; supersession **transferring** retained claims
  in one transaction rather than cascading them away; discard refused on a
  claim-retaining park until the operator resolves the torrent; an ambiguous
  `add_prepared` outcome **holding** the lease against an immediate
  absent-probe; and the staged gate leaving NULL rows removable during I2.

## Implementation scope

- **I1 (claim before the add, plus the owner swap): Medium.** One migration
  (claim-owner columns + two tables), the pre-add commit, the owner-swap
  activation on the post-add success path, the widened predicates at every read
  site, `exclude_intent_id` at all eight `_active_conflict_for_targets` call
  sites, the submission lease, `pending_recategorize` and its retry, the
  stale-claim reaper, and the guard tests. No port change, no frontend.
  Independently shippable and soakable, subject to Open Question 18 before
  the migration reaches an updater-consumed tag. Sized above the first
  estimate deliberately: the self-exclusion threading is mechanical but
  unforgiving, and the owner swap is real logic on the hottest error-handling
  path in the file.
- **I1b (movie claims): Small.** Request-level claim writes on the download
  side and a movie eviction predicate that consults them.
- **I2 (port split + client-facing recovery): Large.** The port contract change
  and its adapter work (including the v2-magnet route, persisting
  `client_created`, and adding `category` + `added_on` to `DownloadStatus`),
  intent recovery with per-item isolation and backoff, the park-liveness
  taxonomy, same-hash activation convergence, claim transfer on supersession,
  the staged (warn-only-on-NULL) destructive gate, the
  hash becoming `NOT NULL`, the single retirement point, the parked-intent
  supersession rule, and the cancel↔recovery serialization. This is where every
  #538 P1 lived; it deserves its own review window against a frozen commit.
- **I3 (correction surface): Medium.** The two exact-match sweeps and their
  cadence, the on-demand deep scan, the observation table, the adopt/remove
  verbs with their tiered ownership proofs, **the confirmation path that
  promotes the NULL gate from warn-only to enforced**, the resolve-before-discard
  flow for claim-retaining parks, the OpenAPI/client regeneration, and the
  frontend surface.

**Recommendation:** accept this ADR as `Proposed`, land **I1** (+ **I1b**) first
as a self-contained fix for #477, and let it soak on `:edge` before committing
to I2/I3. If I2/I3 are not funded, record #481's residual explicitly against
I1's documented gap rather than leaving it implied.

## Open implementation questions

Deliberately left to the implementing changes (to be settled in the `Accepted`
revision or its PRs), because they do not change the option choice:

1. **Movie claim mechanics (I1b).** The requirement is fixed — the movie path
   must positively *write* request-level claims and positively *consult*
   intent-owned claims in its eviction predicate; a unique-index sentinel alone
   changes nothing. Open: the discriminator's form (reserved sentinel
   `season_number` vs. an expression index over `COALESCE(season_number, -1)`,
   given NULL-distinctness in SQLite and Postgres unique indexes), and whether
   the movie guard reuses `_coverage_claim_active` (retiring its
   "movies are honestly `False`" early return) or gets its own predicate.
2. **Where intent recovery runs relative to `_reconcile_once_leased`** and the
   maintenance lease. L4 fixes the constraint (recovery must not gate
   reconciliation); the exact placement interacts with the loop's lease.
3. **Cadence and bound of the client-only sweep**, whether `client_only_torrents`
   is a persisted table or a computed view, and whether an operator dismissal
   persists. Also: whether the on-demand deep scan needs a rate limit on a
   client with a very large inventory.
4. **v2-only magnet handling in `prepare_add`** — derive from `btmh`, or accept
   the indexer-supplied `infoHash`.
5. **Backoff schedule shape** for `needs_attention` / `next_attempt_at`, and
   whether it reuses the auto-grab cooldown machinery.
6. **Ordering against #526's per-root lease** — *resolved for
   pressure-triggered sweeps by PR #568 (commit `881b8cbe`)*: the lease landed
   first, so I1 acquires the intent claim inside the per-root serialization for
   the TV eviction path (see *Interaction with the existing claim machinery*).
   *Still open:* a proactive sweep takes no lease, so I1 must either bring
   proactive sweeps under the lease or key the pre-add exclusion on something
   a proactive sweep does consult. Which is I1's to decide.
7. **I1's stale-claim reaper predicate** — age-based, or tied to a process-start
   generation marker.
8. **Whether `client_only_torrents` observations are also the storage for I2's
   `needs_attention` surface**, or a separate list with a shared component.
9. **The two lease durations.** The ordinary `submitting_since` timeout must
   exceed the adapter's add timeout; the *extended* expiry after an ambiguous
   outcome must exceed the plausible upstream-settling time of an interposed
   proxy, which the app cannot measure. Both are safety-vs-latency trades on
   cancellation responsiveness, and both should be configurable rather than
   guessed once. Whether the ambiguous case warrants a distinct state
   (`submitting_ambiguous`) or just a longer expiry on the same column is an
   implementation choice; the constraint is that an ambiguous outcome must never
   clear the lease immediately.
10. **Instance-marker portability.** `added_on` is the qBittorrent-specific
   spelling of "this torrent instance". The *requirement* is fixed — a
   destructive removal must re-prove instance identity, not just hash identity —
   but a future download-client adapter may expose no equivalent. What such an
   adapter does (degrade to operator-gated removal, or synthesize a marker at
   add time) is left open, and is the kind of thing ADR-0006's port contract
   should state explicitly when a second adapter actually arrives.

### Open questions raised in review

This ADR went through seven substantive review rounds before it merged as
Proposed, and an eighth after the merge (recorded as questions 17–18 plus the
text corrections for #568, `added_on`, and the intent-scope key). Each new
mechanism kept surfacing further edge cases. That is not a sign the design is
wrong; it is a measurement of how large the surface is — which is itself an
argument for the release-separated staging, since each increment gets its own
implementation review against real code rather than against a document. The
following constraints were raised late and are recorded here as obligations on
the implementing changes rather than resolved with more speculative machinery.
Each states what must be true; where the direction is obvious it is named in
one sentence.

11. **I1's stale-claim reaper cannot distinguish crash-after-add from
    crash-before-submission.** In I1 the intent has no hash, so nothing lets the
    reaper ask the client whether a torrent exists. **Constraint:** reaping must
    never free coverage without positive evidence that no add occurred —
    silently freeing a claim whose torrent is live is precisely the #477 gap.
    *Direction:* the `submitting_since` lease is the only local evidence
    available; a claim whose lease was never stamped can be reaped safely, while
    one stamped-then-orphaned should be surfaced rather than silently freed.
    This may be what forces the hash earlier than I2 — which is a legitimate
    outcome for I1 to discover.
12. **Intent creation must revalidate its premise atomically.** A cancellation
    (or any status write) can commit between the grab's premise read and the
    intent commit, so an intent can be born already stale, holding claims for a
    request nobody wants. **Constraint:** the intent commit must be
    compare-and-swap against the status it observed, not an unconditional
    insert. *Direction:* mirror the CAS `grab()` already performs on its post-add
    status move, using the `observed_*_status` columns this ADR already defines.
13. **I2's attention states have no inspection surface until I3.** I2 can park
    intents `needs_attention`, but the list, adopt, and discard verbs are I3
    work. **Constraint:** the staging table must either pull a minimal I2
    observation surface forward or state plainly that parks are log-only for one
    increment. This is a real north-star-1 tension — a state the operator cannot
    see or act on is exactly what north star 1 forbids — and it must be accepted
    explicitly, not by omission. *Direction:* the same staging precedent used for
    the destructive gate (ship the honest surface first, enforce later) applies
    here.
14. **A `foreign_category` park is unsupersedable, which can loop candidate
    selection.** Such a park releases its hash's uniqueness reservation but
    refuses adoption by design, so the next grab of the same release resolves the
    same hash, re-adds, and parks again indefinitely. **Constraint:** a parked
    foreign hash must feed release-candidate selection as an exclusion, so the
    selector moves to another candidate instead of retrying the same one
    forever. *Direction:* the blocklist is the existing mechanism of this shape;
    whether this reuses it or needs a distinct, non-punitive exclusion (the
    release is not bad, it is merely unavailable to us) is the open part.
15. **Retained claims are keyed to a `media_request_id` that may settle or be
    replaced.** `download_coverage_claims.media_request_id` is `ON DELETE SET
    NULL`, and a cancelled or settled request can be replaced by a new active row
    for the same title, against which the old claim no longer protects anything.
    **Constraint:** a retained claim's protective scope must survive replacement
    of the request row it was created under. *Direction:* `find_active_coverage_title`
    (#470) already solved exactly this for downloads by protecting on the
    `(tmdb_id, season)` title key rather than the request id; the intent-owned
    case should follow it rather than invent a second rule.
16. **A caller-supplied `infoHash` is untrusted metadata.** Part 2's obligation 4
    permits Prowlarr's `infoHash` to satisfy `prepare_add` so v2/hybrid magnets
    stay grabbable, but an indexer-supplied hash is not proof of what the client
    actually holds. **Constraint:** the caller-supplied hash may be used to
    *submit*, but only the hash qBittorrent itself reports may be used to probe,
    adopt, activate, or destroy. Identity for every ownership decision comes from
    the client's own report, never from indexer metadata. *Direction:* this needs
    reconciling with obligation 4's non-empty-hash requirement — most likely by
    distinguishing a *provisional* hash used for submission from a *confirmed*
    hash written back once the client reports it.
17. **`added_on` cannot distinguish instances within one second.** The
    instance marker in the *Ownership model* is qBittorrent's `added_on`, which
    the adapter documents as Unix-epoch seconds. A delete-and-re-add completing
    within the same second yields an identical marker, so the comparison would
    accept the replacement as the original app-created instance and authorize
    remove-with-data against it. **Constraint:** the instance check may
    authorize a destructive removal only when the persisted marker can actually
    distinguish the present instance from the one activation observed; where it
    cannot, the removal must be operator-gated, never silent. *Direction:* a
    stronger marker (further client-reported instance evidence persisted at
    activation, or one the app stamps on the torrent at add time and verifies
    before removal), or an explicit degrade to the operator-gated path when the
    only available marker is second-resolution. This is the resolution half of
    question 10's portability concern; the two should be settled together.
18. **The N-1 rollback residual violates ADR-0024 on updater-consumed tags.**
    *Consequences* records that after a rollback from I1, claim rows whose
    `download_id` is NULL are invisible to the older inner-join guard, so N-1
    can admit a replacement grab or evict against coverage that is in fact
    claimed. [ADR-0024](0024-first-party-container-auto-updater.md) requires
    that N-1 "still be able to start and provide its supported behavior
    against the resulting database" for every release eligible for automatic
    update; the coverage guard is supported behavior, and a release note does
    not restore it. **Constraint (I1's release gate):** I1's migration, and
    I2's after it, may not ship to an updater-consumed moving tag until either
    an N-1-compatible guard exists or the change is sequenced as
    expand/contract across releases. *Direction:* ship the expand migration
    together with the intent-aware guard predicate one release *ahead* of the
    release that first writes intent-owned claims, so the rollback target
    already reads them. ADR-0024's own fallback — stay off moving tags and ship
    with documented pinned/manual upgrade instructions — also satisfies the
    rule, but `:edge` is itself a moving tag the canary fleet auto-pulls, so
    that fallback forfeits the soak the staging plan depends on. Which route
    the maintainer takes is a release decision, not a design one, and belongs
    in the acceptance revision.

## Alternatives considered

Summarized above and rejected as complete answers: **(b)** the narrow
placeholder claim row — closes #477 but cannot touch #481, and is adopted as
increment I1 rather than as a fork; **(c)** a category-scoped sweep with
adopt/remove UI alone — closes #481's visibility half only, leaves #477 open,
and rests removal on inferred ownership (C2), with its sweep adopted as I3;
**(d)** status quo plus telemetry — there is nothing to instrument in the #481
case, and monitoring is not a correction path (north star 1); **(e)** a global
grab lock — closes #477 by construction but does nothing for #481, caps
auto-grab throughput, and is not durable across the restart the issue is about.
