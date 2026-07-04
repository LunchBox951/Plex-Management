/**
 * The typed data layer. Every backend call is a hook here so screens stay
 * presentational and consistent. Built on the generated client, so a contract
 * change surfaces as a type error in these hooks.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from './client'
import { unwrap, ensureOk } from './http'
import { setApiKey } from '../lib/apiKey'
import type {
  AppApiKeyResponse,
  BlocklistResponse,
  CreateRequestBody,
  DiscoverHomeResponse,
  DiscoverSearchResponse,
  DiskResponse,
  EvictResponse,
  GrabRequest,
  HealthResponse,
  LogsResponse,
  LogsTailResponse,
  PlexLibraryOption,
  QualityProfileResponse,
  QueueItem,
  QueueResponse,
  RequestListResponse,
  RequestResponse,
  SearchPreviewRequest,
  SearchPreviewResponse,
  ServiceValidateResponse,
  SettingsResponse,
  SettingsUpdate,
  SetupCompleteRequest,
  SetupStatusResponse,
} from './types'
import {
  LOG_TAIL_POLL_INTERVAL_MS,
  OPS_POLL_INTERVAL_MS,
  POLL_INTERVAL_MS,
  REQUESTS_POLL_INTERVAL_MS,
  queryKeys,
} from '../lib/queryClient'

/* ------------------------------------------------------------------ setup -- */

export function useSetupStatus() {
  return useQuery({
    queryKey: queryKeys.setupStatus,
    queryFn: async (): Promise<SetupStatusResponse> =>
      unwrap(await client.GET('/api/v1/setup/status')),
  })
}

export type SetupService = 'plex' | 'prowlarr' | 'qbittorrent' | 'tmdb'

export function useValidateService() {
  return useMutation({
    mutationFn: async (args: {
      service: SetupService
      body: Record<string, string>
    }): Promise<ServiceValidateResponse> => {
      switch (args.service) {
        case 'plex':
          return unwrap(
            await client.POST('/api/v1/setup/validate/plex', {
              body: args.body as { url: string; token: string },
            }),
          )
        case 'prowlarr':
          return unwrap(
            await client.POST('/api/v1/setup/validate/prowlarr', {
              body: args.body as { url: string; api_key: string },
            }),
          )
        case 'qbittorrent':
          return unwrap(
            await client.POST('/api/v1/setup/validate/qbittorrent', {
              body: args.body as { url: string; username: string; password: string },
            }),
          )
        case 'tmdb':
          return unwrap(
            await client.POST('/api/v1/setup/validate/tmdb', {
              body: args.body as { api_key: string },
            }),
          )
      }
    },
  })
}

export function useCompleteSetup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: SetupCompleteRequest) =>
      unwrap(await client.POST('/api/v1/setup/complete', { body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.setupStatus })
    },
  })
}

/* --------------------------------------------------------------- settings -- */

export function useSettings() {
  return useQuery({
    queryKey: queryKeys.settings,
    queryFn: async (): Promise<SettingsResponse> => unwrap(await client.GET('/api/v1/settings')),
  })
}

export function useUpdateSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: SettingsUpdate): Promise<SettingsResponse> =>
      unwrap(await client.PUT('/api/v1/settings', { body })),
    onSuccess: (data, variables) => {
      const previous = qc.getQueryData<SettingsResponse>(queryKeys.settings)
      const plexConnectionChanged =
        previous === undefined ||
        (typeof variables.plex_url === 'string' &&
          variables.plex_url !== (previous.plex_url ?? '')) ||
        (typeof variables.plex_token === 'string' && variables.plex_token.length > 0)
      qc.setQueryData(queryKeys.settings, data)
      if (plexConnectionChanged) {
        // A library path belongs to a specific Plex server. Drop the whole cached
        // picker result before the form re-enables it, so old folders cannot remain
        // selectable while the new connection refetch is in flight.
        qc.removeQueries({ queryKey: queryKeys.plexLibraries })
      } else {
        // Clear any prior unconfigured/auth error, which retry:false otherwise leaves
        // stuck until the page remounts.
        void qc.invalidateQueries({ queryKey: queryKeys.plexLibraries })
      }
      // The TMDB api key may have changed; Discover's home + search results are
      // keyed on the old credentials, so drop them too. TanStack Query v5's
      // default exact:false prefix match covers both queryKeys.discoverHome
      // (['discover','home']) and every queryKeys.discover(query, year) variant
      // (['discover', query, year]) with this one call (issue #14).
      void qc.invalidateQueries({ queryKey: ['discover'] })
    },
  })
}

