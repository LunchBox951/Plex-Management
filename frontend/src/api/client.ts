/**
 * Typed API client (ADR-0009).
 *
 * Wraps `openapi-fetch` with the generated `paths` so every call is checked
 * against the backend's exported OpenAPI contract. The browser authenticates
 * SOLELY by the HTTP-only `plexmgr.session` cookie — minted either by Plex
 * sign-in or by exchanging the recovery key once (`POST /api/v1/auth/api-key`).
 * The raw `X-Api-Key` is never read into JS-managed state or attached per
 * request, so it never needs JS-readable storage (CodeQL #263). Two
 * cross-cutting concerns live here as middleware:
 *   - echo the readable `plexmgr.csrf` cookie as `X-CSRF-Token` on unsafe
 *     methods (the double-submit check the cookie session requires), and attach
 *     the pre-init `X-Setup-Token` while the setup wizard needs it;
 *   - turn the backend's honest guard responses into app-level signals
 *     (409 `setup_required` -> drive the wizard; 401 -> the session lapsed).
 */
import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from './schema'
import { getSetupToken } from '../lib/setupToken'

/** Fired when any call returns 409 `setup_required`; the shell routes to /setup. */
export const SETUP_REQUIRED_EVENT = 'plexmgr:setup-required'
/**
 * Fired when a cookie-authenticated call is rejected 401: the browser session
 * (Plex sign-in or a recovery-key exchange) is missing, expired, or revoked. The
 * shell refetches auth state and routes back to the login instead of stranding
 * stale "authenticated" UI on error states.
 */
export const AUTH_EXPIRED_EVENT = 'plexmgr:auth-expired'

function emit(event: string): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(event))
  }
}

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const setupToken = getSetupToken()
    if (setupToken) {
      request.headers.set('X-Setup-Token', setupToken)
    }
    const csrf = getCookie('plexmgr.csrf')
    if (csrf && isUnsafeMethod(request.method)) {
      request.headers.set('X-CSRF-Token', csrf)
    }
    return request
  },
  onResponse({ response }) {
    if (response.status === 409) {
      // Body shape: { detail: "setup_required", setup_path: "/setup" }. We only
      // need to know it happened; the route guard reads install state itself.
      void response
        .clone()
        .json()
        .then((body: unknown) => {
          if (isDetail(body, 'setup_required')) emit(SETUP_REQUIRED_EVENT)
        })
        .catch(() => undefined)
    } else if (response.status === 401) {
      // The request relied on the browser session cookie (the only browser
      // credential now), which is missing/expired/revoked. Signal so the gate
      // re-checks auth and shows the login again.
      emit(AUTH_EXPIRED_EVENT)
    }
    return response
  },
}

function isUnsafeMethod(method: string): boolean {
  return !['GET', 'HEAD', 'OPTIONS', 'TRACE'].includes(method.toUpperCase())
}

function getCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const prefix = `${name}=`
  return (
    document.cookie
      .split(';')
      .map((part) => part.trim())
      .find((part) => part.startsWith(prefix))
      ?.slice(prefix.length) ?? null
  )
}

function isDetail(body: unknown, detail: string): boolean {
  return (
    typeof body === 'object' &&
    body !== null &&
    'detail' in body &&
    (body as { detail: unknown }).detail === detail
  )
}

export const client = createClient<paths>({ baseUrl: '' })
client.use(authMiddleware)
