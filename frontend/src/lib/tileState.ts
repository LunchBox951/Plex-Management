/**
 * Discover/Search tile library-state badge (issue #29).
 *
 * A tile's badge is the fold of two sources:
 *   1. the SERVER base state on `DiscoverResult.library_state` — Plex presence +
 *      the request store, computed per page (only the server can crawl Plex, so
 *      "owned but never requested through the app" can only come from here);
 *   2. the CLIENT overlay — the live request lifecycle from the `useRequests()`
 *      poll the app already runs, so a tile animates pending→downloading→available
 *      without re-fetching Discover.
 *
 * The client overlay WINS for a live active/available request, using the exact
 * `(tmdb_id, media_type)` correlation `TitleDetailModal` implements. A settled
 * NON-available request (failed/evicted/cancelled) must NOT shadow the server base
 * with a "Failed"/"Evicted" badge — and when the settle happened AFTER the base
 * snapshot was fetched, the request-derived portion of that base
 * (`requested`/`processing`/`partially_available`) is stale and must not fall
 * through either: it degrades to presence-derived truth (see `settledBaseFallback`).
 *
 * WHICH base snapshots count as stale is TIME-AWARE, not row-shape-aware. Rows
 * expose no server timestamps, so the client tracks — entirely on its own clock, no
 * server-clock mixing — when THIS session first OBSERVED each row in a settled-bad
 * status (see `settleObservedAt`): a watched TRANSITION records the poll that
 * carried the settle, and a row ALREADY SETTLED at first sighting records that
 * first sighting. The first-sighting case is what closes the mount race: a discover
 * response computed while the row was still `requested`, raced by a first
 * /requests poll that lands after the settle, would otherwise render its stale
 * base indefinitely with nothing ever observed. The degradation applies only to a
 * base fetched BEFORE the observation; a base fetched after it was recomputed by
 * the server with the settled status already in the fold (`derive_library_state`
 * reads the request store fresh per page), so it is trusted verbatim. Without the
 * time gate the suppression would be PERMANENT: a movie re-added to Plex after a
 * failed re-request would never regain its "In library" badge even after Discover
 * refetched a genuinely fresh `available` base.
 *
 * The trust rule carries a one-RTT ERROR BAR: `dataUpdatedAt` is client RECEIPT
 * time, but the server read the request rows up to one round trip earlier (state
 * resolution reads statuses before the Plex presence crawl), so a base that
 * resolved just after the observation can still have been computed pre-settle and
 * be wrongly trusted for one beat. That is why, on first observing a settle, the
 * discover queries are ALWAYS invalidated (fire-and-forget, once per row per
 * session, never gated on the observing call's own base freshness — wave 7):
 * `invalidateQueries(['discover'])` marks every sibling cache stale (react-query
 * refetches active queries immediately; inactive ones on next activation), which
 * both bounds any wrong-trust from the error bar to one refetch cycle and heals
 * OLDER cached sibling queries (home vs per-search caches) that still predate the
 * settle — a freshness-gated skip starved exactly those. Costs: for a
 * LONG-AGO-settled row, one discover refetch at first sighting (and, when the
 * mount's discover response resolved before the first poll, one suppressed beat
 * until that refetch lands) — bounded and self-healing, versus indefinite
 * staleness in the races it closes.
 *
 * This status→state table mirrors the server's `derive_library_state`
 * (services/discovery_service.py); a drift makes base and overlay disagree on a tile.
 */
import type { DiscoverResult, RequestResponse } from '../api/types'
import { queryClient } from './queryClient'
import { requestStatus, type StatusPresentation } from './status'

// Settled, non-available request statuses. A row in one of these is "done and gone"
// and must never overlay the server base (mirrors the backend `_SETTLED_REQUEST_STATUSES`
// minus `available`, and `TitleDetailModal`'s own `liveRequest` exclusion list).
const OVERLAY_SUPPRESSED = new Set(['failed', 'evicted', 'cancelled'])

// ---------------------------------------------------------------------------
// Settle observation (client clock only).
//
// `settleObservedAt`: the client-clock instant each request row was FIRST seen in
// a settled-bad status — on a watched transition, the timestamp of the poll that
// carried the settle; on a row ALREADY settled at first sighting, that first
// sighting (the settle provably happened at or before it — wave 6: without this,
// a row settling between the discover fetch and the first poll would never be
// observed and its stale base would render indefinitely). Comparing against the
// discover query's `dataUpdatedAt` (also client clock, via react-query) is sound:
// no server clock is involved on either side. The residual race — a discover
// response computed server-side just before the settle but received just after
// the observation — is bounded by one HTTP round trip and self-heals on the
// invalidation refetch below.
//
// ADR-0014 report-issue can re-arm a settled row to an ACTIVE status (same id), so
// seeing a row in a non-settled-bad status clears its observation: the next settle
// is a NEW event and gets a fresh timestamp.
// ---------------------------------------------------------------------------
const settleObservedAt = new Map<number, number>()

