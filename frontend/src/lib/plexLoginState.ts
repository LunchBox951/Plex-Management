const STORAGE_KEY = 'plexmgr.plexLoginState'

export function rememberPlexLoginState(state: string): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, state)
  } catch {
    /* storage-disabled browsers can still return with state in the callback URL */
  }
}

export function readRememberedPlexLoginState(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

export function clearRememberedPlexLoginState(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}