/**
 * Reveal the CURRENT app X-Api-Key in plaintext — the break-glass recovery
 * path for a new device/browser, without re-running setup. On-demand
 * (mutation, not a query) so the key is only ever fetched on an explicit
 * "Reveal" click, never pre-fetched/cached.
 */
export function useRevealAppKey() {
  return useMutation({
    mutationFn: async (): Promise<AppApiKeyResponse> =>
      unwrap(await client.GET('/api/v1/settings/app-key')),
  })
}

/**
 * Mint a brand-new app X-Api-Key, invalidating the old one everywhere. The
 * CALLER'S own stored key must be updated immediately (via setApiKey) or the
 * current session's very next request would 401 with no saved key to fall
 * back to — every OTHER device/browser holding the old key is, correctly,
 * locked out until it's re-paired with the new one.
 */
export function useRotateAppKey() {
  return useMutation({
    mutationFn: async (): Promise<AppApiKeyResponse> =>
      unwrap(await client.POST('/api/v1/settings/app-key/rotate')),
    onSuccess: (data) => {
      setApiKey(data.app_api_key)
    },
  })
}

/** Movie library folders Plex reports, for the Settings movies_root picker. */
export function usePlexLibraries(enabled = true) {
  return useQuery({
    queryKey: queryKeys.plexLibraries,
    enabled,
    retry: false, // a 409 (Plex unconfigured) is a normal state, not worth retrying
    queryFn: async (): Promise<PlexLibraryOption[]> =>
      unwrap(await client.GET('/api/v1/settings/plex-libraries')),
  })
}

/* --------------------------------------------------------------- discover -- */

export function useDiscoverHome() {
  return useQuery({
    queryKey: queryKeys.discoverHome,
    queryFn: async (): Promise<DiscoverHomeResponse> =>
      unwrap(await client.GET('/api/v1/discover/home')),
  })
}

export function useDiscoverSearch(query: string, year?: number) {
  const trimmed = query.trim()
  return useQuery({
    queryKey: queryKeys.discover(trimmed, year),
    enabled: trimmed.length > 0,
    queryFn: async (): Promise<DiscoverSearchResponse> =>
      unwrap(
        await client.GET('/api/v1/discover/search', {
          params: { query: year === undefined ? { query: trimmed } : { query: trimmed, year } },
        }),
      ),
  })
}

/* --------------------------------------------------------------- requests -- */

export function useRequests(options?: { poll?: boolean }) {
  return useQuery({
    queryKey: queryKeys.requests,
    queryFn: async (): Promise<RequestListResponse> => unwrap(await client.GET('/api/v1/requests')),
    refetchInterval: options?.poll ? REQUESTS_POLL_INTERVAL_MS : false,
  })
}

export function useRequest(id: number, enabled = true) {
  return useQuery({
    queryKey: queryKeys.request(id),
    enabled,
    queryFn: async (): Promise<RequestResponse> =>
      unwrap(
        await client.GET('/api/v1/requests/{request_id}', {
          params: { path: { request_id: id } },
        }),
      ),
  })
}

export function useCreateRequest() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: CreateRequestBody): Promise<RequestResponse> =>
      unwrap(await client.POST('/api/v1/requests', { body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
    },
  })
}

/**
 * Set or clear the "keep forever" pin (ADR-0012) — the north-star #1
 * correction path for "don't let the eviction sweep touch this one". Also
 * invalidates the disk preview: a newly-pinned title must drop out of (and a
 * newly-unpinned one may re-enter) the eviction-candidate list immediately.
 */
export function useSetKeepForever() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: {
      requestId: number
      keepForever: boolean
    }): Promise<RequestResponse> =>
      unwrap(
        await client.POST('/api/v1/requests/{request_id}/keep-forever', {
          params: { path: { request_id: args.requestId } },
          body: { keep_forever: args.keepForever },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
      void qc.invalidateQueries({ queryKey: queryKeys.opsDisk })
    },
  })
}

/** The operator-choosable report-issue reasons (ADR-0014) — the `BlocklistReason`
 * values minus the auto-only `failed`. Mirrors the backend `ReportIssueBody`. */
