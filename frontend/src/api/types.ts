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
export type WithdrawSubscriptionResponse = Schemas['WithdrawSubscriptionResponse']
export type CreateRequestBody = Schemas['CreateRequestBody']
export type SeasonStatus = Schemas['SeasonStatus']

/** Issue #370 phase 2 — the compact (folded) live-state view for tile polling. */
export type TileKey = Schemas['TileKey']
export type CompactStateRequest = Schemas['CompactStateRequest']
export type CompactStateResponse = Schemas['CompactStateResponse']
export type CompactStateField = Schemas['CompactStateField']

export type SearchPreviewRequest = Schemas['SearchPreviewRequest']
export type SearchPreviewResponse = Schemas['SearchPreviewResponse']
export type AcceptedRelease = Schemas['AcceptedRelease']
export type RejectedRelease = Schemas['RejectedRelease']

export type QueueItem = Schemas['QueueItem']
export type QueueResponse = Schemas['QueueResponse']
export type GrabRequest = Schemas['GrabRequest']

/**
 * The typed lifecycle-status unions (issue #205). Deriving these FROM the
 * generated response types (rather than re-declaring the member lists) means
 * a backend enum add/rename changes `schema.d.ts`, which changes these
 * aliases, which red-builds every exhaustive `Record<..., …>` map and
 * allowlist `Set<...>` built on them below — the whole point of typing the
 * wire contract.
 */
export type RequestStatusValue = RequestResponse['status']
export type DownloadStateValue = QueueItem['status']

export type BlocklistEntry = Schemas['BlocklistEntry']
export type BlocklistResponse = Schemas['BlocklistResponse']

export type QualityProfileResponse = Schemas['QualityProfileResponse']
export type QualityProfileItemResponse = Schemas['QualityProfileItemResponse']

export type SettingsResponse = Schemas['SettingsResponse']
export type SettingsUpdate = Schemas['SettingsUpdate']
export type AutomaticUpdateWeekday = NonNullable<
  SettingsResponse['automatic_update_weekdays']
>[number]
export type UpdateResultItem = Schemas['UpdateResultItem']
export type UpdateStatusResponse = Schemas['UpdateStatusResponse']
export type AuthMeResponse = Schemas['AuthMeResponse']
export type AuthUser = Schemas['AuthUser']
export type PlexSignInRequest = Schemas['PlexSignInRequest']
export type ActiveSessionsResponse = Schemas['ActiveSessionsResponse']
export type ActiveSessionUser = Schemas['ActiveSessionUser']
export type RecoverySessionGroup = Schemas['RecoverySessionGroup']
export type RevokeSessionsResponse = Schemas['RevokeSessionsResponse']

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
export type AppApiKeyStatusResponse = Schemas['AppApiKeyStatusResponse']

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

/**
 * The value a library-root picker `<option>` stores when selected: the confident
 * container remap (`suggested_path`) when the backend resolved one, else the raw
 * Plex path. Deliberately nothing else — the backend offers no guesses (an
 * unresolvable location keeps its raw path; the operator types a container path
 * manually for exotic bind topologies), so the UI never silently rewrites.
 */
export function libraryOptionValue(lib: PlexLibraryOption): string {
  return lib.suggested_path ?? lib.path
}

/** The trailing " · in-container: …" note for a library-root `<option>`. */
export function libraryOptionNote(lib: PlexLibraryOption): string {
  if (lib.suggested_path && lib.suggested_path !== lib.path) {
    return ` · in-container: ${lib.suggested_path}`
  }
  return ''
}
