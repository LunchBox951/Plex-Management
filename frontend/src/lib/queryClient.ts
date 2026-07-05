import { QueryClient } from '@tanstack/react-query'

/**
 * Polling cadence for the live surfaces. The realtime SSE stream (ADR-0017)
 * invalidates these same caches when connected, but polling never stops — it
 * drops to a slow floor (below) as a permanent safety net against a dead or
 * zombied stream. When disconnected, the fast cadence takes over.
 */
export const POLL_INTERVAL_MS = 2000
export const REQUESTS_POLL_INTERVAL_MS = 5000
// When realtime SSE is connected we do NOT stop polling — we drop to a SLOW
// floor instead. This is a permanent safety net (Overseerr keeps polling even
// with its socket up): a dead or zombie stream, or a missed heartbeat the
// client watchdog somehow fails to catch, still self-heals within one slow tick
// regardless of the stream's health. Coarse cadence so it costs almost nothing.
export const QUEUE_REALTIME_FLOOR_MS = 25000
export const REQUESTS_REALTIME_FLOOR_MS = 45000
// The Status page's health/disk cards: matches the backend's own ~15s TTL
// cache on the upstream probes (ADR-0012), so polling faster than this would
// just re-read the same cached snapshot without learning anything new.
export const OPS_POLL_INTERVAL_MS = 15000
// The Logs page's live-tail toggle — a snappier cadence than the durable
// store poll above, since the ring buffer is meant to feel "live".
export const LOG_TAIL_POLL_INTERVAL_MS = 3000

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
  authMe: ['auth', 'me'] as const,
  setupStatus: ['setup', 'status'] as const,
  setupPlexServers: ['setup', 'plex-servers'] as const,
  settings: ['settings'] as const,
  plexLibraries: ['settings', 'plex-libraries'] as const,
  // Kept OFF the ['settings'] prefix on purpose: a settings save must not
  // invalidate the recovery-key existence check (they are independent facts).
  appKeyStatus: ['app-key', 'status'] as const,
  requests: ['requests'] as const,
  request: (id: number) => ['requests', id] as const,
  queue: ['queue'] as const,
  blocklist: (tmdbId?: number) => ['blocklist', tmdbId ?? 'all'] as const,
  qualityProfile: ['quality-profile'] as const,
  discover: (query: string, year?: number) => ['discover', query, year ?? null] as const,
  discoverHome: ['discover', 'home'] as const,
  searchPreview: ['search-preview'] as const,
  opsHealth: ['ops', 'health'] as const,
  opsDisk: ['ops', 'disk'] as const,
  opsLogsTail: ['ops', 'logs', 'tail'] as const,
  opsLogs: (filter: {
    level?: string
    since?: string
    logger?: string
    correlationId?: string
    limit?: number
    offset?: number
  }) => ['ops', 'logs', filter] as const,
}
