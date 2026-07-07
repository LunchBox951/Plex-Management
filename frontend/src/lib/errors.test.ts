import { describe, expect, it } from 'vitest'
import { humanize, toApiError } from './errors'

describe('humanize', () => {
  it('turns a snake_case code into a readable sentence-case phrase', () => {
    expect(humanize('no_acceptable_release')).toBe('No acceptable release')
  })
})

describe('toApiError', () => {
  it('maps a known detail code to a friendly, honest message', () => {
    const err = toApiError({ detail: 'no_acceptable_release' }, 409)
    expect(err.code).toBe('no_acceptable_release')
    expect(err.status).toBe(409)
    expect(err.message).toMatch(/no acceptable release/i)
  })

  it('humanizes an unmapped detail code instead of surfacing raw snake_case', () => {
    // A pipeline code absent from DETAIL_MESSAGES (e.g. a correction/request verb)
    // must still read as a phrase — regression guard for the dropped fallback.
    const err = toApiError({ detail: 'some_future_pipeline_code' }, 409)
    // The raw code stays available for technical display...
    expect(err.code).toBe('some_future_pipeline_code')
    // ...while the message is the readable, humanized rendering.
    expect(err.message).toBe('Some future pipeline code')
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

  it('lets an explicit envelope message win over media_root_unavailable\'s built-in copy', () => {
    const err = toApiError(
      { detail: 'media_root_unavailable', message: 'The library folder for Some Movie is gone.' },
      409,
    )
    expect(err.code).toBe('media_root_unavailable')
    expect(err.message).toBe('The library folder for Some Movie is gone.')
  })

  it('falls back to the built-in media_root_unavailable copy with no envelope message', () => {
    const err = toApiError({ detail: 'media_root_unavailable' }, 409)
    expect(err.message).toMatch(/isn’t reachable/)
  })

  it.each([
    ['library_root_unreachable', /isn’t visible to Plex Manager/],
    ['not_reportable', /can’t be reported right now/],
    ['active_duplicate', /newer request .* already exists/],
  ] as const)('maps %s to its honest sentence, never raw snake_case', (code, expected) => {
    const err = toApiError({ detail: code }, 409)
    expect(err.code).toBe(code)
    expect(err.message).toMatch(expected)
  })
})
