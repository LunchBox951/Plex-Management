# ADR-0022: report-issue claims the active slot before the irreversible purge

- **Status:** Accepted
- **Date:** 2026-07-12
- **Supersedes:** [ADR-0014](0014-correction-verbs.md)'s report-issue step
  ordering only — every other part of ADR-0014 (the two correction verbs, the
  seeding-leak fix, the shared purge primitive, the hardlink reasoning, the
  foot-gun mount check) stands unchanged.
- **Context builds on:** [ADR-0014](0014-correction-verbs.md) (report-issue:
  blocklist + remove torrent + purge file + scan + re-arm),
  [ADR-0011](0011-tv-season-episode-support.md) (per-season TV lifecycle —
  `uq_media_requests_active` is the slot being claimed),
  [ADR-0007](0007-sqlite-alembic-migrations.md) (SQLite + single-writer
  serialization, which this reordering leans on).

## Context

ADR-0014 specified report-issue's step order as: blocklist the culprit release,
remove the torrent (with data), purge the library file, trigger a Plex scan,
**then** re-arm the request/season to `searching` (claiming
`uq_media_requests_active`) and finally commit. That is: purge, then claim.

That order has a data-loss race. `uq_media_requests_active` is a partial unique
index — at most one **active** (non-settled) row may exist per
`(tmdb_id, media_type)`. A reported request is **settled** (`available`) right
up until the re-arm flips it back to `searching`, so between the report starting
and that re-arm, nothing stops a *different* request for the same title from
being created and grabbed — concurrent auto-grab can legitimately fill that
window. Under the old order, by the time the re-arm ran, the torrent was already
removed and the library file already purged. If the re-arm then collided with
that newly-grabbed active sibling, the only thing that could be rolled back was
the database write — the deleted torrent and file stayed gone. The freshly
grabbed replacement download (or, worse, the sibling's own in-flight download)
had its backing content purged out from under it, and the report-issue call that
caused it returned failure while having destroyed a live download it never
should have touched.

Commit `c1e5608` (PR #185's follow-on fixes) closed this by reversing the
boundary: claim first, purge second. The sibling-scope rescue for a shared
multi-season pack, added in `5eb6c36` (PR #185), is threaded through the same
claim step for the same reason — a purge that follows the claim must not take a
co-owned season pack down with it. `src/plex_manager/services/correction_service.py`
(`report_issue`, roughly lines 805–994) and its regression tests reflect the new
order; ADR-0014's prose did not, leaving the accepted decision record describing
an order the code deliberately abandoned as unsafe.

## Decision

**report-issue claims the active uniqueness slot before any irreversible
external I/O.** Concretely, within one flow:

1. **Reversible checks and reversible DB writes may run before the claim.**
   The upfront active-sibling preflight (`find_active`), the foot-gun mounted-root
   check, and writing the blocklist row all happen first — none of them touch a
   torrent or a file, and the blocklist row rolls back cleanly if the claim below
   fails.
2. **The request (or season) must claim and flush the active slot —
   `uq_media_requests_active` — before any irreversible client or filesystem
   I/O.** The re-arm to `searching` is flushed (not just added to the session)
   so a colliding insert/update from a racing active sibling raises
   `IntegrityError` right here, before the torrent-remove or file-purge steps
   run.
3. **A lost claim returns 409 with nothing deleted.** If the flush collides,
   the transaction is rolled back — undoing the blocklist row and the partial
   re-arm together — and the call fails with `ActiveDuplicateError` (409). At
   that point no torrent has been removed and no file has been purged; the
   racing sibling's download is untouched.
4. **The library-path breadcrumb is kept through the claim and cleared only on
   confirmed purge success.** The claim step re-arms the row with
   `clear_library_path=False`; the breadcrumb is the only handle a later retry
   or eviction sweep has to reclaim an orphaned file, so it must survive a step
   whose file-purge outcome isn't known yet.
5. **Shared-pack sibling scopes are rescued before the payload they share is
   deleted.** When the culprit's release is a multi-season pack, the other
   seasons sharing that download are rescued inside the same claim step,
   before the torrent-with-data removal that follows can take their payload
   with it.
6. **Only after the claim succeeds does the flow perform irreversible I/O:**
   remove the culprit torrent with data, purge the library file, trigger the
   Plex scan. These steps cannot be rolled back by a database transaction, so
   they must never run while the slot claim that guards them could still fail.
7. **SQLite's single-writer serialization holds the slot through commit.**
   Because SQLite serializes writers, once the claim's flush holds
   `uq_media_requests_active` inside the open transaction, no competing writer
   can commit a conflicting active row before this transaction's own commit —
   so the final commit (blocklist + claim + breadcrumb-clear + audit row,
   together) cannot itself fail on the dedup index after the irreversible
   torrent/file steps have already run. This guarantee is specific to SQLite's
   locking model (ADR-0007); a future multi-writer database would need an
   equivalent (e.g., `SELECT … FOR UPDATE` or a serializable transaction) to
   preserve the same property.

The general rule this establishes for any future correction/eviction verb that
mixes a uniqueness claim with irreversible external side effects: **claim
first, purge second — never the reverse.** External side effects (torrent
removal, filesystem deletion) cannot be rolled back by a database transaction,
so any step that can still fail on a uniqueness collision must run strictly
before them, not after.

## Consequences

- The report-issue race that could purge a live, freshly-grabbed download is
  closed and regression-tested: a deterministic check-to-claim race test
  inserts a competing active request between the preflight and the claim,
  and asserts the collision returns 409 with the file/torrent intact and the
  blocklist row rolled back.
- ADR-0014 remains the record of *why* report-issue exists and how it composes
  the blocklist/purge/re-search/hardlink primitives — only its step-ordering
  claim is superseded here. ADR-0014's header now links forward to this ADR so
  a reader following the ordering steps lands on the current, safe order
  instead of reimplementing the data-loss-prone one.
- Any new verb that combines a `uq_media_requests_active` (or similarly-scoped
  uniqueness) claim with an irreversible external delete must follow this
  ordering — claim-then-purge — not ADR-0014's original purge-then-claim.
- The SQLite single-writer assumption in step 7 is now an explicit, documented
  dependency. Porting the claim/purge boundary to a different database engine
  requires re-verifying (or replacing) that guarantee, not assuming it holds.
