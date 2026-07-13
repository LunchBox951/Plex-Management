import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { purgeLegacyApiKey } from './legacyCleanup'
import { client } from '../api/client'

// No network: the typed client is replaced with a controllable POST mock, same
// pattern as api/hooks.test.tsx.
vi.mock('../api/client', () => ({
  client: { POST: vi.fn() },
}))

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

beforeEach(() => {
  vi.mocked(client.POST).mockReset()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('purgeLegacyApiKey', () => {
  it('exchanges a present legacy key for a session cookie before scrubbing it', async () => {
    const store = stubStore({
      'plexmgr.apiKey': 'super-secret-recovery-key',
      'plexmgr.apiKeyActive': 'true',
    })
    vi.mocked(client.POST).mockResolvedValue({
      data: { authenticated: true },
      response: new Response(null, { status: 200 }),
    })

    await purgeLegacyApiKey()

    expect(client.POST).toHaveBeenCalledWith('/api/v1/auth/api-key', {
      headers: { 'X-Api-Key': 'super-secret-recovery-key' },
    })
    expect(store['plexmgr.apiKey']).toBeUndefined()
    expect(store['plexmgr.apiKeyActive']).toBeUndefined()
  })

  it('does not touch storage when the exchange is rejected (a browser stays recoverable)', async () => {
    const store = stubStore({
      'plexmgr.apiKey': 'super-secret-recovery-key',
      'plexmgr.apiKeyActive': 'true',
    })
    vi.mocked(client.POST).mockResolvedValue({
      error: { detail: 'invalid_api_key' },
      response: new Response(null, { status: 401 }),
    })

    await purgeLegacyApiKey()

    expect(store['plexmgr.apiKey']).toBe('super-secret-recovery-key')
    expect(store['plexmgr.apiKeyActive']).toBe('true')
  })

  it('does not touch storage when the exchange call throws (network drop)', async () => {
    const store = stubStore({ 'plexmgr.apiKey': 'super-secret-recovery-key' })
    vi.mocked(client.POST).mockRejectedValue(new TypeError('network error'))

    await purgeLegacyApiKey()

    expect(store['plexmgr.apiKey']).toBe('super-secret-recovery-key')
  })

  it('leaves unrelated keys untouched', async () => {
    const store = stubStore({
      'plexmgr.plexClientId': 'client-123',
      'plexmgr.apiKey': 'super-secret-recovery-key',
    })
    vi.mocked(client.POST).mockResolvedValue({
      data: { authenticated: true },
      response: new Response(null, { status: 200 }),
    })

    await purgeLegacyApiKey()

    expect(store['plexmgr.plexClientId']).toBe('client-123')
  })

  it('is a no-op — and never calls the exchange — when nothing was stored', async () => {
    const store = stubStore()

    await expect(purgeLegacyApiKey()).resolves.toBeUndefined()

    expect(client.POST).not.toHaveBeenCalled()
    expect(store['plexmgr.apiKey']).toBeUndefined()
  })

  it('swallows a storage-disabled failure (private mode) without throwing', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('storage disabled')
      },
    })

    await expect(purgeLegacyApiKey()).resolves.toBeUndefined()
    expect(client.POST).not.toHaveBeenCalled()
  })
})
