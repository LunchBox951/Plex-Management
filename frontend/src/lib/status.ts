/**
 * Per-title / per-download status presentation.
 *
 * The backend's `status` fields are free strings carrying the canonical
 * `RequestStatus` / `DownloadState` enum values (see the backend's
 * domain/state machine). The design handoff (§4) collapses these onto five
 * semantic colors. This map is the single place that translation lives, so
 * every badge/pill across the app reads identically. Unknown values fall back to
 * a neutral intent rather than throwing — honesty over silence.
 */
export type StatusIntent = 'searching' | 'downloading' | 'available' | 'error' | 'neutral'

export interface StatusPresentation {
  label: string
  intent: StatusIntent
}

const REQUEST_STATUS: Record<string, StatusPresentation> = {
  pending: { label: 'Requested', intent: 'neutral' },
  searching: { label: 'Searching', intent: 'searching' },
  no_acceptable_release: { label: 'No release', intent: 'error' },
  downloading: { label: 'Downloading', intent: 'downloading' },
  import_blocked: { label: 'Import blocked', intent: 'error' },
  completed: { label: 'Finalizing', intent: 'downloading' },
  available: { label: 'In library', intent: 'available' },
  // tv only: the show's rollup when SOME (not all) tracked seasons are available
  // (domain.season_rollup.rollup_status). Never a per-season status itself — a
  // single SeasonRequest only ever carries the statuses above.
  partially_available: { label: 'Partially available', intent: 'available' },
  failed: { label: 'Failed', intent: 'error' },
}

const DOWNLOAD_STATUS: Record<string, StatusPresentation> = {
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

function lookup(table: Record<string, StatusPresentation>, status: string): StatusPresentation {
  return table[status] ?? { label: humanize(status), intent: 'neutral' }
}

export function requestStatus(status: string): StatusPresentation {
  return lookup(REQUEST_STATUS, status)
}

export function downloadStatus(status: string): StatusPresentation {
  return lookup(DOWNLOAD_STATUS, status)
}

/** Tailwind classes per intent (background tint + text + ring), used by StatusBadge. */
export const INTENT_CLASSES: Record<StatusIntent, string> = {
  searching: 'bg-searching/15 text-searching ring-searching/30',
  downloading: 'bg-downloading/15 text-downloading ring-downloading/30',
  available: 'bg-available/15 text-available ring-available/30',
  error: 'bg-error/15 text-error ring-error/30',
  neutral: 'bg-white/8 text-muted ring-white/10',
}
