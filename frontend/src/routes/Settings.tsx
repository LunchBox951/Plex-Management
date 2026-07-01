import { useEffect, useState } from 'react'
import { usePlexLibraries, useSettings, useUpdateSettings } from '../api/hooks'
import type { SettingsResponse, SettingsUpdate } from '../api/types'
import type { ApiError } from '../lib/errors'
import { Button } from '../components/ui/Button'
import { LinkButton } from '../components/ui/LinkButton'
import { Field } from '../components/ui/Field'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'

/** A secret value is rendered by the backend as this sentinel when configured. */
const SECRET_SET = '***'

interface FormState {
  plex_url: string
  plex_token: string
  prowlarr_url: string
  prowlarr_api_key: string
  qbittorrent_url: string
  qbittorrent_username: string
  qbittorrent_password: string
  tmdb_api_key: string
  movies_root: string
}

/** Plaintext fields prefill from current values; secret inputs always start empty. */
function initialForm(data: SettingsResponse): FormState {
  return {
    plex_url: data.plex_url ?? '',
    plex_token: '',
    prowlarr_url: data.prowlarr_url ?? '',
    prowlarr_api_key: '',
    qbittorrent_url: data.qbittorrent_url ?? '',
    qbittorrent_username: data.qbittorrent_username ?? '',
    qbittorrent_password: '',
    tmdb_api_key: '',
    movies_root: data.movies_root ?? '',
  }
}

type TextKey =
  | 'plex_url'
  | 'prowlarr_url'
  | 'qbittorrent_url'
  | 'qbittorrent_username'
  | 'movies_root'
type SecretKey = 'plex_token' | 'prowlarr_api_key' | 'qbittorrent_password' | 'tmdb_api_key'

const Heading = () => <h1 className="font-display text-2xl font-extrabold">Settings</h1>

