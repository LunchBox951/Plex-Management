/**
 * The optional pre-init hardening token (`PLEX_MANAGER_SETUP_TOKEN`, ADR-0016).
 * Held in `sessionStorage` for the duration of the setup tab so the wizard can
 * echo it as `X-Setup-Token` on every setup call; an in-memory fallback keeps it
 * usable when storage is unavailable (private mode / locked-down browsers).
 *
 * This is NOT the app recovery key. The recovery key (`X-Api-Key`) is never
 * persisted in the browser at all: the break-glass flow exchanges it once for the
 * HTTP-only session cookie (`POST /api/v1/auth/api-key`), so nothing JS-readable
 * ever holds it — closing CodeQL #263. The setup token, by contrast, is a
 * short-lived first-run gate, not a credential, and only ever leaves this tab as
 * the `X-Setup-Token` header while the install is still uninitialized.
 */
const SETUP_STORAGE_KEY = 'plexmgr.setupToken'

let memorySetupToken: string | null = null

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
