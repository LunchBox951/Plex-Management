/**
 * The app API key is minted exactly once by `POST /setup/complete` and shown to
 * the operator one time. We persist it in localStorage and attach it as
 * `X-Api-Key` on every request (see api/client.ts). In dev with
 * `PLEX_MANAGER_DEV_AUTH_BYPASS=true` the backend ignores it, so an empty store
 * is fine locally.
 */
const STORAGE_KEY = 'plexmgr.apiKey'

export function getApiKey(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

export function setApiKey(key: string): void {
  try {
    localStorage.setItem(STORAGE_KEY, key)
  } catch {
    /* private-mode / storage-disabled: requests just go out without the header */
  }
}

export function clearApiKey(): void {
  try {
    localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}

export function hasApiKey(): boolean {
  return getApiKey() !== null
}
