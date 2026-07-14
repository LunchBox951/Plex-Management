import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DiscoverResult, RequestResponse } from '../api/types'
import { queryClient } from './queryClient'
import { deriveTileState, resetSettleObservations } from './tileState'

function result(overrides: Partial<DiscoverResult> = {}): DiscoverResult {
  return {
    tmdb_id: 1,
    media_type: 'movie',
    title: 'A Title',
    year: 2024,
    overview: null,
    poster_url: null,
    backdrop_url: null,
    library_state: 'none',
    ...overrides,
  }
}

function request(overrides: Partial<RequestResponse> = {}): RequestResponse {
  return {
    id: 1,
    tmdb_id: 1,
    media_type: 'movie',
    title: 'A Title',
    status: 'pending',
    is_anime: false,
    keep_forever: false,
    can_mutate: false,
    is_owner: false,
    can_withdraw: false,
    has_other_participants: false,
    ...overrides,
  }
}

/**
 * Simulate this session OBSERVING a settle: one poll shows the row active, the
 * next shows it settled. The second call both records the observation (client
 * clock) and derives — its return value is the tile state right after the settle.
 */
function observeSettle(
  tile: DiscoverResult,
  activeRows: RequestResponse[],
  settledRows: RequestResponse[],
  baseFetchedAt?: number,
) {
  deriveTileState(tile, activeRows, baseFetchedAt)
  return deriveTileState(tile, settledRows, baseFetchedAt)
}

const BASE_BEFORE_SETTLE = () => Date.now() - 60_000
const BASE_AFTER_SETTLE = () => Date.now() + 60_000

