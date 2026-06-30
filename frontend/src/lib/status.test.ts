import { describe, expect, it } from 'vitest'
import { downloadStatus, requestStatus } from './status'

describe('requestStatus', () => {
  it('maps known request statuses to labels + intents', () => {
    expect(requestStatus('downloading')).toEqual({ label: 'Downloading', intent: 'downloading' })
    expect(requestStatus('no_acceptable_release')).toEqual({ label: 'No release', intent: 'error' })
    expect(requestStatus('available')).toEqual({ label: 'In library', intent: 'available' })
  })

  it('falls back to a humanized neutral label for unknown values', () => {
    expect(requestStatus('some_new_state')).toEqual({ label: 'Some new state', intent: 'neutral' })
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
