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

  it('signals an expired session on any 401 (the cookie is the only credential)', () => {
    const expired = vi.fn()
    window.addEventListener(AUTH_EXPIRED_EVENT, expired)
    const request = new Request('http://localhost/api/v1/settings')

    middleware().onResponse({ request, response: new Response(null, { status: 401 }) })

    expect(expired).toHaveBeenCalledTimes(1)
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
