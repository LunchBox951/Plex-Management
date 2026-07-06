/**
 * The app API key is minted exactly once by `POST /setup/complete` and shown to
 * the operator one time. We persist it in localStorage for recovery, but the
 * browser client only attaches it after the operator explicitly chooses the
 * access-key path (see api/client.ts). In dev with
 * `PLEX_MANAGER_DEV_AUTH_BYPASS=true` the backend ignores it, so an empty store
 * is fine locally.
 *
 * An in-memory fallback (`memoryKey`) keeps the key usable for the session even
 * when localStorage is unavailable (private mode / locked-down browsers) — without
 * it, a failed write would silently drop the key and every request would go out
 * unauthenticated with no way to recover.
 */
const STORAGE_KEY = 'plexmgr.apiKey'
const SETUP_STORAGE_KEY = 'plexmgr.setupToken'
const ACTIVE_KEY = 'plexmgr.apiKeyActive'

let memoryKey: string | null = null
let memorySetupToken: string | null = null
let memoryActive = false

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

export function getSetupToken(): string | null {
  try {
    const stored = sessionStorage.getItem(SETUP_STORAGE_KEY)
    if (stored !== null) return stored
  } catch {
    /* storage unreadable — fall through to the in-memory copy */
  }
  return memorySetupToken
}

export function setSetupToken(token: string): void {
  memorySetupToken = token
  try {
    sessionStorage.setItem(SETUP_STORAGE_KEY, token)
  } catch {
    /* private-mode / storage-disabled: the in-memory copy carries the session */
  }
}

export function clearSetupToken(): void {
  memorySetupToken = null
  try {
    sessionStorage.removeItem(SETUP_STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

export function clearApiKey(): void {
  memoryKey = null
  disableApiKeyAuth()
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

export function hasApiKey(): boolean {
  return getApiKey() !== null
}

export function enableApiKeyAuth(): void {
  memoryActive = true
  try {
    sessionStorage.setItem(ACTIVE_KEY, 'true')
  } catch {
    /* storage unavailable — the in-memory flag carries the tab */
  }
}

export function disableApiKeyAuth(): void {
  memoryActive = false
  try {
    sessionStorage.removeItem(ACTIVE_KEY)
  } catch {
    /* ignore */
  }
}

export function isApiKeyAuthEnabled(): boolean {
  try {
    if (sessionStorage.getItem(ACTIVE_KEY) === 'true') return true
  } catch {
    /* storage unreadable — fall through to the in-memory flag */
  }
  return memoryActive
}
