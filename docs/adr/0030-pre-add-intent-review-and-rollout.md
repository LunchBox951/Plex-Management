# ADR-0030: Pre-add intent review constraints and compatible rollout

- **Status:** Proposed
- **Date:** 2026-09-09
- **Proposes to supersede in part:** [ADR-0029](0029-durable-pre-add-intent.md), specifically the instance-identity rule in *Ownership model*, the `download_add_intent_scopes` definition, the #526 interaction and open question 6, the N/N-1 crash-matrix row and consequence, and *Staging* and *Implementation scope*, including their recommendation.
- **Resolves:** [issue #571](https://github.com/LunchBox951/Plex-Management/issues/571), the post-merge review of [PR #565](https://github.com/LunchBox951/Plex-Management/pull/565), and the follow-up findings on [PR #588](https://github.com/LunchBox951/Plex-Management/pull/588).
- **Preserves:** ADR-0029's durable-intent option, shared claim namespace, owner swap, recovery and correction design, and other open questions. Both records remain Proposed; merging this document does not accept either proposal or authorize an implementation release.

## Context

ADR-0029 merged as Proposed before its eighth review round arrived. That round identified four gaps: `added_on` cannot distinguish a same-second torrent replacement; #526's pressure-exclusion lease had already landed; the proposed rollback residual violates ADR-0024 on updater-consumed tags; and the intent-scope table names uniqueness without defining a key.

PR #588 initially amended ADR-0029 directly. Its review also found that the new compatibility gate contradicted the unchanged recommendation to ship the migration, guard and first intent writer together in I1 and soak that release on `:edge`. This record carries the substantive revisions separately, as the repository's superseding-ADR workflow requires. ADR-0029 retains its original proposal with a historical reference to this record.

The sections below replace the corresponding guidance listed in this record's header when evaluating the revised proposal. In particular, ADR-0029's original I1-first recommendation and its release-note-only rollback treatment are not the rollout plan for this revision.

## Decision (proposed)

### Instance identity: `added_on` alone cannot authorize removal (OQ17)

qBittorrent's `added_on` has Unix-epoch seconds resolution, as documented in [the adapter](../../src/plex_manager/adapters/qbittorrent/adapter.py). A torrent re-added in a later second has a different marker. A delete-and-re-add within the same second can retain the same hash and timestamp, so matching `added_on` and a historical `client_created=True` does not prove that the present torrent is the app-created instance.

Activation must persist instance evidence that can distinguish the present instance from the one it observed. Where that evidence is unavailable, destructive removal must use the explicit operator confirmation/adoption path; second-resolution identity alone must never authorize automatic removal. This applies to report-issue, eviction cleanup and `cancel_requested` recovery. Observing proven absence still invalidates the ownership flag, but absence polling cannot detect a replacement that occurs between polls.

The stronger marker remains an implementation question, paired with ADR-0029's open question 10 about client portability. Possible evidence includes a stronger client-reported identity or an app-stamped marker whose replacement semantics are verified. Neither is assumed to exist merely because this ADR names it. If an increment needs operator confirmation to satisfy the rule, that confirmation path must ship with the increment; it cannot rely on I3's future UI.

### Intent-scope identity

The revised `download_add_intent_scopes` definition is:

| Column | Contract |
|---|---|
| `id` | Primary key. |
| `intent_id` | Non-null foreign key to `download_add_intents`, `ON DELETE CASCADE`. |
| `media_request_id`, `season_number` | Non-null scope identity. Movie representation remains subject to ADR-0029's I1b discriminator decision. |
| `role` | `target` (will be imported) or `covered` (physical ride-along). |
| `episodes_json` | Episode filter for `target` rows only. |

Define a unique key on `(intent_id, media_request_id, season_number)`: one row per season per intent. The non-null columns prevent NULL-distinctness from bypassing that key. Retried construction and supersession that reattach the same scope must upsert the authoritative role and episode filter rather than append another row. A scope cannot acquire both a target row and a covered row for the same intent and season.

This is a proposed schema contract. Its Alembic migration belongs to I0 below; this documentation PR changes no database schema.

### Eviction ordering after #568 (revises OQ6)

[PR #568](https://github.com/LunchBox951/Plex-Management/pull/568) closed [issue #526](https://github.com/LunchBox951/Plex-Management/issues/526) in commit `881b8cbe`. The ordering is established: the pressure-exclusion lease landed before pre-add intents. ADR-0029's header, C7 and open question 6 must be read with that historical correction.

A pressure-triggered `run_eviction_sweep` acquires a root-scoped lease before the first disk probe and holds it across recovery, candidate assembly, claims and deletes. The per-candidate `_coverage_claim_active` check runs under that lease. The lease itself does not sample claims and leases do not exclude one another. Its two mechanisms are:

- `acquire_pressure_exclusion` refuses a new sweep when a purge path is already registered under its root.
- `revoke_pressure_exclusions` defeats leases already held over a path. The sweep checks the revocation before each victim and at the `before_delete` boundary, including after the marker-arm commit.

`correction_service.report_issue` covers both orderings in one await-free step: `begin_purge` followed by `revoke_pressure_exclusions`. Registration alone does not stop an existing lease. See [purge_service.py](../../src/plex_manager/services/purge_service.py), [correction_service.py](../../src/plex_manager/services/correction_service.py) and [eviction_service.py](../../src/plex_manager/services/eviction_service.py).

I1's pre-add claim commit must participate in the same exclusion. It can register and revoke as a correction does, or extend the protocol so sweep acquisition refuses an in-flight claim and claim acquisition refuses or waits for a held sweep lease. The choice must protect both orderings through the destructive boundary, including a delete worker that has already started; revoking a lease cannot undo a running filesystem operation. The widened claim predicate is necessary but does not close the suspension window on its own.

Proactive sweeps (`eviction_proactive_enabled`) take no pressure lease and still have suspension points between the claim check and deletion. They therefore need their own exclusion integration before I1 can claim to close #477 for TV. Whether I1 extends the lease to proactive sweeps or uses an exclusion they already consult remains open. #568 resolves which implementation landed first, not this remaining obligation.

### Rollback is a release gate (OQ18)

[ADR-0024](0024-first-party-container-auto-updater.md) requires the retained N-1 application to provide its supported behaviour against the migrated database. N-1 starts without running its older Alembic graph; it does not restore the previous database. Expand-only migrations are therefore insufficient if the old application misreads rows written by the new one.

The current coverage queries in [repositories/downloads.py](../../src/plex_manager/repositories/downloads.py) inner-join `downloads` on `download_id`. They cannot see intent-owned claims whose `download_id` is NULL. Rolling back a first writer to that application would allow replacement grabs or eviction against live coverage. Release notes cannot make that behaviour compatible.

The revised rollout requires a no-write compatibility increment, **I0**, before I1. I0 installs the expanded schema and the intent-aware guard behaviour; I1 first creates intent-owned claims. The exact retained image must support the claim states and all relevant readers and mutations that the writer can leave behind. Merely merging I0 one commit earlier, or publishing an image that a deployment skips, does not establish that condition.

For each updater-consumed channel, I1 is eligible only when the retained rollback image contains that compatibility support. A deployment that skipped I0 must receive a compatible intermediate image first, or use the documented pinned/manual upgrade path. If the automatic update path cannot enforce that prerequisite, the first writer must stay off its moving tag. The `:edge` canary is subject to this rule just as `:stable` is.

I1b and I2 must pass the same gate against their own retained predecessor. In particular, I0's TV guard does not establish movie compatibility, and I2 must not make `torrent_hash` non-null while its retained I1 predecessor can still write NULL. Defer that contraction until the rollback horizon permits it, or first ship a predecessor that no longer writes NULL. This replaces ADR-0029's unconditional assignment of that narrowing to I2.

### Staging and implementation scope (replacement)

Each increment has a distinct release boundary and its own compatibility proof. These are release prerequisites, not just PR ordering.

| Increment | Work and release condition |
|---|---|
| **I0 — compatibility without intent writes** | Move the claim-owner expansion, intent and scope tables, and intent-aware guard predicates out of I1 into this release. Retain the legacy direct-add path. No production path creates intent rows or intent-owned claims; no intent activation, replay or reaper is enabled. Guard and conflict handling must conservatively preserve live intent-owned coverage when running as I1's rollback target, including park, grab, eviction and marker recovery. Ship and soak I0 before enabling a writer. |
| **I1 — TV claim and owner swap** | After the retained-image gate is satisfied, enable the pre-add commit, owner-swap activation, self-exclusion at every conflict call site, submission lease and the supporting lifecycle behaviour from ADR-0029's I1 scope. Complete both pressure and proactive exclusion. The expanded schema and guard support already exist in I0. Closes #477 for TV; remains a partial answer to #481. Ship and soak separately from I0. |
| **I1b — movie claims** | Add request-level movie claims and their eviction guard. Ship the necessary movie schema/read compatibility in a preceding release before enabling movie writes. I1b may accompany I1 only if its retained I0 predecessor already supports that representation and behaviour; otherwise stage it later. |
| **I2 — port split and client-facing recovery** | Add `prepare_add` / `add_prepared`, hash-based recovery, ownership evidence and the other I2 behaviour in ADR-0029. Prove N-1 compatibility for the new rows and destructive gates before publication; stage any prerequisite support first. Keep the hash nullable for as long as N-1 can write NULL. Meet OQ17 when introducing instance-gated removal. |
| **I3 — correction surface** | Add the client-only sweeps, observations and adopt/remove UI from ADR-0029. Preserve the rollback contract for any additional schema or state changes. A confirmation path needed by an earlier increment must already have shipped there. |

I0 does not close #477: it prepares the safe rollback target without adding pre-add claims. I1 remains the TV fix and still requires its owner swap in the same increment as the first claim writer. This split does not repeat PR #538's expansion of an inert substrate into a full implementation: I0's production write prohibition is an explicit boundary to verify.

**Recommendation:** after acceptance, ship and soak **I0** first. Verify that the retained rollback image for the intended channel contains its compatibility support, then ship and soak **I1** on `:edge`. Add **I1b** only after its movie compatibility prerequisite is satisfied. Decide on I2/I3 after that soak, retaining #481's documented residual if work stops at I1. Do not use ADR-0029's original I1-first recommendation to skip I0 or its deployment prerequisite.

### Rollback matrix (replacement for the N-1 row)

| Transition | Required state and supported behaviour |
|---|---|
| I0 → pre-I0 | Expanded schema only; I0 has written no intents or intent-owned claims. Prove that the old application's existing operations still work against that schema. |
| I1 → compatible I0 | Prepared intents and NULL-`download_id` claims can remain. I0 need not activate an intent, but must preserve its coverage across every relevant guard and mutation and must not silently reap an unfamiliar intent state. This protection is required until forward recovery or explicit supported resolution. |
| I1 → pre-I0 | Not an eligible automatic-update transition. The rollback application cannot see intent-owned coverage. A release note does not clear this gate. |
| I1b or I2 → predecessor | Prove compatibility with the exact predecessor and all states/schema that the new increment can leave. The TV proof alone does not establish movie or I2 compatibility. |

## Verification required for implementation

These are acceptance checks for the future implementation increments, not tests claimed to run in this documentation PR:

- Exercise I0's normal grab and background paths and prove they create no intents or intent-owned claims. Apply I0's migration and run pre-I0's supported operations against the result.
- Create representative I1 intent-owned claims, including active and retained-claim parked states, with I1; then start the actual retained I0 application against those database bytes through the rollback path. Verify conflicting grabs, park and eviction remain guarded and cleanup does not discard live claims. Testing I1's own guard against a fixture does not prove N-1 compatibility.
- Exercise the skipped-I0 upgrade case. The deployment must obtain a compatible predecessor or refuse the automatic first-writer update.
- Test scope retries and supersession: one authoritative row per intent/request/season, with the target/covered role and episode filter preserved.
- Test pressure and proactive sweeps against a pre-add claim in both orderings, including suspension at the destructive boundary. Preserve the current correction lease behaviour.
- Test same-second and later-second torrent replacement before every destructive removal path. A matching coarse timestamp alone must not authorize removal.
- Recheck I1b and I2 against their own retained predecessors, including old writes against any proposed constraint tightening. Keep ADR-0029's remaining crash-matrix and lifecycle obligations.

## Consequences and open questions

The rollout gains a compatibility release before it gains the TV fix. Each channel must establish a compatible rollback image; release ordering alone cannot guarantee that a deployment installed the intermediate image. Implementation must prove those properties before publishing the first writer to an updater-consumed tag.

The stronger instance marker, proactive-sweep exclusion mechanism, movie discriminator and concrete compatibility migrations still need implementation decisions and evidence. Recording those open choices does not waive their gates. This record changes the proposed contract and sequence, not application behaviour or schema.

## Alternatives considered

- **Continue amending ADR-0029.** Its Proposed status motivated the original amendment, but a separate record preserves the merged proposal and satisfies the repository's stricter instruction for substantive revisions.
- **Publish I1 with a rollback warning.** Rejected: ADR-0024 requires supported behaviour, and the old guard cannot see the new claims.
- **Keep the first writer pinned/manual only.** Permitted by ADR-0024 when automatic compatibility cannot be established. It does not satisfy the planned `:edge` soak, so it is an explicit alternative to the recommended I0 → I1 automatic rollout.
