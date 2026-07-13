/**
 * One-time scrub of the pre-session recovery-key remnants (CodeQL #263).
 *
 * The old break-glass flow persisted the raw `X-Api-Key` in localStorage under
 * `plexmgr.apiKey` (plus a `plexmgr.apiKeyActive` flag) so it survived a reload.
 * The current flow exchanges the key ONCE for the HTTP-only session cookie and
 * never writes it to storage — but browsers already on the beta fleet still hold
 * that cleartext secret at rest from the old build. Merely not writing it anymore
 * doesn't remove what's there, so this deletes the stale values on every app
 * init. It's idempotent (removing an absent key is a no-op) and cheap enough to
 * run unconditionally, which also covers a browser that downgraded and re-upgraded
 * across the change.
 */
const LEGACY_API_KEY = 'plexmgr.apiKey'
const LEGACY_API_KEY_ACTIVE = 'plexmgr.apiKeyActive'

export function purgeLegacyApiKey(): void {
  try {
    localStorage.removeItem(LEGACY_API_KEY)
    localStorage.removeItem(LEGACY_API_KEY_ACTIVE)
  } catch {
    /* private-mode / storage-disabled: there was nothing persistable to leak */
  }
}
