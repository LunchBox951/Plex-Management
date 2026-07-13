/**
 * Migrates the pre-session recovery-key remnant (CodeQL #263) onto the current
 * cookie-based session, then scrubs the cleartext value.
 *
 * The old break-glass flow persisted the raw `X-Api-Key` in localStorage under
 * `plexmgr.apiKey` (plus a `plexmgr.apiKeyActive` flag) and attached it as a
 * header on every request, so it was the *ongoing* credential for any browser
 * still running that build — e.g. a beta-fleet tab open across the deploy, or
 * one that simply hasn't gotten a session cookie yet. Deleting that key before
 * the browser actually has a cookie would strand it with no recoverable
 * credential: the local copy was the only one, and the new build never reads
 * or re-derives it.
 *
 * So this is exchange-then-purge, not purge-on-sight: if a legacy key is
 * present, it's first redeemed through the same one-shot endpoint the manual
 * recovery form uses (`POST /api/v1/auth/api-key`, see `KeyEntry.tsx`) to mint
 * the HTTP-only session cookie. Only once that exchange succeeds — the only
 * signal JS can get, since the cookie itself is HttpOnly and unreadable here —
 * do we clear localStorage. A failed exchange (network hiccup, an
 * already-revoked key) leaves the stored value untouched so this can retry on
 * the next load instead of silently logging the browser out. No legacy key
 * present is the steady-state no-op this always was.
 */
import { client } from '../api/client'
import { ensureOk } from '../api/http'

const LEGACY_API_KEY = 'plexmgr.apiKey'
const LEGACY_API_KEY_ACTIVE = 'plexmgr.apiKeyActive'

export async function purgeLegacyApiKey(): Promise<void> {
  let legacyKey: string | null
  try {
    legacyKey = localStorage.getItem(LEGACY_API_KEY)
  } catch {
    return // private-mode / storage-disabled: nothing persistable was ever at risk
  }
  if (!legacyKey) return // nothing to migrate: matches the prior idempotent no-op

  try {
    ensureOk(
      await client.POST('/api/v1/auth/api-key', {
        headers: { 'X-Api-Key': legacyKey },
      }),
    )
  } catch {
    // Exchange rejected or unreachable: leave localStorage exactly as it was.
    // Purging here — with no cookie minted — would strand this browser.
    return
  }

  try {
    localStorage.removeItem(LEGACY_API_KEY)
    localStorage.removeItem(LEGACY_API_KEY_ACTIVE)
  } catch {
    /* private-mode / storage-disabled: the session cookie is already live */
  }
}
