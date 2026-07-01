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

export type SetupStatusResponse = Schemas['SetupStatusResponse']
export type SetupCompleteRequest = Schemas['SetupCompleteRequest']
export type ServiceValidateResponse = Schemas['ServiceValidateResponse']
export type PlexLibraryOption = Schemas['PlexLibraryOption']
export type PlexValidateRequest = Schemas['PlexValidateRequest']
export type ProwlarrValidateRequest = Schemas['ProwlarrValidateRequest']
export type QbittorrentValidateRequest = Schemas['QbittorrentValidateRequest']
export type TmdbValidateRequest = Schemas['TmdbValidateRequest']

/** `media_type` is a free string in the contract; the UI only ever sets these. */
export type MediaType = 'movie' | 'tv'
