import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearApiKey,
  enableApiKeyAuth,
  getApiKey,
  isApiKeyAuthEnabled,
  setApiKey,
} from '../lib/apiKey'
import { AUTH_INVALID_EVENT } from './client'

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
    clearApiKey()
    clearCookie('plexmgr.csrf')
  })

  it('does not attach a stored access key until recovery mode is enabled', () => {
    setApiKey('stored-key')
    const request = new Request('http://localhost/api/v1/settings')

    middleware().onRequest({ request })

    expect(request.headers.get('X-Api-Key')).toBeNull()
  })

  it('attaches the access key after explicit recovery-mode opt-in', () => {
    setApiKey('stored-key')
    enableApiKeyAuth()
    const request = new Request('http://localhost/api/v1/settings')

    middleware().onRequest({ request })

    expect(request.headers.get('X-Api-Key')).toBe('stored-key')
  })

  it('does not treat a session-only 401 as a rejected access key', () => {
    const listener = vi.fn()
    window.addEventListener(AUTH_INVALID_EVENT, listener)
    const request = new Request('http://localhost/api/v1/settings')

    middleware().onResponse({ request, response: new Response(null, { status: 401 }) })

    expect(listener).not.toHaveBeenCalled()
    window.removeEventListener(AUTH_INVALID_EVENT, listener)
  })

  it('clears and reports a rejected key only when that key was sent', () => {
    setApiKey('stored-key')
    enableApiKeyAuth()
    const listener = vi.fn()
    window.addEventListener(AUTH_INVALID_EVENT, listener)
    const request = new Request('http://localhost/api/v1/settings')
    middleware().onRequest({ request })

    middleware().onResponse({ request, response: new Response(null, { status: 401 }) })

    expect(listener).toHaveBeenCalledTimes(1)
    expect(getApiKey()).toBeNull()
    expect(isApiKeyAuthEnabled()).toBe(false)
    window.removeEventListener(AUTH_INVALID_EVENT, listener)
  })

  it('adds CSRF from the readable cookie on unsafe session requests', () => {
    document.cookie = 'plexmgr.csrf=csrf-token'
    const request = new Request('http://localhost/api/v1/settings', { method: 'PUT' })

    middleware().onRequest({ request })

    expect(request.headers.get('X-CSRF-Token')).toBe('csrf-token')
  })
})