export function Settings() {
  const { data, isLoading, isError, error, refetch } = useSettings()
  const update = useUpdateSettings()
  const { toast } = useToast()

  // Controlled state, seeded once the settings have loaded.
  const [form, setForm] = useState<FormState | null>(null)
  // Reveal a typed override instead of the Plex pick-list.
  const [manualPath, setManualPath] = useState(false)
  useEffect(() => {
    if (data && form === null) setForm(initialForm(data))
  }, [data, form])
  const plexConnectionChanged =
    data !== undefined &&
    form !== null &&
    (form.plex_url !== (data.plex_url ?? '') || form.plex_token.length > 0)
  const libraries = usePlexLibraries(!plexConnectionChanged) // movie folders Plex reports

  if (isLoading || (data && form === null)) {
    return (
      <div className="flex flex-col gap-6">
        <Heading />
        <CenteredSpinner label="Loading settings…" />
      </div>
    )
  }

  if (isError || !data || !form) {
    return (
      <div className="flex flex-col gap-6">
        <Heading />
        <StateMessage
          tone="error"
          title="Couldn't load settings"
          message={error?.message ?? 'Settings are unavailable right now.'}
          action={
            <Button variant="secondary" onClick={() => void refetch()}>
              Retry
            </Button>
          }
        />
      </div>
    )
  }

  const setField = (key: keyof FormState, value: string) =>
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev))

  const textField = (key: TextKey, label: string, placeholder: string) => (
    <Field
      label={label}
      value={form[key]}
      onChange={(e) => setField(key, e.target.value)}
      placeholder={placeholder}
    />
  )

  const secretField = (key: SecretKey, label: string, isSet: boolean) => (
    <Field
      label={label}
      type="password"
      autoComplete="off"
      value={form[key]}
      onChange={(e) => setField(key, e.target.value)}
      placeholder={isSet ? '•••• set' : 'Not set'}
      hint={isSet ? '•••• set (leave blank to keep)' : undefined}
    />
  )

  const handleSave = async () => {
    // A library folder is discovered against a *specific* Plex server. If the
    // operator just changed the Plex connection, don't carry any folder from the
    // OLD server's picker over with the new creds. '' reads as unset server-side.
    const clearMoviesRoot = plexConnectionChanged

    // Plaintext fields always written; secrets only when the user typed a value,
    // so an untouched secret stays the backend's no-op (left unchanged).
    const body: SettingsUpdate = {
      plex_url: form.plex_url,
      prowlarr_url: form.prowlarr_url,
      qbittorrent_url: form.qbittorrent_url,
      qbittorrent_username: form.qbittorrent_username,
      movies_root: clearMoviesRoot ? '' : form.movies_root,
    }
    if (form.plex_token) body.plex_token = form.plex_token
    if (form.prowlarr_api_key) body.prowlarr_api_key = form.prowlarr_api_key
    if (form.qbittorrent_password) body.qbittorrent_password = form.qbittorrent_password
    if (form.tmdb_api_key) body.tmdb_api_key = form.tmdb_api_key

    try {
      await update.mutateAsync(body)
      toast({ title: 'Settings saved', intent: 'success' })
      // Clear secret inputs so they reflect the now-masked stored values, and
      // drop movies_root from the form when we cleared it server-side so the
      // refreshed picker shows the placeholder (and a follow-up save can't
      // re-write the stale path).
      setForm((prev) =>
        prev
          ? {
              ...prev,
              plex_token: '',
              prowlarr_api_key: '',
              qbittorrent_password: '',
              tmdb_api_key: '',
              ...(clearMoviesRoot ? { movies_root: '' } : {}),
            }
          : prev,
      )
    } catch (err) {
      const apiError = err as ApiError
      toast({ title: 'Save failed', description: apiError.message, intent: 'error' })
    }
  }

  return (
    <div className="flex flex-col gap-6">
      <Heading />

      <div className="flex flex-col gap-5">
        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Plex</h2>
          <div className="mt-4 flex flex-col gap-4">
            {textField('plex_url', 'URL', 'http://localhost:32400')}
            {secretField('plex_token', 'Token', data.plex_token === SECRET_SET)}
          </div>
        </section>

        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Prowlarr</h2>
          <div className="mt-4 flex flex-col gap-4">
            {textField('prowlarr_url', 'URL', 'http://localhost:9696')}
            {secretField('prowlarr_api_key', 'API key', data.prowlarr_api_key === SECRET_SET)}
          </div>
        </section>

        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">qBittorrent</h2>
          <div className="mt-4 flex flex-col gap-4">
            {textField('qbittorrent_url', 'URL', 'http://localhost:8080')}
            {textField('qbittorrent_username', 'Username', 'admin')}
            {secretField('qbittorrent_password', 'Password', data.qbittorrent_password === SECRET_SET)}
          </div>
        </section>

        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">TMDB</h2>
          <div className="mt-4 flex flex-col gap-4">
            {secretField('tmdb_api_key', 'API key', data.tmdb_api_key === SECRET_SET)}
          </div>
        </section>

        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Library</h2>
          <p className="mt-1 text-xs text-faint">Where imported movies are placed.</p>
          <div className="mt-4 flex flex-col gap-2">
            {plexConnectionChanged ? (
              <select
                aria-label="Movies library folder"
                className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none disabled:text-faint"
                value=""
                disabled
                onChange={() => undefined}
              >
                <option value="">Choose a movie library folder…</option>
              </select>
            ) : !manualPath && libraries.isError ? (
              <StateMessage
                tone="error"
                title="Couldn't load Plex libraries"
                message={
                  (libraries.error as ApiError | undefined)?.message ??
                  'Library folders are unavailable right now.'
                }
                action={
                  <div className="flex flex-wrap gap-2">
                    <Button variant="secondary" onClick={() => void libraries.refetch()}>
                      Retry
                    </Button>
                    <Button variant="secondary" onClick={() => setManualPath(true)}>
                      Use custom path
                    </Button>
                  </div>
                }
              />
            ) : !manualPath && libraries.data && libraries.data.length > 0 ? (
              <>
                <select
                  aria-label="Movies library folder"
                  className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
                  value={form.movies_root}
                  onChange={(e) => setField('movies_root', e.target.value)}
                >
                  <option value="">Choose a movie library folder…</option>
                  {libraries.data.map((lib) => (
                    <option
                      key={`${lib.section_key}:${lib.path}`}
                      value={lib.path}
                      disabled={lib.writable === false}
                    >
                      {lib.title} — {lib.path}
                      {lib.writable === false ? ' · not writable by the app' : ''}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className="self-start text-xs text-gold hover:underline"
                  onClick={() => setManualPath(true)}
                >
                  Use a custom path instead
                </button>
              </>
            ) : (
              <>
                {textField('movies_root', 'Movies library folder', '/library/movies')}
                {libraries.data && libraries.data.length > 0 ? (
                  <button
                    type="button"
                    className="self-start text-xs text-gold hover:underline"
                    onClick={() => setManualPath(false)}
                  >
                    ← Pick from a Plex library instead
                  </button>
                ) : null}
              </>
            )}
          </div>
        </section>
      </div>

      <div>
        <Button loading={update.isPending} onClick={() => void handleSave()}>
          Save changes
        </Button>
      </div>

      <section className="rounded-xl border border-hairline bg-surface p-5">
        <h2 className="font-display text-sm font-semibold text-ink">More</h2>
        <div className="mt-4 flex flex-wrap gap-3">
          <LinkButton variant="secondary" to="/blocklist">
            Manage blocklist
          </LinkButton>
          <LinkButton variant="secondary" to="/quality">
            View quality profile
          </LinkButton>
        </div>
      </section>
    </div>
  )
}
