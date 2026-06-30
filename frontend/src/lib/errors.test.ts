import { describe, expect, it } from 'vitest'
import { toApiError } from './errors'

describe('toApiError', () => {
  it('maps a known detail code to a friendly, honest message', () => {
    const err = toApiError({ detail: 'no_acceptable_release' }, 409)
    expect(err.code).toBe('no_acceptable_release')
    expect(err.status).toBe(409)
    expect(err.message).toMatch(/no acceptable release/i)
  })

  it('humanizes an unknown detail code instead of swallowing it', () => {
    const err = toApiError({ detail: 'some_unmapped_code' }, 500)
    expect(err.code).toBe('some_unmapped_code')
    expect(err.message).toBe('Some unmapped code')
  })

  it('reads the first message from a FastAPI validation error list', () => {
    const err = toApiError({ detail: [{ msg: 'field required', loc: ['body', 'x'] }] }, 422)
    expect(err.message).toBe('field required')
  })

  it('returns a safe default when there is no detail', () => {
    const err = toApiError({}, 0)
    expect(err.code).toBe('unknown_error')
    expect(err.message).toMatch(/something went wrong/i)
  })
})
