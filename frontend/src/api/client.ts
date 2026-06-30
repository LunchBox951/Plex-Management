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
import { clearApiKey, getApiKey } from '../lib/apiKey'

/** Fired when any call returns 409 `setup_required`; the shell routes to /setup. */
export const SETUP_REQUIRED_EVENT = 'plexmgr:setup-required'
/** Fired when any call returns 401 `invalid_api_key`; the shell re-runs setup. */
export const AUTH_INVALID_EVENT = 'plexmgr:auth-invalid'

function emit(event: string): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(event))
  }
}

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const key = getApiKey()
    if (key) {
      request.headers.set('X-Api-Key', key)
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
      // a freshly pasted/valid key and undo recovery.
      if (request.headers.get('X-Api-Key') === getApiKey()) {
        clearApiKey()
        emit(AUTH_INVALID_EVENT)
      }
    }
    return response
  },
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
