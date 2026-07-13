import { afterEach, describe, expect, it, vi } from 'vitest'
import { purgeLegacyApiKey } from './legacyCleanup'

// localStorage isn't provided by this jsdom config (see plexOAuth.test.ts), so
// each test stubs a real in-memory store to pin the exact key names being scrubbed.
function stubStore(initial: Record<string, string> = {}): Record<string, string> {
  const store = { ...initial }
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => store[key] ?? null,
    setItem: (key: string, value: string) => {
      store[key] = value
    },
    removeItem: (key: string) => {
      delete store[key]
    },
  })
  return store
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('purgeLegacyApiKey', () => {
  it('removes the cleartext recovery key and its active flag left by the old flow', () => {
    const store = stubStore({
      'plexmgr.apiKey': 'super-secret-recovery-key',
      'plexmgr.apiKeyActive': 'true',
    })

    purgeLegacyApiKey()

    expect(store['plexmgr.apiKey']).toBeUndefined()
    expect(store['plexmgr.apiKeyActive']).toBeUndefined()
  })

  it('leaves unrelated keys untouched', () => {
    const store = stubStore({ 'plexmgr.plexClientId': 'client-123' })

    purgeLegacyApiKey()

    expect(store['plexmgr.plexClientId']).toBe('client-123')
  })

  it('is a no-op when nothing was stored', () => {
    const store = stubStore()

    expect(() => purgeLegacyApiKey()).not.toThrow()
    expect(store['plexmgr.apiKey']).toBeUndefined()
  })

  it('swallows a storage-disabled failure (private mode) without throwing', () => {
    vi.stubGlobal('localStorage', {
      removeItem: () => {
        throw new Error('storage disabled')
      },
    })

    expect(() => purgeLegacyApiKey()).not.toThrow()
  })
})
