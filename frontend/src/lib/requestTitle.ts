import type { DiscoverResult, RequestResponse } from '../api/types'

/**
 * Adapt a request row to the shape `TitleDetailModal` expects. The modal
 * self-correlates live state by `(tmdb_id, media_type)` off its own polled
 * `useRequests`/`useQueue`, so this is just enough to identify the title and
 * paint its poster while that correlation resolves — `overview` is omitted
 * (requests don't carry it and the modal never reads it) and `media_type` is
 * NARROWED (never cast) from the backend's free string to the literal union,
 * per the "UI only ever sets these" contract in types.ts.
 */
export function requestToDiscoverResult(request: RequestResponse): DiscoverResult {
  return {
    media_type: request.media_type === 'tv' ? 'tv' : 'movie',
    tmdb_id: request.tmdb_id,
    title: request.title,
    year: request.year ?? null,
    poster_url: request.poster_url ?? null,
    backdrop_url: request.backdrop_url ?? null,
    library_state: requestStatusToLibraryState(request.status),
  }
}

/**
 * The request-status half of the server's `derive_library_state` fold
 * (services/discovery_service.py), for a synthesized `DiscoverResult`. A request
 * row carries no Plex-presence bit, so the settled/unknown fallback is an honest
 * `'none'` rather than a fabricated presence claim — the modal self-correlates
 * live state and never reads this field.
 */
export function requestStatusToLibraryState(status: string): DiscoverResult['library_state'] {
  if (status === 'pending') return 'requested'
  if (
    status === 'searching' ||
    status === 'downloading' ||
    status === 'completed' ||
    status === 'no_acceptable_release' ||
    status === 'import_blocked'
  ) {
    return 'processing'
  }
  if (status === 'available') return 'available'
  if (status === 'partially_available') return 'partially_available'
  return 'none'
}
