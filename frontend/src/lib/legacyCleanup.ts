/**
 * Migrates the pre-session recovery-key remnant (CodeQL #263) onto the current
 * cookie-based session, then scrubs the cleartext value.
 *
 * The old break-glass flow persisted the raw `X-Api-Key` in localStorage under
 * `plexmgr.apiKey` (plus a `plexmgr.apiKeyActive` flag) and attached it as a
 * header *only while that flag was set*. So the key could be one of two things:
 *   - the *active* credential (flag set) for a browser that authenticated by
 *     header on every request and never held a session cookie, or
 *   - a *dormant* recovery copy (flag unset) kept purely for break-glass, while
 *     the browser was actually signed in as a Plex user via a session cookie.
 *
 * That second case is the trap: redeeming a dormant key through the exchange
 * endpoint mints a `user_id=NULL` api-key session and overwrites the live
 * cookie, silently dropping the signed-in Plex user's identity for the tab so
 * later requests run as the anonymous break-glass admin. So before touching the
 * key we ask the server who this browser already is (`GET /api/v1/auth/me`,
 * which authenticates by cookie only — the client never attaches the localStorage
 * key). If a valid non-anonymous session already carries the browser, the key is
 * redundant: we scrub the cleartext remnant WITHOUT exchanging, protecting the
 * live identity.
 *
 * Only when there is no session to protect do we redeem the key, through the
 * same one-shot endpoint the manual recovery form uses (`POST /api/v1/auth/api-key`,
 * see `KeyEntry.tsx`), to mint the HTTP-only session cookie — exchange-then-purge,
 * not purge-on-sight. Deleting the key before a cookie exists would strand an
 * active-key browser: the local copy is the only one, and the new build never
 * re-derives it. A failed exchange (network hiccup, an already-revoked key)
 * leaves the stored value untouched so this retries on the next load instead of
 * silently logging the browser out. If the session probe itself can't reach the
 * server, we do nothing at all and retry next load: redeeming blind could clobber
 * an unverified live session, and purging blind could strand an active-key
 * browser. No legacy key present is the steady-state no-op this always was.
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

  // Cookie-only session probe. A live session here (Plex or otherwise) is an
  // identity we must not overwrite with an anonymous api-key session.
  let authenticated: boolean
  try {
    const { data } = await client.GET('/api/v1/auth/me')
    authenticated = data?.authenticated === true
  } catch {
    // Server unreachable: state unknown. Redeeming blind risks clobbering a live
    // session; purging blind risks stranding an active-key browser. Do neither
    // and let the next load retry once the server answers.
    return
  }

  if (authenticated) {
    // A valid non-anonymous session already carries this browser, so the stored
    // key is a redundant break-glass copy. Scrub the cleartext remnant (the
    // CodeQL #263 fix) WITHOUT exchanging — redeeming it would mint a
    // `user_id=NULL` session and drop the signed-in identity for this tab.
    purgeStoredKey()
    return
  }

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

  purgeStoredKey()
}

function purgeStoredKey(): void {
  try {
    localStorage.removeItem(LEGACY_API_KEY)
    localStorage.removeItem(LEGACY_API_KEY_ACTIVE)
  } catch {
    /* private-mode / storage-disabled: the session cookie is already live */
  }
}
