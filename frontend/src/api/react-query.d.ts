import type { ApiError } from '../lib/errors'

// Every query/mutation throws a normalized ApiError (see api/http.ts), so type
// the default error as ApiError app-wide. Screens can read error.message /
// error.code / error.status without casting.
declare module '@tanstack/react-query' {
  interface Register {
    defaultError: ApiError
  }
}
