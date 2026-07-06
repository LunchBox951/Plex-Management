import { describe, expect, it } from 'vitest'
import { toApiError } from './errors'

describe('toApiError', () => {
  it('maps a known detail code to a friendly, honest message', () => {
    const err = toApiError({ detail: 'no_acceptable_release' }, 409)
    expect(err.code).toBe('no_acceptable_release')
    expect(err.status).toBe(409)
    expect(err.message).toMatch(/no acceptable release/i)
  })

  it('renders the raw code for an unknown detail code instead of swallowing it', () => {
    const err = toApiError({ detail: 'some_unmapped_code' }, 500)
    expect(err.code).toBe('some_unmapped_code')
    expect(err.message).toBe('some_unmapped_code')
  })

  it('maps the recovery-key rotation 409 to the honest refresh-and-retry copy', () => {
    const err = toApiError({ detail: 'app_key_changed' }, 409)
    expect(err.code).toBe('app_key_changed')
    expect(err.status).toBe(409)
    expect(err.message).toBe(
      'The recovery key changed while you were rotating it. Refresh and try again.',
    )
  })

  it('reads the first message from a FastAPI validation error list', () => {
    const err = toApiError({ detail: [{ msg: 'field required', loc: ['body', 'x'] }] }, 422)
    expect(err.message).toBe('field required')
  })

  it('reads the envelope message, hint, and diagnostics alongside detail', () => {
    const err = toApiError(
      {
        detail: 'server_unreachable_from_backend',
        message: 'The server could not reach 10.0.0.5:32400.',
        hint: 'Use the host IP, not localhost.',
        diagnostics: { host: '10.0.0.5:32400', reason: 'timeout' },
      },
      502,
    )
    expect(err.code).toBe('server_unreachable_from_backend')
    // An explicit envelope message wins over the built-in copy for that code.
    expect(err.message).toBe('The server could not reach 10.0.0.5:32400.')
    expect(err.hint).toBe('Use the host IP, not localhost.')
    expect(err.diagnostics).toEqual({ host: '10.0.0.5:32400', reason: 'timeout' })
  })

  it('returns an honest HTTP-status fallback (no bare catch-all) with no detail', () => {
    const err = toApiError({}, 503)
    expect(err.code).toBe('unknown_error')
    // Pinning the exact new fallback proves the old generic sentence is gone —
    // a positive assertion, so the banned phrase never re-enters the source.
    expect(err.message).toBe('The server returned an unexpected error (HTTP 503).')
  })
})
