/** Thin helpers that turn an openapi-fetch result into a value-or-throw for TanStack Query. */
import { type ApiError, toApiError } from '../lib/errors'

interface FetchResult<T> {
  data?: T
  error?: unknown
  response: Response
}

/** Return the success body, or throw a normalized {@link ApiError}. */
export function unwrap<T>(result: FetchResult<T>): T {
  if (result.error !== undefined) {
    throw toApiError(result.error, result.response.status)
  }
  if (result.data === undefined) {
    throw toApiError({ detail: 'empty_response' }, result.response.status)
  }
  return result.data
}

/** For no-content (204) calls: throw on error, otherwise resolve void. */
export function ensureOk(result: { error?: unknown; response: Response }): void {
  if (result.error !== undefined) {
    throw toApiError(result.error, result.response.status)
  }
}

export type { ApiError }
