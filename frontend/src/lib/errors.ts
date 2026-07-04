/**
 * Turn an `openapi-fetch` error body into a human, honest message. The backend
 * returns machine-readable `detail` codes (e.g. `indexer_unavailable`,
 * `no_acceptable_release`); we surface a readable sentence and keep the raw code
 * available for callers that branch on it. Never swallow — always say something.
 */
const DETAIL_MESSAGES: Record<string, string> = {
  setup_required: 'Finish first-run setup to continue.',
  service_not_configured: 'That service is not configured yet. Add it in Settings.',
  invalid_api_key: 'Your access key is no longer valid. Re-run setup.',
  invalid_setup_token: 'Enter the setup token from your server environment.',
  already_initialized: 'This install is already set up.',
  indexer_unavailable: 'The indexer (Prowlarr) is unavailable right now. Try again shortly.',
  indexer_rate_limited: 'The indexer is rate-limiting requests. Try again shortly.',
  tmdb_unavailable: 'TMDB is unavailable right now. Try again shortly.',
  tmdb_auth_failed: 'TMDB rejected the API key. Re-check it in Settings.',
  qbittorrent_unavailable: 'qBittorrent is unavailable right now. Try again shortly.',
  qbittorrent_auth_failed: 'qBittorrent rejected the credentials. Re-check them in Settings.',
  torrent_source_unresolvable:
    'That release’s download link did not resolve to a usable torrent. Try another release.',
  upstream_error: 'An upstream service failed. Try again shortly.',
  media_not_found: 'That title could not be found on TMDB.',
  request_not_found: 'That request no longer exists.',
  request_not_active: 'That request is already finished or was superseded.',
  no_acceptable_release: 'No acceptable release was found. You can re-search later.',
  release_not_found: 'That release is no longer available to grab.',
  no_grab_source: 'The download client refused the grab.',
  already_downloading: 'That request already has an active download.',
  grab_hash_unresolved: 'The download client took the grab but returned no hash.',
  download_not_found: 'That download no longer exists.',
  invalid_state_transition: 'That action is not allowed from the current state.',
  blocklist_entry_not_found: 'That blocklist entry no longer exists.',
  app_key_changed:
    'The app key changed while this request was in flight — refresh and retry.',
}

export interface ApiError {
  code: string
  message: string
  status: number
}

interface DetailBody {
  detail?: unknown
}

interface ExtractedDetail {
  code: string
  /** An explicit, already-human message (e.g. a FastAPI validation `msg`). */
  message?: string
}

function extractDetail(error: unknown): ExtractedDetail | undefined {
  if (typeof error === 'object' && error !== null && 'detail' in error) {
    const detail = (error as DetailBody).detail
    if (typeof detail === 'string') return { code: detail }
    // FastAPI validation errors: detail is a list of {msg, loc}; the msg is
    // already human, so surface it verbatim rather than humanizing a code.
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown }
      if (typeof first.msg === 'string') return { code: 'validation_error', message: first.msg }
    }
  }
  return undefined
}

export function toApiError(error: unknown, status = 0): ApiError {
  const extracted = extractDetail(error)
  const code = extracted?.code ?? 'unknown_error'
  const message =
    extracted?.message ??
    DETAIL_MESSAGES[code] ??
    (code === 'unknown_error' ? 'Something went wrong. Please try again.' : humanize(code))
  return { code, message, status }
}

function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
}
