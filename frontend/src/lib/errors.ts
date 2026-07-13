/**
 * Turn an `openapi-fetch` error body into a human, honest message. The backend
 * returns a structured envelope — a machine-readable `detail` code plus an
 * operator-facing `message`, an optional `hint`, and optional non-secret
 * `diagnostics` (see `web/errors.py`). We surface the crafted sentence and keep
 * the raw code + diagnostics available for the UI. Never swallow, never a bare
 * catch-all sentence: an unmapped code surfaces the code itself and a
 * detail-less failure names the HTTP status (north star #3).
 */
const DETAIL_MESSAGES: Record<string, string> = {
  // --- request/download pipeline (pre-existing) ---
  setup_required: 'Finish first-run setup to continue.',
  invalid_api_key: 'Your access key is no longer valid. Re-run setup.',
  recovery_key_rejected: 'That access key was rejected. Double-check it and try again.',
  invalid_setup_token: 'Enter the setup token from your server environment.',
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
  removal_in_progress:
    'A removal for this download is already in progress — retry after it completes.',
  blocklist_entry_not_found: 'That blocklist entry no longer exists.',
  media_root_unavailable:
    'The library folder for this title isn’t reachable. Make sure it’s mounted and visible to Plex Manager, then try again from Settings.',
  library_root_unreachable:
    'That library folder isn’t visible to Plex Manager. Pick a folder under a mounted volume (usually /media), or fix the container’s volume mounts.',
  not_reportable: 'This title can’t be reported right now — it isn’t imported or available yet.',
  active_duplicate: 'A newer request for this title already exists. Act on that one instead.',
  not_relocatable:
    'This download isn’t a path-invisible import-blocked row — there’s nothing to relocate.',
  downloads_root_unavailable:
    'No downloads root could be derived for this server. Fix the Docker volume mounts (or set the downloads root) and try again.',
  relocation_superseded:
    'The move was requested, but this row was already re-blocked with a different reason — refresh to see the current status.',

  // --- Plex sign-in / setup / access (auth error-honesty layer) ---
  plex_tv_unreachable_browser:
    "Your browser couldn't reach plex.tv. Check your connection and any ad blockers, then try again.",
  plex_tv_unreachable_server:
    "Plex Manager's server couldn't reach plex.tv. plex.tv may be down, or the server has no internet access.",
  plex_tv_bad_response:
    'plex.tv answered in an unexpected way. Try again; if it keeps happening, plex.tv may be having issues.',
  plex_popup_blocked:
    'Your browser blocked the Plex sign-in popup. Allow popups for this site and try again.',
  plex_popup_closed: 'The Plex sign-in window was closed before finishing. Try again.',
  plex_pin_expired: 'The Plex sign-in expired. Try again.',
  plex_token_invalid: 'plex.tv rejected the sign-in token. Sign in again.',
  no_owned_servers:
    'Your Plex account does not own any Plex Media Server. Sign in with the account that owns the server this app should manage.',
  setup_already_claimed:
    'Setup was already started by a different Plex account. Finish setup from that account, or reset the database.',
  server_not_owned:
    'Your Plex account does not own this server. Pick a server you own, or sign in as the owner.',
  server_unreachable_from_backend:
    "Plex Manager's server can't reach this address (your browser reaching it isn't enough). If Plex Manager runs in Docker, localhost points at the container — use the host's IP or host.docker.internal.",
  server_identity_failed: 'That address answered, but not like a Plex server. Check the URL.',
  server_access_denied:
    "Your Plex account doesn't have access to this server. Ask the owner to share it with you.",
  session_expired: 'Your session expired. Sign in again.',
  session_required: 'Sign in with Plex to continue.',
  sign_in_throttled: 'Too many sign-in attempts. Wait a minute and try again.',
  plex_account_required:
    'Server discovery needs a Plex-signed-in admin. Sign in with Plex first.',
  service_not_configured:
    "This service isn't configured yet. Finish setup, or add it from the Settings page.",
  csrf_token_required: 'The request was blocked by CSRF protection.',
  already_initialized: 'Setup is already complete. Change settings from the Settings page instead.',
  app_key_not_set: 'No recovery key exists. Generate one from Settings → Access.',
  app_key_changed: 'The recovery key changed while you were rotating it. Refresh and try again.',
}

export { DETAIL_MESSAGES }

export interface ApiError {
  code: string
  message: string
  status: number
  /** Operator-facing next step from the envelope, when the backend supplied one. */
  hint?: string
  /** Non-secret context (host, status, reason, …) for the "Technical details" expando. */
  diagnostics?: Record<string, string>
}

interface DetailBody {
  detail?: unknown
  message?: unknown
  hint?: unknown
  diagnostics?: unknown
}

interface ExtractedDetail {
  code: string
  /** An explicit, already-human message (envelope `message` or a validation `msg`). */
  message?: string
  hint?: string
  diagnostics?: Record<string, string>
}

/** Keep only string→string pairs; the envelope's `diagnostics` is `dict[str, str]`. */
function extractDiagnostics(value: unknown): Record<string, string> | undefined {
  if (typeof value !== 'object' || value === null) return undefined
  const result: Record<string, string> = {}
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === 'string') result[key] = entry
  }
  return Object.keys(result).length > 0 ? result : undefined
}

function extractDetail(error: unknown): ExtractedDetail | undefined {
  if (typeof error === 'object' && error !== null && 'detail' in error) {
    const body = error as DetailBody
    const detail = body.detail
    if (typeof detail === 'string') {
      const extracted: ExtractedDetail = { code: detail }
      if (typeof body.message === 'string') extracted.message = body.message
      if (typeof body.hint === 'string') extracted.hint = body.hint
      const diagnostics = extractDiagnostics(body.diagnostics)
      if (diagnostics) extracted.diagnostics = diagnostics
      return extracted
    }
    // FastAPI validation errors: detail is a list of {msg, loc}; the msg is
    // already human, so surface it verbatim rather than humanizing a code.
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown }
      if (typeof first.msg === 'string') return { code: 'validation_error', message: first.msg }
    }
  }
  return undefined
}

/** snake_case → sentence case: `no_acceptable_release` → `No acceptable release`. */
export function humanize(value: string): string {
  return value.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
}

export function toApiError(error: unknown, status = 0): ApiError {
  const extracted = extractDetail(error)
  const code = extracted?.code ?? 'unknown_error'
  // An explicit envelope message wins; then the crafted per-code copy; then, for
  // a detail-less failure, the HTTP status; otherwise a humanized rendering of the
  // code so an unmapped pipeline code (e.g. a correction/request verb) still reads
  // as a phrase, not raw snake_case. `code` itself stays the raw machine value for
  // technical display. Never a generic string — nothing is swallowed (north star
  // #3), and the banned catch-all sentence never reappears.
  const message =
    extracted?.message ??
    DETAIL_MESSAGES[code] ??
    (code === 'unknown_error'
      ? `The server returned an unexpected error (HTTP ${String(status)}).`
      : humanize(code))
  const result: ApiError = { code, message, status }
  if (extracted?.hint) result.hint = extracted.hint
  if (extracted?.diagnostics) result.diagnostics = extracted.diagnostics
  return result
}

/**
 * Narrow an unknown thrown value to a normalized {@link ApiError}. Rejections
 * from `unwrap`/`ensureOk` are already `ApiError`s carrying their crafted
 * message; callers use this to pass those through unchanged while routing any
 * other throw through {@link toApiError} — so nothing ever renders `undefined`.
 */
export function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as ApiError).code === 'string' &&
    typeof (value as ApiError).message === 'string' &&
    typeof (value as ApiError).status === 'number'
  )
}
