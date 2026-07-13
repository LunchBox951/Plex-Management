import type { DownloadStateValue, RequestStatusValue } from '../api/types'

/**
 * Per-title / per-download status presentation.
 *
 * The backend's `status` fields carry the canonical `RequestStatus` /
 * `DownloadState` enum values, typed in the OpenAPI contract and generated
 * client (issue #205). The design handoff (§4) collapses these onto five
 * semantic colors. This map is the single place that translation lives, so
 * every badge/pill across the app reads identically.
 *
 * The maps below are typed `Record<RequestStatusValue, …>` /
 * `Record<DownloadStateValue, …>` — a `Record` over a union type requires
 * every member to have an entry, so adding or renaming a backend enum value
 * red-builds `tsc` right here until this map is updated (that's the
 * contract-drift guarantee ADR-0009 promises). `lookup()` still falls back to
 * a neutral, humanized label for a status that is unknown AT RUNTIME (a
 * rolling deploy: a stale bundle whose union is missing a value the backend
 * — running newer code — actually emits) — honesty over silence, not a
 * contradiction: the exhaustiveness check is compile-time against what THIS
 * bundle's contract says exists, while the runtime fallback covers a bundle
 * that hasn't caught up yet.
 */
export type StatusIntent = 'searching' | 'downloading' | 'available' | 'error' | 'neutral'

export interface StatusPresentation {
  label: string
  intent: StatusIntent
}

const REQUEST_STATUS: Record<RequestStatusValue, StatusPresentation> = {
  pending: { label: 'Requested', intent: 'neutral' },
  searching: { label: 'Searching', intent: 'searching' },
  no_acceptable_release: { label: 'No release', intent: 'error' },
  waiting_for_air_date: { label: 'Waiting for air date', intent: 'neutral' },
  downloading: { label: 'Downloading', intent: 'downloading' },
  import_blocked: { label: 'Import blocked', intent: 'error' },
  completed: { label: 'Finalizing', intent: 'downloading' },
  available: { label: 'In library', intent: 'available' },
  // tv only: the show's rollup when SOME (not all) tracked seasons are available
  // (domain.season_rollup.rollup_status). Never a per-season status itself — a
  // single SeasonRequest only ever carries the statuses above.
  partially_available: { label: 'Partially available', intent: 'available' },
  failed: { label: 'Failed', intent: 'error' },
  // ADR-0012: the disk-pressure sweep reclaimed this title's (or, for tv, this
  // season's) file. Settled/re-requestable, same as available/failed — never an
  // error (it is deliberate, honest space management), so this gets the neutral
  // intent rather than red.
  evicted: { label: 'Evicted', intent: 'neutral' },
  // ADR-0014: the operator cancelled a not-yet-imported request. Settled/
  // re-requestable; deliberate, not a failure, so neutral rather than red.
  cancelled: { label: 'Cancelled', intent: 'neutral' },
}

const DOWNLOAD_STATUS: Record<DownloadStateValue, StatusPresentation> = {
  searching: { label: 'Searching', intent: 'searching' },
  downloading: { label: 'Downloading', intent: 'downloading' },
  metadata_fetching: { label: 'Fetching metadata', intent: 'searching' },
  import_pending: { label: 'Import pending', intent: 'downloading' },
  import_blocked: { label: 'Import blocked', intent: 'error' },
  importing: { label: 'Importing', intent: 'downloading' },
  imported: { label: 'Imported', intent: 'available' },
  failed_pending: { label: 'Retrying', intent: 'error' },
  failed: { label: 'Failed', intent: 'error' },
  no_acceptable_release: { label: 'No release', intent: 'error' },
  client_missing: { label: 'Client missing', intent: 'error' },
}

function humanize(value: string): string {
  return value
    .replace(/_/g, ' ')
    .replace(/^\w/, (c) => c.toUpperCase())
}

/**
 * `status` is deliberately plain `string` (callers pass `string | null` from
 * looser call sites), not the narrower union — so the lookup itself must
 * treat the table as a partial map and honor a miss (an unknown-at-runtime
 * value) with the neutral humanized fallback, never a throw.
 */
function lookup<T extends string>(
  table: Partial<Record<T, StatusPresentation>>,
  status: string,
): StatusPresentation {
  return table[status as T] ?? { label: humanize(status), intent: 'neutral' }
}

export function requestStatus(status: string): StatusPresentation {
  return lookup(REQUEST_STATUS, status)
}

export function downloadStatus(status: string): StatusPresentation {
  return lookup(DOWNLOAD_STATUS, status)
}

