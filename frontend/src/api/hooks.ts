/**
 * The typed data layer. Every backend call is a hook here so screens stay
 * presentational and consistent. Built on the generated client, so a contract
 * change surfaces as a type error in these hooks.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { client } from './client'
import { unwrap, ensureOk } from './http'
import type {
  BlocklistResponse,
  CreateRequestBody,
  DiscoverSearchResponse,
  GrabRequest,
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
    onSuccess: (data) => {
      qc.setQueryData(queryKeys.settings, data)
    },
  })
}

/* --------------------------------------------------------------- discover -- */

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
      void qc.invalidateQueries({ queryKey: ['blocklist'] })
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
