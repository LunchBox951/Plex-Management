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
 * server-clock mixing — when THIS session first OBSERVED a row transition into a
 * settled-bad status (see `settleObservedAt`). The degradation applies only to a
 * base fetched BEFORE that observation; a base fetched after it was recomputed by
 * the server with the settled status already in the fold (`derive_library_state`
 * reads the request store fresh per page), so it is trusted verbatim. Without the
 * time gate the suppression would be PERMANENT: a movie re-added to Plex after a
 * failed re-request would never regain its "In library" badge even after Discover
 * refetched a genuinely fresh `available` base. A row already settled when this
 * session first sees it never suppresses anything — its base snapshot was fetched
 * at the same time or later and already reflects the settle.
 *
 * On first observing a settle transition the discover queries are invalidated
 * (fire-and-forget), so the suppressed tile self-heals to the server's fresh truth
 * within one refetch instead of waiting for the next visit.
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
// Settle-transition observation (client clock only).
//
// `lastSeenStatus`: the status each request row carried when this session last
// processed it. `settleObservedAt`: Date.now() at the moment a row was FIRST seen
// in a settled-bad status having previously been seen in a different, non-settled-
// bad one — i.e. the client-side observation time of the settle. Comparing that
// against the discover query's `dataUpdatedAt` (also client clock, via react-query)
// is sound: no server clock is involved on either side. The residual race — a
// discover response computed server-side just before the settle but received just
// after the observation — is bounded by one HTTP round trip and self-heals on the
// invalidation refetch below.
//
// ADR-0014 report-issue can re-arm a settled row to an ACTIVE status (same id), so
// seeing a row in a non-settled-bad status clears its observation: the next settle
// is a NEW event and gets a fresh timestamp.
// ---------------------------------------------------------------------------
const lastSeenStatus = new Map<number, string>()
const settleObservedAt = new Map<number, number>()

/** Test-isolation helper: forget every observed settle transition. */
export function resetSettleObservations(): void {
  lastSeenStatus.clear()
  settleObservedAt.clear()
}

function trackSettleTransitions(matches: RequestResponse[]): void {
  for (const r of matches) {
    const prev = lastSeenStatus.get(r.id)
    if (!OVERLAY_SUPPRESSED.has(r.status)) {
      // Active (or available) again — e.g. an ADR-0014 report-issue re-arm. Any
      // previous settle observation is history; a future settle is a new event.
      settleObservedAt.delete(r.id)
    } else if (prev !== undefined && !OVERLAY_SUPPRESSED.has(prev) && !settleObservedAt.has(r.id)) {
      settleObservedAt.set(r.id, Date.now())
      // The base snapshot predates this settle — ask react-query to refetch every
      // discover query (home + any search page; prefix match, same call shape as
      // useUpdateSettings) so the tile self-heals to the server's fresh fold within
      // one round trip. Deferred to a microtask: deriveTileState runs during render,
      // and scheduling refetches synchronously mid-render is a React anti-pattern.
      queueMicrotask(() => {
        void queryClient.invalidateQueries({ queryKey: ['discover'] })
      })
    }
    lastSeenStatus.set(r.id, r.status)
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
 */
export function deriveTileState(
  result: DiscoverResult,
  requests: RequestResponse[] | undefined,
  baseFetchedAt?: number,
): StatusPresentation | null {
  // The live request for this exact title — identical correlation to
  // TitleDetailModal.tsx: /requests is id-ascending and the backend allows
  // re-requesting a settled title, so prefer a non-settled match, else the newest.
  const matches = (requests ?? []).filter(
    (r) => r.tmdb_id === result.tmdb_id && r.media_type === result.media_type,
  )
  trackSettleTransitions(matches)
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
    //  - settle never observed in-session: the row was already settled when this
    //    session started, so the base (fetched at or after session start) already
    //    folds it — trust the base.
    //  - base fetched AFTER the observation: the server recomputed it with the
    //    settled status — trust the base. This is what lifts the suppression after
    //    a refetch instead of hiding a re-added title forever.
    //  - base fetched BEFORE the observation (or fetch time unknown): the base
    //    cannot reflect the settle — degrade its request-derived portion.
    const observedAt = settleObservedAt.get(liveRequest.id)
    const basePostdatesSettle =
      observedAt === undefined || (baseFetchedAt !== undefined && baseFetchedAt > observedAt)
    if (basePostdatesSettle) {
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
