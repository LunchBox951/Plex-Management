import { describe, expect, it } from 'vitest'
import { downloadStatus, glyphKind, isInFlightRequestStatus, requestStatus } from './status'

describe('requestStatus', () => {
  it('maps known request statuses to labels + intents', () => {
    expect(requestStatus('downloading')).toEqual({ label: 'Downloading', intent: 'downloading' })
    expect(requestStatus('no_acceptable_release')).toEqual({ label: 'No release', intent: 'error' })
    expect(requestStatus('available')).toEqual({ label: 'In library', intent: 'available' })
  })

  it('falls back to a humanized neutral label for unknown values', () => {
    expect(requestStatus('some_new_state')).toEqual({ label: 'Some new state', intent: 'neutral' })
  })

  it('maps the tv-only rollup partially_available to the available intent', () => {
    expect(requestStatus('partially_available')).toEqual({
      label: 'Partially available',
      intent: 'available',
    })
  })

  it('maps evicted (ADR-0012) to a neutral intent, never error', () => {
    expect(requestStatus('evicted')).toEqual({ label: 'Evicted', intent: 'neutral' })
  })
})

describe('downloadStatus', () => {
  it('maps download states, including post-download work', () => {
    expect(downloadStatus('importing')).toEqual({ label: 'Importing', intent: 'downloading' })
    expect(downloadStatus('imported')).toEqual({ label: 'Imported', intent: 'available' })
    expect(downloadStatus('client_missing')).toEqual({ label: 'Client missing', intent: 'error' })
  })

  it('never throws on an unrecognized state', () => {
    expect(downloadStatus('weird').intent).toBe('neutral')
  })
})

describe('isInFlightRequestStatus', () => {
  it('counts the actively-worked statuses that drive the Requests nav badge', () => {
    for (const status of ['searching', 'downloading', 'no_acceptable_release']) {
      expect(isInFlightRequestStatus(status)).toBe(true)
    }
  })

  it('excludes not-yet-started, settled, and terminal-error statuses', () => {
    for (const status of [
      'pending',
      'waiting_for_air_date',
      'completed',
      'available',
      'partially_available',
      'import_blocked',
      'failed',
      'cancelled',
      'evicted',
    ]) {
      expect(isInFlightRequestStatus(status)).toBe(false)
    }
  })

  it('treats an unknown status as not in flight rather than throwing', () => {
    expect(isInFlightRequestStatus('some_new_state')).toBe(false)
  })
})

/**
 * `glyphKind` (issue #135) is `TileStatusGlyph`'s StatusPresentation -> icon
 * mapping. Bare `StatusIntent` only has five buckets, but the tile needs six
 * distinct pictograms — these tests pin the two label-based splits that make
 * that possible, and that the other four kinds stay a pure function of intent.
 */
describe('glyphKind', () => {
  it('renders a full check for "In library" but a distinct partial glyph for the tv rollup', () => {
    expect(glyphKind(requestStatus('available'))).toBe('available')
    // Same `available` intent as above — must NOT collapse to the same glyph.
    expect(glyphKind(requestStatus('partially_available'))).toBe('partial')
  })

  it('renders the plain pending clock for "Requested" (pending), not the active-search pulse', () => {
    expect(glyphKind(requestStatus('pending'))).toBe('pending')
  })

  it('renders the pending clock (not the pulse) for the Discover-only "processing" fallback', () => {
    // libraryStateToPresentation's processing case (tileState.ts): same label
    // as pending ("Requested") but intent 'searching' — must still read as
    // "waiting", not "actively searching".
    expect(glyphKind({ label: 'Requested', intent: 'searching' })).toBe('pending')
  })

  it('renders the active-search pulse only for a genuine "Searching" status', () => {
    expect(glyphKind(requestStatus('searching'))).toBe('searching')
  })

  it('renders the downloading glyph regardless of which downloading-intent label produced it', () => {
    expect(glyphKind(requestStatus('downloading'))).toBe('downloading')
    expect(glyphKind(requestStatus('completed'))).toBe('downloading') // "Finalizing"
  })

  it('renders the error glyph for both no-release and import-blocked', () => {
    expect(glyphKind(requestStatus('no_acceptable_release'))).toBe('error')
    expect(glyphKind(requestStatus('import_blocked'))).toBe('error')
  })

  it('falls back to the pending clock for an unrecognized neutral status', () => {
    expect(glyphKind(requestStatus('some_new_state'))).toBe('pending')
  })
})
