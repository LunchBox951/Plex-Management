/**
 * Friendly aliases for the generated contract types. Components import these so
 * the UI never re-declares a field or enum the backend already owns — a backend
 * change regenerates `schema.d.ts` and breaks the build here, not in production.
 */
import type { components } from './schema'

type Schemas = components['schemas']

export type DiscoverResult = Schemas['DiscoverResult']
export type DiscoverSearchResponse = Schemas['DiscoverSearchResponse']
export type DiscoverHomeResponse = Schemas['DiscoverHomeResponse']
export type DiscoverHomeRow = Schemas['DiscoverHomeRow']
export type DiscoverListResponse = Schemas['DiscoverListResponse']

export type RequestResponse = Schemas['RequestResponse']
export type RequestListResponse = Schemas['RequestListResponse']
export type CreateRequestBody = Schemas['CreateRequestBody']
export type SeasonStatus = Schemas['SeasonStatus']

export type SearchPreviewRequest = Schemas['SearchPreviewRequest']
export type SearchPreviewResponse = Schemas['SearchPreviewResponse']
export type AcceptedRelease = Schemas['AcceptedRelease']
export type RejectedRelease = Schemas['RejectedRelease']

export type QueueItem = Schemas['QueueItem']
export type QueueResponse = Schemas['QueueResponse']
export type GrabRequest = Schemas['GrabRequest']

export type BlocklistEntry = Schemas['BlocklistEntry']
export type BlocklistResponse = Schemas['BlocklistResponse']

export type QualityProfileResponse = Schemas['QualityProfileResponse']
export type QualityProfileItemResponse = Schemas['QualityProfileItemResponse']

export type SettingsResponse = Schemas['SettingsResponse']
export type SettingsUpdate = Schemas['SettingsUpdate']
export type AuthMeResponse = Schemas['AuthMeResponse']
export type AuthUser = Schemas['AuthUser']
export type PlexSignInRequest = Schemas['PlexSignInRequest']

export type SetupStatusResponse = Schemas['SetupStatusResponse']
export type SetupCompleteRequest = Schemas['SetupCompleteRequest']
export type ServiceValidateResponse = Schemas['ServiceValidateResponse']
export type PlexLibraryOption = Schemas['PlexLibraryOption']
export type PlexValidateRequest = Schemas['PlexValidateRequest']
export type PlexServersResponse = Schemas['PlexServersResponse']
export type PlexServerOption = Schemas['PlexServerOption']
export type PlexServerConnection = Schemas['PlexServerConnection']
export type ProwlarrValidateRequest = Schemas['ProwlarrValidateRequest']
export type QbittorrentValidateRequest = Schemas['QbittorrentValidateRequest']
export type TmdbValidateRequest = Schemas['TmdbValidateRequest']

export type KeepForeverBody = Schemas['KeepForeverBody']
export type AppApiKeyResponse = Schemas['AppApiKeyResponse']

/* ------------------------------------------------------------------- ops -- */
// ADR-0012 — health/status dashboard, log viewer, disk-pressure eviction.

export type HealthResponse = Schemas['HealthResponse']
export type SubsystemHealthItem = Schemas['SubsystemHealthItem']
export type DiskGaugeItem = Schemas['DiskGaugeItem']
export type ReconcileStatusItem = Schemas['ReconcileStatusItem']

export type LogEventItem = Schemas['LogEventItem']
export type LogsResponse = Schemas['LogsResponse']
export type LiveLogRecordItem = Schemas['LiveLogRecordItem']
export type LogsTailResponse = Schemas['LogsTailResponse']

export type EvictionCandidateItem = Schemas['EvictionCandidateItem']
export type DiskRootItem = Schemas['DiskRootItem']
export type DiskResponse = Schemas['DiskResponse']
export type EvictionOutcomeItem = Schemas['EvictionOutcomeItem']
export type EvictErrorItem = Schemas['EvictErrorItem']
export type EvictResponse = Schemas['EvictResponse']

/** `media_type` is a free string in the contract; the UI only ever sets these. */
export type MediaType = 'movie' | 'tv'