/** Test-isolation helper: forget every observed settle. */
export function resetSettleObservations(): void {
  settleObservedAt.clear()
}

function trackSettleObservations(
  matches: RequestResponse[],
  requestsFetchedAt: number | undefined,
): void {
  // The poll snapshot's own receipt time is tighter than render-time Date.now()
  // (same clock domain either way); fall back when the caller doesn't have it.
  const observed = requestsFetchedAt ?? Date.now()
  for (const r of matches) {
    if (!OVERLAY_SUPPRESSED.has(r.status)) {
      // Active (or available) again — e.g. an ADR-0014 report-issue re-arm. Any
      // previous settle observation is history; a future settle is a new event.
      settleObservedAt.delete(r.id)
    } else if (!settleObservedAt.has(r.id)) {
      // First time this session sees the row settled — either a watched transition
      // or already settled at first sighting; both record (see the registry
      // comment on why first sighting counts).
      settleObservedAt.set(r.id, observed)
      // Ask react-query to refetch every discover query: invalidateQueries with the
      // ['discover'] prefix (same call shape as useUpdateSettings) marks ALL sibling
      // caches stale — active ones refetch immediately, inactive ones on their next
      // activation — so every base snapshot heals, not just the one this call
      // happens to render with. Fired UNCONDITIONALLY (wave 7): gating it on the
      // calling query's own freshness broke the healing twice over — (a) the
      // receipt-time race: `baseFetchedAt` is client RECEIPT time, but the server
      // read the request rows up to one RTT earlier, so a base that RESOLVED after
      // the observation can still have been COMPUTED pre-settle (wrongly trusted;
      // gated, nothing ever healed it), and (b) sibling caches: an older cached
      // discover query still predates the settle, but the per-row once-guard meant
      // the skipped invalidation could never fire again (stuck unbadged). One extra
      // discover refetch per settled row per session is the whole cost.
      // Deferred to a microtask: deriveTileState runs during render, and scheduling
      // refetches synchronously mid-render is a React anti-pattern. Fires at most
      // once per row per session (guarded by the `has` check above).
      queueMicrotask(() => {
        void queryClient.invalidateQueries({ queryKey: ['discover'] })
      })
    }
  }
}

function libraryStateToPresentation(
  state: DiscoverResult['library_state'],
): StatusPresentation | null {
  switch (state) {
    case 'available':
      return requestStatus('available') // { label: 'In library', intent: 'available' }
    case 'partially_available':
      return requestStatus('partially_available')
    case 'requested':
      return requestStatus('pending') // { label: 'Requested', intent: 'neutral' }
    case 'processing':
      // An in-flight grab the client poll hasn't surfaced yet (no visible request row):
      // the honest "working on it" hint until the live overlay takes over.
      return { label: 'Requested', intent: 'searching' }
    case 'none':
      return null
    default:
      return null
  }
}

/**
 * The badge presentation for a tile, or `null` when it should stay unbadged.
 *
 * `baseFetchedAt` is the client-clock time the tile's discover snapshot was
 * received — react-query's `dataUpdatedAt` for the query that produced `result`.
 * It gates the stale-base degradation (see the module docstring): a base fetched
 * after the settle was observed is trusted verbatim. Omitted (tests/legacy
 * callers), the base is treated as predating every observed settle.
 *
 * `requestsFetchedAt` is the client-clock time the `requests` snapshot was
 * received — react-query's `dataUpdatedAt` for the /requests poll. It stamps
 * settle observations (tighter than render-time `Date.now()`, which is the
 * fallback when omitted; same clock domain either way).
 */
