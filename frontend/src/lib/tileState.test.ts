import { describe, expect, it } from 'vitest'
import type { DiscoverResult, RequestResponse } from '../api/types'
import { deriveTileState } from './tileState'

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
    ...overrides,
  }
}

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
    // it falls through to the server base (In library).
    const state = deriveTileState(
      result({ library_state: 'available' }),
      [request({ status: 'failed' })],
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

describe('deriveTileState — settled row degrades a stale request-derived base', () => {
  // The server base was computed at page load; the live poll shows the request has
  // since settled. The stale request-derived base must NOT fall through as "Requested".
  it('degrades a stale "requested" base to none when the live row failed', () => {
    const state = deriveTileState(
      result({ library_state: 'requested' }),
      [request({ status: 'failed' })],
    )
    expect(state).toBeNull()
  })

  it('degrades a stale "processing" base to none when the live row cancelled', () => {
    const state = deriveTileState(
      result({ library_state: 'processing' }),
      [request({ status: 'cancelled' })],
    )
    expect(state).toBeNull()
  })

  it('keeps presence-derived "available" through a settled failed row', () => {
    // Library presence is independent of the request lifecycle — failed doesn't evict.
    const state = deriveTileState(
      result({ library_state: 'available' }),
      [request({ status: 'failed' })],
    )
    expect(state).toEqual({ label: 'In library', intent: 'available' })
  })

  it('degrades a stale "partially_available" base to none when the live row failed', () => {
    // `partially_available` is ONLY ever request-derived (the server's presence crawl
    // is a whole-title boolean — see derive_library_state), so a settled row proves it
    // stale just like `requested`/`processing`: e.g. the last available season was
    // reported and the replacement grab failed. Unbadge; don't keep the dead rollup.
    const state = deriveTileState(
      result({ tmdb_id: 7, media_type: 'tv', library_state: 'partially_available' }),
      [request({ tmdb_id: 7, media_type: 'tv', status: 'failed' })],
    )
    expect(state).toBeNull()
  })

  it('degrades a stale "partially_available" base to none when the live row cancelled', () => {
    const state = deriveTileState(
      result({ tmdb_id: 7, media_type: 'tv', library_state: 'partially_available' }),
      [request({ tmdb_id: 7, media_type: 'tv', status: 'cancelled' })],
    )
    expect(state).toBeNull()
  })

  it('drops even a stale "available" base when the live row is evicted', () => {
    // ADR-0012: eviction DELETED the file, contradicting the page-load presence
    // snapshot. The fresher live evicted row wins — degrade to unbadged, don't claim
    // a file that was just deleted.
    const state = deriveTileState(
      result({ library_state: 'available' }),
      [request({ status: 'evicted' })],
    )
    expect(state).toBeNull()
  })

  it('drops a stale "partially_available" base when the live row is evicted', () => {
    const state = deriveTileState(
      result({ tmdb_id: 7, media_type: 'tv', library_state: 'partially_available' }),
      [request({ tmdb_id: 7, media_type: 'tv', status: 'evicted' })],
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
