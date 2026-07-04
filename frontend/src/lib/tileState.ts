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
 * with a "Failed"/"Evicted" badge — but its presence ALSO proves the request the
 * server saw at page load is gone, so the request-derived portion of that base
 * (`requested`/`processing`/`partially_available`) is now stale and must not fall
 * through either. It degrades to the presence-derived truth instead (see
 * `settledBaseFallback`).
 *
 * This status→state table mirrors the server's `derive_library_state`
 * (services/discovery_service.py); a drift makes base and overlay disagree on a tile.
 */
import type { DiscoverResult, RequestResponse } from '../api/types'
import { requestStatus, type StatusPresentation } from './status'

// Settled, non-available request statuses. A row in one of these is "done and gone"
// and must never overlay the server base (mirrors the backend `_SETTLED_REQUEST_STATUSES`
// minus `available`, and `TitleDetailModal`'s own `liveRequest` exclusion list).
const OVERLAY_SUPPRESSED = new Set(['failed', 'evicted', 'cancelled'])

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
 */
export function deriveTileState(
  result: DiscoverResult,
  requests: RequestResponse[] | undefined,
): StatusPresentation | null {
  // The live request for this exact title — identical correlation to
  // TitleDetailModal.tsx: /requests is id-ascending and the backend allows
  // re-requesting a settled title, so prefer a non-settled match, else the newest.
  const matches = (requests ?? []).filter(
    (r) => r.tmdb_id === result.tmdb_id && r.media_type === result.media_type,
  )
  const active = matches.find((r) => !isSettled(r.status))
  const liveRequest = active ?? matches[matches.length - 1] ?? null

  if (liveRequest) {
    // Overlay wins for a live active/available request.
    if (!OVERLAY_SUPPRESSED.has(liveRequest.status)) {
      return requestStatus(liveRequest.status)
    }
    // A settled-bad row (failed/cancelled/evicted) does not badge the tile — and it
    // proves the request the server folded into `library_state` at page load is now
    // gone, so the request-derived base (`requested`/`processing`/
    // `partially_available`) is stale and must not fall through either. Degrade to
    // presence-derived truth.
    return settledBaseFallback(result.library_state, liveRequest.status)
  }

  // No live row for this title: the server base is the only source of truth.
  return libraryStateToPresentation(result.library_state)
}

/**
 * The server base with its stale REQUEST-derived portion stripped, for a tile whose
 * live request row has SETTLED to a non-available terminal state.
 *
 * Which base values are request-derived follows the server's `derive_library_state`
 * (services/discovery_service.py): `requested`, `processing`, and
 * `partially_available` come ONLY from a request-store status — the Plex presence
 * crawl is a whole-title boolean (`available`/`none`) and can never say "partially"
 * — so a settled live row proves all three stale, and they degrade to unbadged.
 *
 * `available` is the one dual-source value: request status OR Plex presence. For
 * `failed` / `cancelled` it survives — presence is an independent fact those statuses
 * don't invalidate, and a request row in `available` cannot itself settle to
 * failed/cancelled (cancel excludes it; ADR-0014 report-issue re-arms to an ACTIVE
 * status, which the overlay shows live), so a settled row beside an `available` base
 * is an old row beside a genuinely-present title.
 *
 * `evicted` is stricter: ADR-0012 eviction means the disk-pressure sweep DELETED the
 * file, which directly contradicts a page-load `available` snapshot. The live evicted
 * row is fresher than that snapshot — and the correlation would have preferred an
 * active re-request over it if one existed, so there is none — so `evicted` drops
 * presence too. The tile degrades to unbadged rather than claim a file that was just
 * deleted; the modal, when opened, shows the true live status.
 */
function settledBaseFallback(
  state: DiscoverResult['library_state'],
  settledStatus: string,
): StatusPresentation | null {
  if (settledStatus === 'evicted') return null
  return state === 'available' ? libraryStateToPresentation(state) : null
}

/** A settled request status — matches the backend `_SETTLED_REQUEST_STATUSES`. */
function isSettled(status: string): boolean {
  return status === 'available' || OVERLAY_SUPPRESSED.has(status)
}
