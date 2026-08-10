import { afterEach, describe, expect, it } from 'vitest'
import {
  clearSessionCloseNotice,
  describeSessionClose,
  getSessionCloseNotice,
  noteSessionClose,
} from './sessionClose'

afterEach(() => {
  clearSessionCloseNotice()
})

describe('describeSessionClose', () => {
  it('gives a revoked share and a stale token DIFFERENT words', () => {
    const revoked = describeSessionClose('share_revalidation_share_revoked')
    const stale = describeSessionClose('share_revalidation_token_stale')

    expect(revoked.title).not.toBe(stale.title)
    // The stale-token message must never claim access was removed — plex.tv
    // rejected the credential before it could say anything about the share.
    expect(stale.message).toMatch(/was NOT removed/)
    expect(revoked.message).toMatch(/no longer has access/)
  })

  it('names an unrecognized reason instead of hiding it', () => {
    const notice = describeSessionClose('something_new_from_the_backend')

    expect(notice.reason).toBe('something_new_from_the_backend')
    expect(notice.message).toContain('something_new_from_the_backend')
    expect(notice.signedOut).toBe(true)
  })

  it('does not treat a key rotation as a sign-out', () => {
    expect(describeSessionClose('app_key_rotated').signedOut).toBe(false)
    expect(describeSessionClose('app_key_revoked').signedOut).toBe(false)
  })

  it('does not treat a graceful shutdown as a sign-out', () => {
    // Every restart closes every stream; sessions survive it. It must also be a
    // KNOWN reason, or the most common server-side close would quote a raw
    // internal string at the user.
    const notice = describeSessionClose('shutdown')

    expect(notice.signedOut).toBe(false)
    expect(notice.message).not.toContain('shutdown')
  })

  it('describes a demotion as lost admin access, not a sign-out', () => {
    // A demotion closes only the admin-only realtime stream — the session stays
    // valid. Calling it a sign-out would also strand the message: the reconnect
    // 403s, so the connect-time clear can never fire for that tab.
    const notice = describeSessionClose('permission_downgraded')

    expect(notice.signedOut).toBe(false)
    expect(notice.title).toMatch(/no longer an administrator/i)
    expect(notice.message).toMatch(/still signed in/i)
  })
})

describe('the session-close notice store', () => {
  it('records a sign-out reason and clears it on demand', () => {
    expect(getSessionCloseNotice()).toBeNull()

    noteSessionClose('share_revalidation_token_stale')
    expect(getSessionCloseNotice()?.reason).toBe('share_revalidation_token_stale')

    clearSessionCloseNotice()
    expect(getSessionCloseNotice()).toBeNull()
  })

  it('ignores closes that did not end the session', () => {
    // Rotating the shared API key closes admin streams but leaves the browser's
    // own cookie session alive; showing "you were signed out" would be false.
    noteSessionClose('app_key_rotated')
    expect(getSessionCloseNotice()).toBeNull()
  })
})
