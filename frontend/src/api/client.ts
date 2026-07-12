/**
 * Typed API client (ADR-0009).
 *
 * Wraps `openapi-fetch` with the generated `paths` so every call is checked
 * against the backend's exported OpenAPI contract. Two cross-cutting concerns
 * live here as middleware:
 *   - attach the stored `X-Api-Key` to every request;
 *   - turn the backend's honest guard responses into app-level signals
 *     (409 `setup_required` -> drive the wizard; 401 -> the stored key is stale).
 */
import createClient, { type Middleware } from 'openapi-fetch'
import type { paths } from './schema'
import {
  clearApiKey,
  getApiKey,
  getSetupToken,
  isApiKeyAuthEnabled,
} from '../lib/apiKey'
import { getPendingApiKeyRotation } from '../lib/apiKeyRotation'

/** Fired when any call returns 409 `setup_required`; the shell routes to /setup. */
export const SETUP_REQUIRED_EVENT = 'plexmgr:setup-required'
/** Fired when any call returns 401 `invalid_api_key`; the shell re-runs setup. */
export const AUTH_INVALID_EVENT = 'plexmgr:auth-invalid'
/**
 * Fired when a call is rejected 401 while relying on the browser SESSION cookie
 * (no current api key was sent). The signed-in Plex session is missing, expired,
 * or revoked; the shell refetches auth state and routes back to the Plex login
 * instead of stranding stale "authenticated" UI on error states.
 */
export const AUTH_EXPIRED_EVENT = 'plexmgr:auth-expired'

function emit(event: string): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(event))
  }
}

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const key = isApiKeyAuthEnabled() ? getApiKey() : null
    if (key) {
      request.headers.set('X-Api-Key', key)
    }
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
  onResponse({ request, response }) {
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
      // Only react if the key THIS request used is still the current one. A slow
      // 401 from an earlier request that used a now-replaced key must not clobber
      // a freshly pasted/valid key and undo recovery (#139).
      const sentKey = request.headers.get('X-Api-Key')
      if (sentKey) {
        rejectApiKeyIfCurrentAfterRotation(sentKey)
      } else {
        // No current api key rode this request, yet it was rejected: the request
        // was relying on the browser session cookie, which is now missing/expired/
        // revoked. Signal so the gate re-checks auth and shows the login again.
        emit(AUTH_EXPIRED_EVENT)
      }
      // else: sentKey is set but stale (rotated out mid-flight) — this response
      // says nothing about the current key or the session, so ignore it entirely.
    }
    return response
  },
}

function rejectApiKeyIfCurrentAfterRotation(sentKey: string): void {
  const rotation = getPendingApiKeyRotation(sentKey)
  if (rotation !== null) {
    // Do not return this promise to openapi-fetch: the rotate request's own 401
    // must be allowed to reach the mutation so its finally block can release the
    // barrier. Re-enter afterward to cover a back-to-back rotation of this key.
    void rotation.then(() => rejectApiKeyIfCurrentAfterRotation(sentKey))
    return
  }
  if (sentKey === getApiKey()) {
    clearApiKey()
    emit(AUTH_INVALID_EVENT)
  }
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