export function deriveTileState(
  result: DiscoverResult,
  requests: RequestResponse[] | undefined,
  baseFetchedAt?: number,
  requestsFetchedAt?: number,
): StatusPresentation | null {
  // The live request for this exact title — identical correlation to
  // TitleDetailModal.tsx: /requests is id-ascending and the backend allows
  // re-requesting a settled title, so prefer a non-settled match, else the newest.
  const matches = (requests ?? []).filter(
    (r) => r.tmdb_id === result.tmdb_id && r.media_type === result.media_type,
  )
  trackSettleObservations(matches, requestsFetchedAt)
  const active = matches.find((r) => !isSettled(r.status))
  const liveRequest = active ?? matches[matches.length - 1] ?? null

  if (liveRequest) {
    // Overlay wins for a live active/available request.
    if (!OVERLAY_SUPPRESSED.has(liveRequest.status)) {
      return requestStatus(liveRequest.status)
    }

    // A settled-bad row (failed/cancelled/evicted) never badges the tile itself.
    // Whether it also invalidates the server base depends on WHEN the base was
    // fetched relative to the observed settle:
    //  - base fetched AFTER the observation: the server recomputed it with the
    //    settled status — trust the base. This is what lifts the suppression after
    //    a refetch instead of hiding a re-added title forever, and what trusts a
    //    long-ago settle whose base resolved after the first poll.
    //  - base fetched BEFORE the observation (or fetch time unknown): the base
    //    cannot be proven to reflect the settle — degrade its request-derived
    //    portion. First-sighting observations (wave 6) land here too: the settle
    //    happened at or before the poll that first carried the row, so a base
    //    older than that poll may predate the settle.
    // A settled liveRequest ALWAYS has an observation (recorded just above);
    // `?? Infinity` merely keeps the comparison total for the type system.
    const observedAt = settleObservedAt.get(liveRequest.id) ?? Number.POSITIVE_INFINITY
    if (baseFetchedAt !== undefined && baseFetchedAt > observedAt) {
      return libraryStateToPresentation(result.library_state)
    }

    // MOVIE re-request contradiction: a settled liveRequest that coexists with an
    // OLDER `available` row proves the pre-settle `available` base stale too. The
    // movie create path (request_service.create_request) NEVER creates a second row
    // while Plex still has the title — its fresh `is_available(use_cache=False)`
    // check dedups to the existing in-library row instead — so the newer request's
    // very existence means the title read ABSENT at create time (the G7
    // removed-then-re-requested path). Not applied to tv: a season-level re-request
    // (e.g. a newly aired season) is legitimately created while the show remains
    // partially/fully present, so its failure says nothing about the seasons on disk.
    const rerequestContradictsPresence =
      result.media_type === 'movie' &&
      matches.some((r) => r !== liveRequest && r.status === 'available')
    return settledBaseFallback(
      result.library_state,
      liveRequest.status,
      rerequestContradictsPresence,
    )
  }

  // No live row for this title: the server base is the only source of truth.
  return libraryStateToPresentation(result.library_state)
}

/**
 * A PRE-SETTLE server base with its stale REQUEST-derived portion stripped, for a
 * tile whose live request row was OBSERVED settling to a non-available terminal
 * state after the base was fetched (the caller has already established that timing;
 * a post-settle base is trusted verbatim and never reaches this fold).
 *
 * Which base values are request-derived follows the server's `derive_library_state`
 * (services/discovery_service.py): `requested`, `processing`, and
 * `partially_available` come ONLY from a request-store status — the Plex presence
 * crawl is a whole-title boolean (`available`/`none`) and can never say "partially"
 * — so the settle proves all three stale, and they degrade to unbadged.
 *
 * `available` is the one dual-source value: request status OR Plex presence. For
 * `failed` / `cancelled` it survives — presence is an independent fact those statuses
 * don't invalidate, and a request row in `available` cannot itself settle to
 * failed/cancelled (cancel excludes it; ADR-0014 report-issue re-arms to an ACTIVE
 * status, which the overlay shows live), so a settled row beside an `available` base
 * is usually an old row beside a genuinely-present title.
 *
 * The exception is `presenceContradicted` (movies only, see the caller): when the
 * settled row is a RE-REQUEST that coexists with an older `available` row, the movie
 * create path's fresh Plex check proved the title ABSENT at create time (it would
 * have deduped to the in-library row otherwise), so the pre-settle `available` base
 * is itself stale history — drop it. The two narrow ways a movie re-request exists
 * WITHOUT proven absence (Plex unconfigured, or a transient outage during the create's
 * check) also can't verify presence, so degrading to unbadged stays the honest hint.
 *
 * `evicted` is stricter: ADR-0012 eviction means the disk-pressure sweep DELETED the
 * file, which directly contradicts a pre-settle `available` snapshot. The live evicted
 * row is fresher than that snapshot — and the correlation would have preferred an
 * active re-request over it if one existed, so there is none — so `evicted` drops
 * presence too. The tile degrades to unbadged rather than claim a file that was just
 * deleted; the modal, when opened, shows the true live status. This mirrors, and must
 * stay in sync with, the server's own evicted rule in `derive_library_state`
 * (services/discovery_service.py), which refuses the presence fallback for an
 * `evicted` request row for the same reason (warmed presence cache + Plex's
 * asynchronous scan can both still say "present" right after the delete).
 */
function settledBaseFallback(
  state: DiscoverResult['library_state'],
  settledStatus: string,
  presenceContradicted: boolean,
): StatusPresentation | null {
  if (settledStatus === 'evicted' || presenceContradicted) return null
  return state === 'available' ? libraryStateToPresentation(state) : null
}

/** A settled request status — matches the backend `_SETTLED_REQUEST_STATUSES`. */
function isSettled(status: string): boolean {
  return status === 'available' || OVERLAY_SUPPRESSED.has(status)
}
