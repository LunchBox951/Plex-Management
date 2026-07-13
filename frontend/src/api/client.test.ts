import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setSetupToken, clearSetupToken } from '../lib/setupToken'
import { AUTH_EXPIRED_EVENT, SETUP_REQUIRED_EVENT } from './client'

const h = vi.hoisted(() => ({
  middleware: null as null | {
    onRequest(args: { request: Request }): Request | undefined
    onResponse(args: { request: Request; response: Response }): Response | undefined
  },
}))

vi.mock('openapi-fetch', () => ({
  default: vi.fn(() => ({
    use: vi.fn((middleware) => {
      h.middleware = middleware
    }),
  })),
}))

function middleware() {
  if (!h.middleware) throw new Error('client middleware was not registered')
  return h.middleware
}

function clearCookie(name: string): void {
  document.cookie = `${name}=; Max-Age=0; path=/`
}

function createStorage(): Storage {
  let values: Record<string, string> = {}
  return {
    get length() {
      return Object.keys(values).length
    },
    clear: () => {
      values = {}
    },
    getItem: (key: string) => values[key] ?? null,
    key: (index: number) => Object.keys(values)[index] ?? null,
    removeItem: (key: string) => {
      delete values[key]
    },
    setItem: (key: string, value: string) => {
      values[key] = value
    },
  }
}

describe('API auth middleware', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', createStorage())
    vi.stubGlobal('sessionStorage', createStorage())
    clearSetupToken()
    clearCookie('plexmgr.csrf')
  })

  it('never attaches an X-Api-Key header — the browser is cookie-only (CodeQL #263)', () => {
    const request = new Request('http://localhost/api/v1/settings')

    middleware().onRequest({ request })

    expect(request.headers.get('X-Api-Key')).toBeNull()
  })

  it('attaches the pre-init setup token when one is held', () => {
    setSetupToken('boot-token')
    const request = new Request('http://localhost/api/v1/setup/plex/servers')

    middleware().onRequest({ request })

    expect(request.headers.get('X-Setup-Token')).toBe('boot-token')
  })

  it('adds CSRF from the readable cookie on unsafe session requests', () => {
    document.cookie = 'plexmgr.csrf=csrf-token'
    const request = new Request('http://localhost/api/v1/settings', { method: 'PUT' })

    middleware().onRequest({ request })

    expect(request.headers.get('X-CSRF-Token')).toBe('csrf-token')
  })

  it('does not add CSRF on safe methods', () => {
    document.cookie = 'plexmgr.csrf=csrf-token'
    const request = new Request('http://localhost/api/v1/settings', { method: 'GET' })

    middleware().onRequest({ request })

    expect(request.headers.get('X-CSRF-Token')).toBeNull()
  })

  it('signals an expired session on a bodyless 401 (the cookie is the only credential)', async () => {
    const expired = vi.fn()
    window.addEventListener(AUTH_EXPIRED_EVENT, expired)
    const request = new Request('http://localhost/api/v1/settings')

    middleware().onResponse({ request, response: new Response(null, { status: 401 }) })

    await vi.waitFor(() => expect(expired).toHaveBeenCalledTimes(1))
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired)
  })

  it('signals an expired session on an ordinary detail-carrying 401', async () => {
    const expired = vi.fn()
    window.addEventListener(AUTH_EXPIRED_EVENT, expired)
    const request = new Request('http://localhost/api/v1/settings')
    const response = new Response(JSON.stringify({ detail: 'invalid_api_key' }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    })

    middleware().onResponse({ request, response })

    await vi.waitFor(() => expect(expired).toHaveBeenCalledTimes(1))
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired)
  })

  it('does NOT signal expiry on a rejected recovery-key exchange (issue #293)', async () => {
    // A mistyped break-glass key at POST /auth/api-key returns 401
    // `recovery_key_rejected`. KeyEntry handles it locally; firing the global
    // "session expired" signal would yank the operator off the key screen back to
    // Plex login. The middleware must stay silent for that one code.
    const expired = vi.fn()
    window.addEventListener(AUTH_EXPIRED_EVENT, expired)
    const request = new Request('http://localhost/api/v1/auth/api-key', { method: 'POST' })
    const response = new Response(JSON.stringify({ detail: 'recovery_key_rejected' }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    })

    middleware().onResponse({ request, response })

    // Give the async body-parse a chance to run, then assert it never emitted.
    await Promise.resolve()
    await Promise.resolve()
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(expired).not.toHaveBeenCalled()
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired)
  })

  it('signals setup-required on a 409 setup_required body', async () => {
    const setupRequired = vi.fn()
    window.addEventListener(SETUP_REQUIRED_EVENT, setupRequired)
    const request = new Request('http://localhost/api/v1/settings')
    const response = new Response(JSON.stringify({ detail: 'setup_required', setup_path: '/setup' }), {
      status: 409,
      headers: { 'content-type': 'application/json' },
    })

    middleware().onResponse({ request, response })

    await vi.waitFor(() => expect(setupRequired).toHaveBeenCalledTimes(1))
    window.removeEventListener(SETUP_REQUIRED_EVENT, setupRequired)
  })
})
