import { QueryClient } from '@tanstack/react-query'

/**
 * Polling cadence for the live surfaces. There is no event stream yet
 * (ADR-0009): `/queue` and `/requests` are polled. When the backend grows SSE,
 * an EventSource handler will write into this same cache and the intervals go
 * away with no component changes.
 */
export const POLL_INTERVAL_MS = 2000
export const REQUESTS_POLL_INTERVAL_MS = 5000

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 1000,
    },
  },
})

/** Stable query-key roots so mutations can invalidate precisely. */
export const queryKeys = {
  setupStatus: ['setup', 'status'] as const,
  settings: ['settings'] as const,
  plexLibraries: ['settings', 'plex-libraries'] as const,
  requests: ['requests'] as const,
  request: (id: number) => ['requests', id] as const,
  queue: ['queue'] as const,
  blocklist: (tmdbId?: number) => ['blocklist', tmdbId ?? 'all'] as const,
  qualityProfile: ['quality-profile'] as const,
  discover: (query: string, year?: number) => ['discover', query, year ?? null] as const,
  discoverHome: ['discover', 'home'] as const,
  searchPreview: ['search-preview'] as const,
}
