/**
 * The app API key is minted exactly once by `POST /setup/complete` and shown to
 * the operator one time. We persist it in localStorage and attach it as
 * `X-Api-Key` on every request (see api/client.ts). In dev with
 * `PLEX_MANAGER_DEV_AUTH_BYPASS=true` the backend ignores it, so an empty store
 * is fine locally.
 *
 * An in-memory fallback (`memoryKey`) keeps the key usable for the session even
 * when localStorage is unavailable (private mode / locked-down browsers) — without
 * it, a failed write would silently drop the key and every request would go out
 * unauthenticated with no way to recover.
 */
const STORAGE_KEY = 'plexmgr.apiKey'

let memoryKey: string | null = null

export function getApiKey(): string | null {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (stored !== null) return stored
  } catch {
    /* storage unreadable — fall through to the in-memory copy */
  }
  return memoryKey
}

export function setApiKey(key: string): void {
  memoryKey = key
  try {
    localStorage.setItem(STORAGE_KEY, key)
  } catch {
    /* private-mode / storage-disabled: the in-memory copy carries the session */
  }
}

export function clearApiKey(): void {
  memoryKey = null
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

export function hasApiKey(): boolean {
  return getApiKey() !== null
}
