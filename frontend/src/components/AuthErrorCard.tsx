import { type ApiError, DETAIL_MESSAGES, toApiError } from '../lib/errors'
import { PlexPinError } from '../lib/plexOAuth'

/**
 * The one honest surface for an auth / setup / access failure. Given either a
 * backend {@link ApiError} envelope or a browser-side {@link PlexPinError}, it
 * shows the crafted, actionable sentence for that failure code (never a bare
 * catch-all sentence), the operator's next step (`hint`) when the envelope
 * carries one, and a collapsed "Technical details" expando with the raw code
 * and any non-secret `diagnostics` for a bug report. Tokens are never part of an
 * envelope, so nothing secret is ever rendered (north star #3).
 */
export function AuthErrorCard({ error }: { error: ApiError | PlexPinError }) {
  // A PlexPinError isn't an API error; route its code through the same copy
  // table so `plex_popup_blocked` reads identically whether it came from the
  // browser or the backend. An ApiError already carries its resolved message.
  const resolved: ApiError =
    error instanceof PlexPinError ? toApiError({ detail: error.code }) : error
  const { code } = resolved
  const message = resolved.message || DETAIL_MESSAGES[code] || code
  const { hint } = resolved
  const diagnostics = resolved.diagnostics ? Object.entries(resolved.diagnostics) : []
  const hasTechnical = code !== '' || diagnostics.length > 0

  return (
    <div
      role="alert"
      className="rounded-xl border border-error/40 bg-error/5 p-4 text-left text-error"
    >
      <p className="text-sm font-semibold">{message}</p>
      {hint ? <p className="mt-2 text-sm text-muted">{hint}</p> : null}
      {hasTechnical ? (
        <details className="mt-3 text-xs text-faint">
          <summary className="cursor-pointer select-none text-muted">Technical details</summary>
          <dl className="mt-2 space-y-1 font-mono">
            <div className="flex gap-2">
              <dt className="text-faint">code</dt>
              <dd className="text-muted break-all">{code}</dd>
            </div>
            {diagnostics.map(([key, value]) => (
              <div key={key} className="flex gap-2">
                <dt className="text-faint">{key}</dt>
                <dd className="text-muted break-all">{value}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </div>
  )
}