export type ReportReason = 'bad_quality' | 'wrong_media' | 'user_reported'

/**
 * Report a bad imported/available movie or TV season (ADR-0014): blocklist the
 * culprit release, purge its torrent + library file, and synchronously re-search
 * for a different release. Returns the updated request (re-grabbing, or parked at
 * no_acceptable_release). Invalidates requests + queue + blocklist so every
 * surface reflects the correction at once.
 */
export function useReportIssue() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: {
      requestId: number
      reason: ReportReason
      season?: number | null
    }): Promise<RequestResponse> =>
      unwrap(
        await client.POST('/api/v1/requests/{request_id}/report-issue', {
          params: { path: { request_id: args.requestId } },
          body: { reason: args.reason, season: args.season ?? null },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
      void qc.invalidateQueries({ queryKey: queryKeys.queue })
      void qc.invalidateQueries({ queryKey: ['blocklist'] })
    },
  })
}

/**
 * Cancel a not-yet-imported request (ADR-0014): drop any active torrent(s) and
 * settle the request to `cancelled`. The honest opposite of report-issue —
 * nothing is re-grabbed. Invalidates requests + queue.
 */
export function useCancelRequest() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (requestId: number): Promise<RequestResponse> =>
      unwrap(
        await client.POST('/api/v1/requests/{request_id}/cancel', {
          params: { path: { request_id: requestId } },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
      void qc.invalidateQueries({ queryKey: queryKeys.queue })
    },
  })
}

/* --------------------------------------------------------- search-preview -- */

export function useSearchPreview() {
  return useMutation({
    mutationFn: async (body: SearchPreviewRequest): Promise<SearchPreviewResponse> =>
      unwrap(await client.POST('/api/v1/search-preview', { body })),
  })
}

/* ------------------------------------------------------------------ queue -- */

export function useQueue(options?: { poll?: boolean }) {
  return useQuery({
    queryKey: queryKeys.queue,
    queryFn: async (): Promise<QueueResponse> => unwrap(await client.GET('/api/v1/queue')),
    refetchInterval: options?.poll ? POLL_INTERVAL_MS : false,
  })
}

export function useGrab() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (body: GrabRequest): Promise<QueueItem> =>
      unwrap(await client.POST('/api/v1/queue/grab', { body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.queue })
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
    },
  })
}

export function useMarkFailed() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (args: { downloadId: number; blocklist: boolean }): Promise<QueueItem> =>
      unwrap(
        await client.POST('/api/v1/queue/{download_id}/mark-failed', {
          params: {
            path: { download_id: args.downloadId },
            query: { blocklist: args.blocklist },
          },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.queue })
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
      void qc.invalidateQueries({ queryKey: ['blocklist'] })
    },
  })
}

export function useImportDownload() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (downloadId: number): Promise<QueueItem> =>
      unwrap(
        await client.POST('/api/v1/queue/{download_id}/import', {
          params: { path: { download_id: downloadId } },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.queue })
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
    },
  })
}

/* -------------------------------------------------------------- blocklist -- */

export function useBlocklist(tmdbId?: number) {
  return useQuery({
    queryKey: queryKeys.blocklist(tmdbId),
    queryFn: async (): Promise<BlocklistResponse> =>
      unwrap(
        await client.GET('/api/v1/blocklist', {
          params: { query: tmdbId === undefined ? {} : { tmdb_id: tmdbId } },
        }),
      ),
  })
}

export function useDeleteBlocklistEntry() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (blocklistId: number): Promise<void> => {
      ensureOk(
        await client.DELETE('/api/v1/blocklist/{blocklist_id}', {
          params: { path: { blocklist_id: blocklistId } },
        }),
      )
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['blocklist'] })
    },
  })
}

/* -------------------------------------------------------- quality-profile -- */

export function useQualityProfile() {
  return useQuery({
    queryKey: queryKeys.qualityProfile,
    queryFn: async (): Promise<QualityProfileResponse> =>
      unwrap(await client.GET('/api/v1/quality-profile')),
  })
}

/* --------------------------------------------------------------------- ops -- */
// ADR-0012 — Status page (health/reconcile/disk) + Logs page.

