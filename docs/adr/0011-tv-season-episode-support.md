# ADR-0011: TV support — per-season lifecycle with a computed request rollup

- **Status:** Accepted
- **Date:** 2026-07-01
- **Context builds on:** [ADR-0010](0010-import-pipeline-honest-availability.md)
  (import pipeline, honest availability, public request-state contract).

## Context

The early beta ([`beta-plan.md`](../design/beta-plan.md)) closed the
request → watchable loop for **movies only** and deliberately deferred TV. The
schema was built TV-aware from day one (`MediaType.tv`, a `season_requests`
table, `Download.season` / `Download.episodes_json`), but the *logic* — naming,
import, availability, discovery, scoring, and the request UI — assumed a single
file per request. To replace the prototype, TV (seasons + episodes) must close
the same honest loop.

A TV show is not one downloadable unit: its seasons air, download, import, and
become available **independently and over time**. Modelling a show as a single
request with a single status (as movies are) would either force whole-show
all-or-nothing behaviour or make the one public `status` field dishonest.

## Decision

1. **The `SeasonRequest` row is the per-season lifecycle unit.** A TV
   `MediaRequest` (`media_type="tv"`) represents the show; each requested season
   is a `SeasonRequest` carrying its own `RequestStatus`, created idempotently
   (mirroring how `Download` rows are created lazily at grab time). A request may
   name specific `seasons`; omitting them means "the whole aired series."

2. **`MediaRequest.status` is a computed rollup** of its seasons' statuses (pure
   `domain/season_rollup.rollup_status`), persisted after every season
   transition, so the single public `status` field stays honest for a
   multi-season show. A new terminal-adjacent `RequestStatus.partially_available`
   expresses "some, but not all, seasons are in the library." It is a bare
   `VARCHAR` value (`native_enum=False`), so no CHECK-constraint migration is
   needed — only the active-dedup index predicate includes it.

3. **Grabs and downloads are per-season, never per-show**, so Season 1 and
   Season 2 of the same show download concurrently. The one-active-download
   invariant widens from `media_request_id` to `(media_request_id,
   COALESCE(season, -1))` — the `-1` sentinel preserves the movie single-download
   guarantee (SQL treats `NULL != NULL`, which would otherwise let two concurrent
   movie grabs stop colliding) while giving TV genuine per-season uniqueness.

4. **A season pack imports with partial success.** Import validation gains a
   sibling `validate_season_import` that validates *every* file independently
   (reusing the movie CAM/TS quality brain and the `matches_media` season-gate
   per file — a wrong-season file in a mislabeled pack is `WRONG_MEDIA`, never
   mis-routed). Some episodes may be accepted while others are rejected or
   skipped; the accepted ones import and the season advances, honestly.

5. **Plex availability is confirmed per season** (the season has ≥1 episode
   indexed by Plex, via `leafCount` on `/library/metadata/{ratingKey}/children`),
   matching `SeasonRequest` granularity. True per-episode completeness is a
   scoped follow-up, not required to close the loop.

6. **Season-pack scoring is additive and gated.** The decision engine gains an
   optional `prefer_season_pack` preference, off by default (byte-identical for
   movies and single-episode grabs), enabled only when the operator requests a
   whole season.

## Consequences

- The request → grab → import → available loop, and the honest retryable states
  (`no_acceptable_release`, `import_blocked`, the two-phase `completed →
  available`), all extend to TV **per season** rather than being rebuilt.
- One Alembic migration carries the schema deltas (the `partially_available`
  dedup predicate, the `COALESCE(season,-1)` download-uniqueness index, and a
  unique `season_requests(media_request_id, season_number)` index that makes the
  idempotent-ensure race safe). Every delta is mirrored in `models.py` because
  the test suite builds its schema via `Base.metadata.create_all`, not Alembic.
- `tv_root` is an **optional** library-root setting (a movie-only install must
  not be forced to configure TV); the reconcile loop surfaces a per-root honest
  `import_blocked` reason instead of skipping the whole cycle when only one root
  is unset.
- Anime separation (its own root + dual-audio bonus) and policy-based retention
  remain deferred; disk-pressure eviction and operability ship alongside TV in
  the wider beta but are tracked separately.