beforeEach(() => {
  resetSettleObservations()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('deriveTileState — server base only (no requests)', () => {
  it('maps library_state "available" to the In-library badge', () => {
    expect(deriveTileState(result({ library_state: 'available' }), undefined)).toEqual({
      label: 'In library',
      intent: 'available',
    })
  })

  it('maps library_state "none" to no badge', () => {
    expect(deriveTileState(result({ library_state: 'none' }), [])).toBeNull()
  })

  it('maps library_state "partially_available" to the partial badge', () => {
    expect(deriveTileState(result({ library_state: 'partially_available' }), [])).toEqual({
      label: 'Partially available',
      intent: 'available',
    })
  })

  it('maps library_state "requested" to a Requested badge', () => {
    expect(deriveTileState(result({ library_state: 'requested' }), [])).toEqual({
      label: 'Requested',
      intent: 'neutral',
    })
  })

  it('maps library_state "processing" to an in-progress badge', () => {
    expect(deriveTileState(result({ library_state: 'processing' }), [])).toEqual({
      label: 'Requested',
      intent: 'searching',
    })
  })
})

describe('deriveTileState — live request overlay', () => {
  it('lets an active request overlay the server base', () => {
    // Server base is "none", but a live downloading request is polling: the overlay wins.
    const state = deriveTileState(
      result({ library_state: 'none' }),
      [request({ status: 'downloading' })],
    )
    expect(state).toEqual({ label: 'Downloading', intent: 'downloading' })
  })

  it('does NOT let a settled failed row shadow a server "available"', () => {
    // A failed re-request from the past must not turn an owned title into "Failed":
    // it falls through to the server base (In library) once the base is provably
    // fresher than the settle observation.
    const state = deriveTileState(
      result({ library_state: 'available' }),
      [request({ status: 'failed' })],
      BASE_AFTER_SETTLE(),
    )
    expect(state).toEqual({ label: 'In library', intent: 'available' })
  })

  it('does NOT let a settled cancelled row shadow a server base', () => {
    const state = deriveTileState(
      result({ library_state: 'none' }),
      [request({ status: 'cancelled' })],
    )
    expect(state).toBeNull()
  })

  it('prefers the active row over an older settled row for the same title', () => {
    const state = deriveTileState(result({ library_state: 'none' }), [
      request({ id: 1, status: 'evicted' }),
      request({ id: 2, status: 'searching' }),
    ])
    expect(state).toEqual({ label: 'Searching', intent: 'searching' })
  })

  it('shows a live available request as In library', () => {
    const state = deriveTileState(
      result({ library_state: 'none' }),
      [request({ status: 'available' })],
    )
    expect(state).toEqual({ label: 'In library', intent: 'available' })
  })
})

describe('deriveTileState — an OBSERVED settle degrades a pre-settle base', () => {
  // The base snapshot was fetched BEFORE this session watched the request settle,
  // so its request-derived portion cannot reflect the settle and must not fall
  // through as "Requested".
  it('degrades a stale "requested" base to none when the live row is seen failing', () => {
    const state = observeSettle(
      result({ library_state: 'requested' }),
      [request({ status: 'downloading' })],
      [request({ status: 'failed' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toBeNull()
  })

  it('degrades a stale "processing" base to none when the live row is seen cancelling', () => {
    const state = observeSettle(
      result({ library_state: 'processing' }),
      [request({ status: 'searching' })],
      [request({ status: 'cancelled' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toBeNull()
  })

  it('keeps presence-derived "available" through an observed failed settle', () => {
    // Library presence is independent of the request lifecycle — failed doesn't
    // evict, and no older available row contradicts it.
    const state = observeSettle(
      result({ library_state: 'available' }),
      [request({ status: 'downloading' })],
      [request({ status: 'failed' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toEqual({ label: 'In library', intent: 'available' })
  })

  it('degrades a stale "partially_available" base to none on an observed failed settle', () => {
    // `partially_available` is ONLY ever request-derived (the server's presence crawl
    // is a whole-title boolean — see derive_library_state), so the settle proves it
    // stale just like `requested`/`processing`.
    const state = observeSettle(
      result({ tmdb_id: 7, media_type: 'tv', library_state: 'partially_available' }),
      [request({ tmdb_id: 7, media_type: 'tv', status: 'downloading' })],
      [request({ tmdb_id: 7, media_type: 'tv', status: 'failed' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toBeNull()
  })

  it('degrades a stale "partially_available" base to none on an observed cancel', () => {
    const state = observeSettle(
      result({ tmdb_id: 7, media_type: 'tv', library_state: 'partially_available' }),
      [request({ tmdb_id: 7, media_type: 'tv', status: 'searching' })],
      [request({ tmdb_id: 7, media_type: 'tv', status: 'cancelled' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toBeNull()
  })

  it('drops even a stale "available" base when the row is seen evicting', () => {
    // ADR-0012: eviction DELETED the file, contradicting the pre-settle presence
    // snapshot. The fresher live evicted row wins — degrade to unbadged, don't claim
    // a file that was just deleted. (available -> evicted is the sweep's own CAS.)
    const state = observeSettle(
      result({ library_state: 'available' }),
      [request({ status: 'available' })],
      [request({ status: 'evicted' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toBeNull()
  })

  it('drops a stale "partially_available" base when the row is seen evicting', () => {
    const state = observeSettle(
      result({ tmdb_id: 7, media_type: 'tv', library_state: 'partially_available' }),
      [request({ tmdb_id: 7, media_type: 'tv', status: 'available' })],
      [request({ tmdb_id: 7, media_type: 'tv', status: 'evicted' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toBeNull()
  })

  it('suppresses when the base fetch time is unknown (conservative default)', () => {
    const state = observeSettle(
      result({ library_state: 'requested' }),
      [request({ status: 'downloading' })],
      [request({ status: 'failed' })],
      undefined,
    )
    expect(state).toBeNull()
  })

  it('leaves the no-live-row case unchanged (server base is the only truth)', () => {
    expect(deriveTileState(result({ library_state: 'requested' }), [])).toEqual({
      label: 'Requested',
      intent: 'neutral',
    })
    expect(deriveTileState(result({ library_state: 'available' }), [])).toEqual({
      label: 'In library',
      intent: 'available',
    })
  })
})

describe('deriveTileState — the suppression is time-aware, never permanent', () => {
  it('trusts a base refetched AFTER the observed settle (movie re-added to Plex)', () => {
    // The wave-5 scenario: old available row + newer re-request observed failing;
    // Discover then REFETCHES and the server returns "available" from fresh presence
    // (the movie was re-added/re-imported). The suppression must lift — hiding the
    // badge forever would be permanent wrongness, worse than the transient staleness
    // it replaced.
    const tile = result({ library_state: 'available' })
    const activeRows = [
      request({ id: 1, status: 'available' }),
      request({ id: 2, status: 'downloading' }),
    ]
    const settledRows = [
      request({ id: 1, status: 'available' }),
      request({ id: 2, status: 'failed' }),
    ]
    // Settle observed against the pre-settle base: suppressed (wave-4 semantics).
    expect(observeSettle(tile, activeRows, settledRows, BASE_BEFORE_SETTLE())).toBeNull()
    // The SAME rows after a discover refetch: the fresh base already folds the
    // failed status server-side, so "available" is presence truth — badge shows.
    expect(deriveTileState(tile, settledRows, BASE_AFTER_SETTLE())).toEqual({
      label: 'In library',
      intent: 'available',
    })
  })

  it('suppresses a base older than the first poll that already carries the settled row', async () => {
    // The wave-6 race: the discover response was computed while the row was still
    // requested/processing; the FIRST /requests poll lands AFTER the settle. There
    // is no transition to watch — the first sighting IS the observation, and a base
    // older than that poll cannot be proven to reflect the settle. Suppress AND
    // queue the one-shot invalidation so the tile self-heals within one refetch.
    const invalidate = vi
      .spyOn(queryClient, 'invalidateQueries')
      .mockResolvedValue(undefined as never)
    const pollAt = Date.now()
    const state = deriveTileState(
      result({ library_state: 'requested' }),
      [request({ status: 'failed' })],
      pollAt - 60_000, // base fetched a minute before the poll
      pollAt,
    )
    expect(state).toBeNull()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
    // After the invalidation refetch: a base NEWER than the observation is trusted
    // verbatim, whatever it says.
    expect(
      deriveTileState(
        result({ library_state: 'available' }),
        [request({ status: 'failed' })],
        pollAt + 60_000,
        pollAt,
      ),
    ).toEqual({ label: 'In library', intent: 'available' })
  })

  it('trusts a long-ago settle with a fresher base, at the cost of one refetch', async () => {
    // A row settled in some earlier session, first seen by this session's first
    // poll, with the mount's discover response resolving AFTER that poll: the base
    // already folds the settle server-side — trusted immediately, no suppression
    // beat. The invalidation still fires ONCE (wave 7): gating it on the observing
    // call's own base freshness was wrong — receipt time overstates the server's
    // read time by up to one RTT, and an OLDER cached sibling discover query may
    // still predate the settle; with the once-per-row guard, a skipped invalidation
    // could never fire again, starving both of their only heal. One extra discover
    // refetch per settled row per session is the accepted cost.
    const invalidate = vi
      .spyOn(queryClient, 'invalidateQueries')
      .mockResolvedValue(undefined as never)
    const pollAt = Date.now() - 10_000
    const state = deriveTileState(
      result({ library_state: 'available' }),
      [request({ id: 1, status: 'available' }), request({ id: 2, status: 'failed' })],
      pollAt + 5_000, // base resolved after the poll snapshot
      pollAt,
    )
    expect(state).toEqual({ label: 'In library', intent: 'available' })
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
    expect(invalidate).toHaveBeenCalledTimes(1)
  })

  it('still invalidates when the base resolved within the receipt-time race window', async () => {
    // dataUpdatedAt is client RECEIPT time; the server read the request rows up to
    // one RTT earlier (statuses are read before the Plex presence crawl). A base
    // that resolved just AFTER the observation may still have been COMPUTED
    // pre-settle: the trust rule renders it for this beat (the documented error
    // bar), so the first observation MUST fire the invalidation regardless — the
    // refetch replaces a wrongly-trusted stale base within one cycle.
    const invalidate = vi
      .spyOn(queryClient, 'invalidateQueries')
      .mockResolvedValue(undefined as never)
    const pollAt = Date.now()
    const state = deriveTileState(
      result({ library_state: 'requested' }),
      [request({ status: 'failed' })],
      pollAt + 50, // resolved 50ms after the observation — inside one RTT
      pollAt,
    )
    // Rendering trusts the base for this beat (the error bar)...
    expect(state).toEqual({ label: 'Requested', intent: 'neutral' })
    // ...but the heal is queued unconditionally.
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
  })

  it('invalidates for a stale sibling cache even when the observing query is fresh', async () => {
    // Discover keeps home and per-search caches separate. The settle is first
    // observed while rendering with a FRESH query (base postdates the observation)
    // — but an OLDER cached sibling still predates the settle. The invalidation
    // must fire anyway: it marks ALL ['discover'] caches stale (react-query
    // refetches active queries immediately, inactive ones on next activation), and
    // the once-per-row guard means a skipped invalidation would never fire again —
    // the sibling would render its stale base suppressed forever.
    const invalidate = vi
      .spyOn(queryClient, 'invalidateQueries')
      .mockResolvedValue(undefined as never)
    const pollAt = Date.now()
    const rows = [request({ status: 'failed' })]
    // First observation happens while rendering the FRESH query's tile.
    deriveTileState(result({ library_state: 'none' }), rows, pollAt + 60_000, pollAt)
    // The stale sibling's tile renders suppressed (its base predates the settle)...
    expect(
      deriveTileState(result({ library_state: 'requested' }), rows, pollAt - 60_000, pollAt),
    ).toBeNull()
    // ...and the invalidation was queued by the first observation so it can heal.
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
    expect(invalidate).toHaveBeenCalledTimes(1)
  })

  it('fires the invalidation at most once per settled row per session', async () => {
    const invalidate = vi
      .spyOn(queryClient, 'invalidateQueries')
      .mockResolvedValue(undefined as never)
    const pollAt = Date.now()
    const tile = result({ library_state: 'requested' })
    const rows = [request({ status: 'failed' })]
    // First sighting with an old base: suppressed + invalidation queued.
    expect(deriveTileState(tile, rows, pollAt - 60_000, pollAt)).toBeNull()
    // Re-derives (same settled row) never re-fire it, whatever the base age.
    deriveTileState(tile, rows, pollAt - 60_000, pollAt)
    deriveTileState(tile, rows, pollAt + 60_000, pollAt)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(invalidate).toHaveBeenCalledTimes(1)
  })

  it('re-arms the observation when a settled row goes active again (report-issue)', () => {
    // ADR-0014 report-issue re-arms a settled row to an ACTIVE status (same id).
    // Seeing it active clears the old observation; a later settle is a new event
    // against whatever base is current.
    const tile = result({ library_state: 'requested' })
    observeSettle(tile, [request({ status: 'downloading' })], [request({ status: 'failed' })])
    // Re-armed: active again — the overlay simply wins.
    expect(deriveTileState(tile, [request({ status: 'searching' })])).toEqual({
      label: 'Searching',
      intent: 'searching',
    })
    // Settles again; a base refetched after THIS settle is still trusted.
    expect(
      deriveTileState(tile, [request({ status: 'failed' })], BASE_AFTER_SETTLE()),
    ).toEqual({ label: 'Requested', intent: 'neutral' })
  })

  it('invalidates the discover queries when a settle is first observed', async () => {
    const invalidate = vi
      .spyOn(queryClient, 'invalidateQueries')
      .mockResolvedValue(undefined as never)
    observeSettle(
      result({ library_state: 'requested' }),
      [request({ status: 'downloading' })],
      [request({ status: 'failed' })],
    )
    // The invalidation is deferred to a microtask (deriveTileState runs in render).
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
    expect(invalidate).toHaveBeenCalledTimes(1)
    // Re-deriving with the same settled rows does not re-fire it.
    deriveTileState(result({ library_state: 'requested' }), [request({ status: 'failed' })])
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(invalidate).toHaveBeenCalledTimes(1)
  })
})

describe('deriveTileState — movie re-request contradiction (pre-settle base only)', () => {
  it('drops the "available" base when a movie re-request beside an older available row fails', () => {
    // The movie create path NEVER creates a second row while Plex still has the
    // title (its fresh is_available(use_cache=False) check dedups to the in-library
    // row instead) — so a newer request coexisting with an older `available` row
    // proves the title read ABSENT at create time. When that re-request is observed
    // settling, the pre-settle `available` base is stale history: unbadge.
    const state = observeSettle(
      result({ library_state: 'available' }),
      [request({ id: 1, status: 'available' }), request({ id: 2, status: 'downloading' })],
      [request({ id: 1, status: 'available' }), request({ id: 2, status: 'failed' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toBeNull()
  })

  it('drops the "available" base when the observed movie re-request is cancelled', () => {
    const state = observeSettle(
      result({ library_state: 'available' }),
      [request({ id: 1, status: 'available' }), request({ id: 2, status: 'pending' })],
      [request({ id: 1, status: 'available' }), request({ id: 2, status: 'cancelled' })],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toBeNull()
  })

  it('keeps the tv "available" base when a season re-request fails', () => {
    // TV is deliberately NOT covered by the movie contradiction rule: a season-level
    // re-request (e.g. a newly aired season) is legitimately created while the show
    // remains partially/fully present (_present_seasons_or_empty only dedups when
    // EVERY requested season is present), so its failure says nothing about the
    // seasons already on disk.
    const state = observeSettle(
      result({ tmdb_id: 7, media_type: 'tv', library_state: 'available' }),
      [
        request({ id: 1, tmdb_id: 7, media_type: 'tv', status: 'available' }),
        request({ id: 2, tmdb_id: 7, media_type: 'tv', status: 'downloading' }),
      ],
      [
        request({ id: 1, tmdb_id: 7, media_type: 'tv', status: 'available' }),
        request({ id: 2, tmdb_id: 7, media_type: 'tv', status: 'failed' }),
      ],
      BASE_BEFORE_SETTLE(),
    )
    expect(state).toEqual({ label: 'In library', intent: 'available' })
  })
})

describe('deriveTileState — movie/tv correlation isolation', () => {
  it('does not apply a tv request to a movie tile with the same tmdb_id', () => {
    const state = deriveTileState(
      result({ tmdb_id: 42, media_type: 'movie', library_state: 'none' }),
      [request({ tmdb_id: 42, media_type: 'tv', status: 'downloading' })],
    )
    // The movie tile ignores the tv request entirely -> falls back to its own base.
    expect(state).toBeNull()
  })

  it('maps a partially_available request to the partial badge', () => {
    const state = deriveTileState(
      result({ tmdb_id: 5, media_type: 'tv', library_state: 'none' }),
      [request({ tmdb_id: 5, media_type: 'tv', status: 'partially_available' })],
    )
    expect(state).toEqual({ label: 'Partially available', intent: 'available' })
  })
})
