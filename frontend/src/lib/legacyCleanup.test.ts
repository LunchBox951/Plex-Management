import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { purgeLegacyApiKey } from './legacyCleanup'
import { client } from '../api/client'

// No network: the typed client is replaced with controllable GET/POST mocks, same
// pattern as api/hooks.test.tsx. GET backs the `/auth/me` session probe; POST backs
// the recovery-key exchange.
vi.mock('../api/client', () => ({
  client: { GET: vi.fn(), POST: vi.fn() },
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

/** Answer the `/auth/me` probe with a given session state (HTTP 200 either way). */
function stubAuthMe(authenticated: boolean, authMethod: string | null = null): void {
  vi.mocked(client.GET).mockResolvedValue({
    data: { authenticated, auth_method: authMethod, is_admin: false, user: null },
    response: new Response(null, { status: 200 }),
  } as never)
}

beforeEach(() => {
  vi.mocked(client.GET).mockReset()
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
    stubAuthMe(false)
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

  it('does not redeem a dormant key over a live Plex session — it only scrubs the cleartext', async () => {
    // The trap: a browser signed in as a Plex user (session cookie) that still
    // carries the raw recovery key in localStorage with the active flag UNSET.
    // Redeeming it would mint a user_id=NULL api-key session and silently drop
    // the signed-in Plex identity for this tab.
    const store = stubStore({ 'plexmgr.apiKey': 'dormant-recovery-key' })
    stubAuthMe(true, 'plex_session')

    await purgeLegacyApiKey()

    // Never exchanged: the live session is left intact.
    expect(client.POST).not.toHaveBeenCalled()
    // But the CodeQL #263 cleartext remnant is still scrubbed.
    expect(store['plexmgr.apiKey']).toBeUndefined()
  })

  it('leaves storage intact when the session probe is unreachable (retries next load)', async () => {
    const store = stubStore({ 'plexmgr.apiKey': 'super-secret-recovery-key' })
    vi.mocked(client.GET).mockRejectedValue(new TypeError('network error'))

    await purgeLegacyApiKey()

    // Unknown session state: neither exchange (would risk clobbering a live
    // session) nor purge (would risk stranding an active-key browser).
    expect(client.POST).not.toHaveBeenCalled()
    expect(store['plexmgr.apiKey']).toBe('super-secret-recovery-key')
  })

  it('does not touch storage when the exchange is rejected (a browser stays recoverable)', async () => {
    const store = stubStore({
      'plexmgr.apiKey': 'super-secret-recovery-key',
      'plexmgr.apiKeyActive': 'true',
    })
    stubAuthMe(false)
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
    stubAuthMe(false)
    vi.mocked(client.POST).mockRejectedValue(new TypeError('network error'))

    await purgeLegacyApiKey()

    expect(store['plexmgr.apiKey']).toBe('super-secret-recovery-key')
  })

  it('leaves unrelated keys untouched', async () => {
    const store = stubStore({
      'plexmgr.plexClientId': 'client-123',
      'plexmgr.apiKey': 'super-secret-recovery-key',
    })
    stubAuthMe(false)
    vi.mocked(client.POST).mockResolvedValue({
      data: { authenticated: true },
      response: new Response(null, { status: 200 }),
    })

    await purgeLegacyApiKey()

    expect(store['plexmgr.plexClientId']).toBe('client-123')
  })

  it('is a no-op — and never probes or exchanges — when nothing was stored', async () => {
    const store = stubStore()

    await expect(purgeLegacyApiKey()).resolves.toBeUndefined()

    expect(client.GET).not.toHaveBeenCalled()
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
    expect(client.GET).not.toHaveBeenCalled()
    expect(client.POST).not.toHaveBeenCalled()
  })
})