/** One read: per-subsystem reachability, disk gauges, the reconcile loop's own
 * health. Polled at `OPS_POLL_INTERVAL_MS` — matches the backend's own ~15s
 * upstream-probe TTL cache, so a faster poll would just re-read the same
 * cached snapshot. `poll` defaults on; the Status page turns it off on unmount
 * via TanStack Query's own inactive-query GC, nothing extra needed here. */
export function useOpsHealth(options?: { poll?: boolean }) {
  return useQuery({
    queryKey: queryKeys.opsHealth,
    queryFn: async (): Promise<HealthResponse> => unwrap(await client.GET('/api/v1/ops/health')),
    refetchInterval: options?.poll === false ? false : OPS_POLL_INTERVAL_MS,
  })
}

/** Disk usage per configured library root, plus each root's ranked
 * eviction-candidate preview (never evicts anything itself — see `useEvict`). */
export function useOpsDisk(options?: { poll?: boolean }) {
  return useQuery({
    queryKey: queryKeys.opsDisk,
    queryFn: async (): Promise<DiskResponse> => unwrap(await client.GET('/api/v1/ops/disk')),
    refetchInterval: options?.poll === false ? false : OPS_POLL_INTERVAL_MS,
  })
}

/**
 * The north-star #1 button: manually trigger a disk-pressure eviction sweep
 * across every configured root, right now. An empty `evicted` list is a
 * normal, honest outcome (nothing was under pressure, or nothing eligible was
 * found) — the caller decides how to phrase that, this hook just reports it.
 */
export function useEvict() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (): Promise<EvictResponse> => unwrap(await client.POST('/api/v1/ops/evict')),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: queryKeys.opsDisk })
      void qc.invalidateQueries({ queryKey: queryKeys.opsHealth })
      // Evicted titles flip to the visible `evicted` request status.
      void qc.invalidateQueries({ queryKey: queryKeys.requests })
    },
  })
}

/** Filters accepted by `GET /ops/logs` — every field is an EXACT match
 * server-side (never a substring search); the Logs page's free-text search
 * box filters the fetched page client-side on top of these. */
export interface LogsFilter {
  level?: string
  since?: string
  logger?: string
  correlationId?: string
  limit?: number
  offset?: number
}

/** A paginated, filtered page of the durable `log_events` store, newest first. */
export function useLogs(filter: LogsFilter, options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.opsLogs(filter),
    enabled: options?.enabled ?? true,
    queryFn: async (): Promise<LogsResponse> =>
      unwrap(
        await client.GET('/api/v1/ops/logs', {
          params: {
            query: {
              level: filter.level ?? null,
              since: filter.since ?? null,
              logger: filter.logger ?? null,
              correlation_id: filter.correlationId ?? null,
              ...(filter.limit !== undefined ? { limit: filter.limit } : {}),
              ...(filter.offset !== undefined ? { offset: filter.offset } : {}),
            },
          },
        }),
      ),
  })
}

/** The live, in-memory, ALL-levels ring-buffer tail (newest first) — lost on
 * restart, never persisted. Only polls while `enabled` (the Logs page's
 * live-tail toggle); otherwise this is a dead, unfetched query. */
export function useLogsTail(options?: { enabled?: boolean; limit?: number }) {
  const enabled = options?.enabled ?? false
  return useQuery({
    queryKey: queryKeys.opsLogsTail,
    enabled,
    queryFn: async (): Promise<LogsTailResponse> =>
      unwrap(
        await client.GET('/api/v1/ops/logs/tail', {
          params: { query: options?.limit !== undefined ? { limit: options.limit } : {} },
        }),
      ),
    refetchInterval: enabled ? LOG_TAIL_POLL_INTERVAL_MS : false,
  })
}

/**
 * The LLM-diagnosis affordance: fetch one coherent, plain-text trail (oldest
 * first) — either a single correlation id's FULL history, or a time window
 * (omitted `since` defaults server-side to the last 24h). A mutation rather
 * than a cached query: this is an on-demand export action (Copy/Download),
 * never something the UI polls or re-renders from cache.
 */
export function useExportLogs() {
  return useMutation({
    mutationFn: async (args: { correlationId?: string; since?: string }): Promise<string> =>
      unwrap(
        await client.GET('/api/v1/ops/logs/export', {
          params: {
            query: {
              correlation_id: args.correlationId ?? null,
              since: args.since ?? null,
              format: 'text',
            },
          },
          parseAs: 'text',
        }),
      ),
  })
}