/**
 * The `RequestStatus` values that count as "in flight" — a request that is mid-
 * pipeline and not yet settled: hunting a release (`searching`), pulling one
 * (`downloading`), between retries after finding none acceptable
 * (`no_acceptable_release`, non-terminal, re-searches on a schedule), imported
 * and awaiting Plex's availability confirmation (`completed` — the backend's
 * in-flight "Finalizing" state; see `_SETTLED_REQUEST_STATUSES` in
 * `repositories/requests.py`, which deliberately excludes it), or stuck
 * mid-import awaiting the operator's retry/reject (`import_blocked` —
 * non-terminal; see `RequestStatus` in `models.py`). Omitting either of the
 * last two would let the badge read zero while the request is still visibly
 * active on the Requests page — dishonest silence.
 *
 * Deliberately EXCLUDES:
 *   - not-yet-started states with no pipeline activity to report (`pending`,
 *     and `waiting_for_air_date`, which is dormant by design until its wake);
 *   - settled states (`available`, `failed`, `cancelled`, `evicted`);
 *   - `partially_available` — the parent-only TV rollup. Non-settled for dedup
 *     purposes, but by rollup precedence it can NEVER coexist with an in-flight
 *     season: any `import_blocked`/`downloading`/`searching`/
 *     `no_acceptable_release` season wins the parent status outright
 *     (`domain/season_rollup.py`, `_PRECEDENCE_STATUSES`), so the moment real
 *     work starts on any season the parent reads that in-flight status and the
 *     badge counts it. A show only sits at `partially_available` when its
 *     non-done seasons are all dormant (`pending`/`waiting_for_air_date`) or
 *     settled (`failed`/`evicted`/`cancelled`) — and a settled-partial show
 *     (e.g. one season evicted after being watched, ADR-0012) holds that status
 *     indefinitely, so counting it would keep the badge permanently lit with
 *     zero activity. Overstating activity is as dishonest as understating it.
 *
 * This is the single source of truth for "this request is still being worked"
 * so the shell's Requests nav badge (issue #187) can't silently desync from the
 * status vocabulary above — the same reason `glyphKind` reads canonical labels
 * rather than hardcoding strings. The badge count is only ever as truthful as
 * the actor-scoped `/requests` payload it is derived from (own requests for a
 * shared user; every request for an admin — matching what that actor sees on
 * the page).
 */
export const IN_FLIGHT_REQUEST_STATUSES: ReadonlySet<string> = new Set([
  'searching',
  'downloading',
  'no_acceptable_release',
  'completed',
  'import_blocked',
])

export function isInFlightRequestStatus(status: string): boolean {
  return IN_FLIGHT_REQUEST_STATUSES.has(status)
}

/** Tailwind classes per intent (background tint + text + ring), used by StatusBadge. */
export const INTENT_CLASSES: Record<StatusIntent, string> = {
  searching: 'bg-searching/15 text-searching ring-searching/30',
  downloading: 'bg-downloading/15 text-downloading ring-downloading/30',
  available: 'bg-available/15 text-available ring-available/30',
  error: 'bg-error/15 text-error ring-error/30',
  neutral: 'bg-white/8 text-muted ring-white/10',
}

/** Tailwind text-color class per intent, shared by `TileStatusGlyph`'s icon stroke. */
export const INTENT_ICON: Record<StatusIntent, string> = {
  searching: 'text-searching',
  downloading: 'text-downloading',
  available: 'text-available',
  error: 'text-error',
  neutral: 'text-muted',
}

/**
 * Which pictogram `TileStatusGlyph` (issue #135) draws for a tile. Bare
 * `StatusIntent` only has five buckets, but the tile needs six distinct
 * icons — two intents each cover two meanings that must render differently:
 *   - `available` also carries the tv rollup "Partially available" (same
 *     green, half/minus glyph instead of a full check).
 *   - `searching` also carries the Discover-only "processing" fallback
 *     (`libraryStateToPresentation` in tileState.ts), which reads "Requested"
 *     and should look like the plain pending clock, not the active-search
 *     pulse — only a genuine `searching` request status gets the pulse.
 * Compared against the canonical labels in `REQUEST_STATUS` above (not
 * hardcoded strings) so a label rename can't silently desync the glyph from
 * the text it stands in for.
 */
export type GlyphKind = 'pending' | 'searching' | 'downloading' | 'available' | 'partial' | 'error'

export function glyphKind(status: StatusPresentation): GlyphKind {
  switch (status.intent) {
    case 'available':
      return status.label === requestStatus('partially_available').label ? 'partial' : 'available'
    case 'searching':
      return status.label === requestStatus('searching').label ? 'searching' : 'pending'
    case 'downloading':
      return 'downloading'
    case 'error':
      return 'error'
    case 'neutral':
      return 'pending'
  }
}
