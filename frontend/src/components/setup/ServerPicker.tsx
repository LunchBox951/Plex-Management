import { useMemo, useState } from 'react'
import { useSetupPlexServers, useValidatePlex } from '../../api/hooks'
import type { PlexServerConnection, PlexLibraryOption } from '../../api/types'
import { type ApiError, isApiError, toApiError } from '../../lib/errors'
import { AuthErrorCard } from '../AuthErrorCard'
import { Button } from '../ui/Button'
import { Field } from '../ui/Field'
import { CenteredSpinner } from '../ui/feedback'

/**
 * A verified Plex server, handed up to the wizard once the backend has confirmed
 * the signed-in admin owns it. `token` is present only for a custom-credential
 * override; omitted, `POST /setup/complete` reuses the admin's stored OAuth token.
 */
export interface VerifiedServer {
  url: string
  token?: string
  machine_identifier: string
  libraries: PlexLibraryOption[]
}

/** One connection flattened out of an owned server, tagged with its parent name. */
interface FlatConnection {
  serverName: string
  uri: string
  local: boolean
  reachable: boolean
}

/**
 * Rank reachable connections above unreachable ones, and (within each) local
 * above remote — so the default selection is the address most likely to work,
 * while an unreachable one stays SELECTABLE (the operator may know a probe was a
 * false negative). A stable order keeps the list predictable across re-probes.
 */
function rank(a: FlatConnection, b: FlatConnection): number {
  if (a.reachable !== b.reachable) return a.reachable ? -1 : 1
  if (a.local !== b.local) return a.local ? -1 : 1
  return 0
}

function flatten(
  servers: { name: string; connections: PlexServerConnection[] }[],
): FlatConnection[] {
  return servers
    .flatMap((server) =>
      server.connections.map((conn) => ({
        serverName: server.name,
        uri: conn.uri,
        local: conn.local,
        reachable: conn.status === 'ok',
      })),
    )
    .sort(rank)
}

/** "Apollo — http://127.0.0.1:32400 (local, reachable)" — every fact surfaced, none hidden. */
function connectionLabel(conn: FlatConnection): string {
  const place = conn.local ? 'local' : 'remote'
  const reach = conn.reachable ? 'reachable' : 'unreachable'
  return `${conn.serverName} — ${conn.uri} (${place}, ${reach})`
}

function asDisplayError(err: unknown): ApiError {
  return isApiError(err) ? err : toApiError(err)
}

/**
 * Step 2 of the wizard: pick the Plex server this app manages. The signed-in
 * admin's OWNED servers are listed (each connection probed + ranked); "Custom"
 * reveals a URL + optional token override for an address plex.tv doesn't
 * advertise. Verify calls the ownership-checking probe; on success the verified
 * server (with its libraries + machine identifier) is handed up via `onVerified`,
 * on failure the honest {@link AuthErrorCard} is shown.
 */
export function ServerPicker({ onVerified }: { onVerified: (server: VerifiedServer) => void }) {
  const serversQuery = useSetupPlexServers(true)
  const validate = useValidatePlex()

  const connections = useMemo(
    () => flatten(serversQuery.data?.servers ?? []),
    [serversQuery.data],
  )
  const hasServers = connections.length > 0

  const [custom, setCustom] = useState(false)
  const [selectedUri, setSelectedUri] = useState('')
  const [customUrl, setCustomUrl] = useState('')
  const [customToken, setCustomToken] = useState('')
  const [error, setError] = useState<ApiError | undefined>(undefined)

  // No servers discovered -> there is nothing to pick, so custom entry is the
  // only path; force it on rather than showing an empty select.
  const inCustom = custom || !hasServers
  // Default the select to the top-ranked (reachable, local) connection until the
  // operator picks another — avoids a leading empty option being "chosen".
  const effectiveUri = selectedUri || connections[0]?.uri || ''

  const verify = async () => {
    setError(undefined)
    const url = inCustom ? customUrl.trim() : effectiveUri
    if (url === '') return
    const token = inCustom ? customToken.trim() : ''
    const body = token !== '' ? { url, token } : { url }
    try {
      const res = await validate.mutateAsync(body)
      if (!res.ok || !res.machine_identifier) {
        // Reachable but not a verifiable Plex server we own: surface the honest
        // code rather than silently advancing on a half-answer.
        setError(toApiError({ detail: res.detail ?? 'server_identity_failed', message: res.message }))
        return
      }
      onVerified({
        ...body,
        machine_identifier: res.machine_identifier,
        libraries: res.libraries ?? [],
      })
    } catch (err) {
      setError(asDisplayError(err))
    }
  }

  const verifyDisabled =
    validate.isPending || (inCustom ? customUrl.trim() === '' : effectiveUri === '')

  return (
    <section className="rounded-2xl border border-hairline bg-surface p-5">
      <h2 className="font-display text-lg font-bold text-ink">Pick your Plex server</h2>
      <p className="mt-1 text-sm text-muted">
        These are the Plex Media Servers your account owns. Choose the one this app should manage.
      </p>

      {serversQuery.isLoading ? (
        <div className="mt-4">
          <CenteredSpinner label="Finding your servers…" />
        </div>
      ) : (
        <div className="mt-4 flex flex-col gap-4">
          {/* An ERROR (a 409 plex_account_required, a 5xx) is NOT "you own no
              servers": surface the honest failure rather than silently rendering
              the empty-state hint below, which would misattribute the outage to
              the account. Custom entry stays available regardless. */}
          {serversQuery.isError ? (
            <AuthErrorCard error={asDisplayError(serversQuery.error)} />
          ) : null}

          {hasServers && !inCustom ? (
            <select
              aria-label="Plex server"
              className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
              value={effectiveUri}
              onChange={(e) => setSelectedUri(e.target.value)}
            >
              {connections.map((conn) => (
                <option
                  key={conn.uri}
                  value={conn.uri}
                  className={conn.reachable ? undefined : 'text-faint'}
                >
                  {connectionLabel(conn)}
                </option>
              ))}
            </select>
          ) : (
            <div className="flex flex-col gap-3">
              <Field
                label="Server URL"
                type="text"
                placeholder="http://localhost:32400"
                value={customUrl}
                onChange={(e) => setCustomUrl(e.target.value)}
              />
              <Field
                label="Plex token (optional)"
                type="password"
                autoComplete="off"
                hint="Uses your Plex sign-in token unless you provide one."
                value={customToken}
                onChange={(e) => setCustomToken(e.target.value)}
              />
            </div>
          )}

          {hasServers ? (
            <label className="flex items-center gap-2 text-sm text-muted">
              <input
                type="checkbox"
                className="size-4 accent-gold"
                checked={custom}
                onChange={(e) => {
                  setCustom(e.target.checked)
                  setError(undefined)
                }}
              />
              Enter a custom server URL instead
            </label>
          ) : serversQuery.isError ? null : (
            <p className="text-xs text-faint">
              Your account owns no auto-discoverable server — enter its URL directly.
            </p>
          )}

          <div className="flex items-center gap-3">
            <Button loading={validate.isPending} disabled={verifyDisabled} onClick={() => void verify()}>
              Verify server
            </Button>
          </div>

          {error ? <AuthErrorCard error={error} /> : null}
        </div>
      )}
    </section>
  )
}
