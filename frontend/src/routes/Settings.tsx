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

// Mirror the backend's operability defaults (web/deps.py — same constants
// Status.tsx uses for the same settings). `SettingsResponse` returns `null` for
// an unset knob (it mirrors what is actually STORED, not the effective
// fallback), so the form prefills with the value that's actually being
// applied rather than a misleading blank.
const DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT = 90
const DISK_PRESSURE_TARGET_PERCENT_DEFAULT = 80
const EVICTION_GRACE_DAYS_DEFAULT = 30
const EVICTION_ENABLED_DEFAULT = true
const EVICTION_PROACTIVE_ENABLED_DEFAULT = false
const EVICTION_INTERVAL_MINUTES_DEFAULT = 30
const LOG_RETENTION_DAYS_DEFAULT = 7

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
  tv_root: string
  // Operability (ADR-0012) — stored as strings so a number input can hold an
  // in-progress edit (e.g. a momentarily empty field while retyping) without
  // fighting the controlled-input value; parsed back to a number on save.
  disk_pressure_threshold_percent: string
  disk_pressure_target_percent: string
  eviction_grace_days: string
  eviction_enabled: boolean
  eviction_proactive_enabled: boolean
  eviction_interval_minutes: string
  log_retention_days: string
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
    tv_root: data.tv_root ?? '',
    disk_pressure_threshold_percent: String(
      data.disk_pressure_threshold_percent ?? DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT,
    ),
    disk_pressure_target_percent: String(
      data.disk_pressure_target_percent ?? DISK_PRESSURE_TARGET_PERCENT_DEFAULT,
    ),
    eviction_grace_days: String(data.eviction_grace_days ?? EVICTION_GRACE_DAYS_DEFAULT),
    eviction_enabled: data.eviction_enabled ?? EVICTION_ENABLED_DEFAULT,
    eviction_proactive_enabled:
      data.eviction_proactive_enabled ?? EVICTION_PROACTIVE_ENABLED_DEFAULT,
    eviction_interval_minutes: String(
      data.eviction_interval_minutes ?? EVICTION_INTERVAL_MINUTES_DEFAULT,
    ),
    log_retention_days: String(data.log_retention_days ?? LOG_RETENTION_DAYS_DEFAULT),
  }
}

type TextKey =
  | 'plex_url'
  | 'prowlarr_url'
  | 'qbittorrent_url'
  | 'qbittorrent_username'
  | 'movies_root'
  | 'tv_root'
type SecretKey = 'plex_token' | 'prowlarr_api_key' | 'qbittorrent_password' | 'tmdb_api_key'
type NumberKey =
  | 'disk_pressure_threshold_percent'
  | 'disk_pressure_target_percent'
  | 'eviction_grace_days'
  | 'eviction_interval_minutes'
  | 'log_retention_days'
type BoolKey = 'eviction_enabled' | 'eviction_proactive_enabled'

// Operator-facing label per numeric operability knob — reused by the Save
// validation below so an invalid field's toast names it the same way the form
// does (R5-1).
const NUMBER_FIELD_LABELS: Record<NumberKey, string> = {
  disk_pressure_threshold_percent: 'Pressure threshold (%)',
  disk_pressure_target_percent: 'Pressure target (%)',
  eviction_grace_days: 'Eviction grace period (days)',
  eviction_interval_minutes: 'Eviction check interval (minutes)',
  log_retention_days: 'Log retention (days)',
}

/** ``true`` only for a non-blank string that parses to a finite number.
 *
 * ``Number('')`` is ``0`` (NOT ``NaN``), so a blank/whitespace-only input must
 * be rejected explicitly — otherwise a cleared numeric field would silently
 * coerce to ``0`` on save (retention 0 = prune logs immediately; grace 0 =
 * watched media eligible right away; threshold 0 = disk always "over
 * pressure") while the UI still toasts success.
 */
function isValidNumberInput(value: string): boolean {
  return value.trim() !== '' && Number.isFinite(Number(value))
}

const Heading = () => <h1 className="font-display text-2xl font-extrabold">Settings</h1>

export function Settings() {
  const { data, isLoading, isError, error, refetch } = useSettings()
  const update = useUpdateSettings()
  const { toast } = useToast()

  // Controlled state, seeded once the settings have loaded.
  const [form, setForm] = useState<FormState | null>(null)
  // Reveal a typed override instead of the Plex pick-list.
  const [manualPath, setManualPath] = useState(false)
  // Same, for the (optional) tv library folder.
  const [manualTvPath, setManualTvPath] = useState(false)
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

  const setField = (key: TextKey | SecretKey | NumberKey, value: string) =>
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev))

  const setBoolField = (key: BoolKey, value: boolean) =>
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

  const numberField = (
    key: NumberKey,
    label: string,
    bounds: { min: number; max?: number; step?: number },
  ) => (
    <Field
      label={label}
      type="number"
      min={bounds.min}
      max={bounds.max}
      step={bounds.step ?? 1}
      value={form[key]}
      onChange={(e) => setField(key, e.target.value)}
    />
  )

  const checkboxField = (key: BoolKey, label: string, hint: string) => (
    <label className="flex items-start gap-2 text-sm text-ink">
      <input
        type="checkbox"
        className="mt-0.5 h-4 w-4 shrink-0 rounded ring-1 ring-inset ring-white/10"
        checked={form[key]}
        onChange={(e) => setBoolField(key, e.target.checked)}
      />
      <span className="flex flex-col">
        {label}
        <span className="text-xs text-faint">{hint}</span>
      </span>
    </label>
  )

  const handleSave = async () => {
    // Every numeric operability knob must be a non-empty, finite number BEFORE
    // any save is attempted. An emptied input (or stray non-numeric text) must
    // never silently coerce to 0 via `Number('')` — that would flip a real
    // safety knob (immediate log prune, immediate eviction eligibility, an
    // always-"over-pressure" disk) while the toast still says "Settings
    // saved". Abort the whole save (no mutateAsync call) and show a visible,
    // specific error the moment the FIRST invalid field is found.
    for (const key of Object.keys(NUMBER_FIELD_LABELS) as NumberKey[]) {
      if (!isValidNumberInput(form[key])) {
        toast({
          title: 'Save failed',
          description: `Enter a number for ${NUMBER_FIELD_LABELS[key]}.`,
          intent: 'error',
        })
        return
      }
    }

    // A library folder is discovered against a *specific* Plex server. If the
    // operator just changed the Plex connection (URL or a freshly typed token)
    // but hasn't re-picked a folder, don't carry the OLD server's movies_root /
    // tv_root over with the new creds: clear whichever wasn't re-picked. ''
    // reads as unset server-side (a visible "library not configured" 409, not a
    // silent wrong-path write), so the picker — refetched against the new
    // connection on save — forces a fresh, valid selection before any import
    // can scan a path off the old Plex. (Omitting the field would NOT fix this:
    // the backend leaves absent fields unchanged, so the stale path would stay
    // persisted.) tv_root gets the SAME treatment as movies_root even though
    // it's optional — a stale tv path is exactly as wrong as a stale movie one.
    const plexConnectionChanged =
      form.plex_url !== (data.plex_url ?? '') || form.plex_token.length > 0
    const moviesRootReselected = form.movies_root !== (data.movies_root ?? '')
    const clearMoviesRoot = plexConnectionChanged && !moviesRootReselected
    const tvRootReselected = form.tv_root !== (data.tv_root ?? '')
    const clearTvRoot = plexConnectionChanged && !tvRootReselected

    // Plaintext fields always written; secrets only when the user typed a value,
    // so an untouched secret stays the backend's no-op (left unchanged).
    const body: SettingsUpdate = {
      plex_url: form.plex_url,
      prowlarr_url: form.prowlarr_url,
      qbittorrent_url: form.qbittorrent_url,
      qbittorrent_username: form.qbittorrent_username,
      movies_root: clearMoviesRoot ? '' : form.movies_root,
      tv_root: clearTvRoot ? '' : form.tv_root,
      // Operability (ADR-0012) knobs are always written (not secrets, and there
      // is no "leave unchanged" state to preserve like the passwords above) so
      // the form is a faithful, web-only editor for every safety knob. A
      // malformed/out-of-range value is a visible 422 (surfaced via the catch
      // below), never a silently-clamped or dropped write.
      disk_pressure_threshold_percent: Number(form.disk_pressure_threshold_percent),
      disk_pressure_target_percent: Number(form.disk_pressure_target_percent),
      eviction_grace_days: Number(form.eviction_grace_days),
      eviction_enabled: form.eviction_enabled,
      eviction_proactive_enabled: form.eviction_proactive_enabled,
      eviction_interval_minutes: Number(form.eviction_interval_minutes),
      log_retention_days: Number(form.log_retention_days),
    }
    if (form.plex_token) body.plex_token = form.plex_token
    if (form.prowlarr_api_key) body.prowlarr_api_key = form.prowlarr_api_key
    if (form.qbittorrent_password) body.qbittorrent_password = form.qbittorrent_password
    if (form.tmdb_api_key) body.tmdb_api_key = form.tmdb_api_key

    try {
      await update.mutateAsync(body)
      toast({ title: 'Settings saved', intent: 'success' })
      // Clear secret inputs so they reflect the now-masked stored values, and
      // drop movies_root/tv_root from the form when we cleared them server-side
      // so the refreshed picker shows the placeholder (and a follow-up save
      // can't re-write the stale path).
      setForm((prev) =>
        prev
          ? {
              ...prev,
              plex_token: '',
              prowlarr_api_key: '',
              qbittorrent_password: '',
              tmdb_api_key: '',
              ...(clearMoviesRoot ? { movies_root: '' } : {}),
              ...(clearTvRoot ? { tv_root: '' } : {}),
            }
          : prev,
      )
    } catch (err) {
      const apiError = err as ApiError
      toast({ title: 'Save failed', description: apiError.message, intent: 'error' })
    }
  }

  // `libraries.data` carries BOTH movie and tv folders, each tagged by
  // `section_type` — split per-picker below.
  const movieLibraries = libraries.data?.filter((lib) => lib.section_type === 'movie') ?? []
  const tvLibraries = libraries.data?.filter((lib) => lib.section_type === 'tv') ?? []

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
            ) : !manualPath && movieLibraries.length > 0 ? (
              <>
                <select
                  aria-label="Movies library folder"
                  className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
                  value={form.movies_root}
                  onChange={(e) => setField('movies_root', e.target.value)}
                >
                  <option value="">Choose a movie library folder…</option>
                  {movieLibraries.map((lib) => (
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
                {movieLibraries.length > 0 ? (
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

        {/* TV is entirely optional (ADR-0011) — an install with only movies_root
            configured is left alone; this section never blocks Save. */}
        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">TV Library</h2>
          <p className="mt-1 text-xs text-faint">
            Where imported tv seasons are placed. Leave unset if you don't request tv shows.
          </p>
          <div className="mt-4 flex flex-col gap-2">
            {plexConnectionChanged ? (
              <select
                aria-label="TV library folder"
                className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none disabled:text-faint"
                value=""
                disabled
                onChange={() => undefined}
              >
                <option value="">No tv library folder…</option>
              </select>
            ) : !manualTvPath && tvLibraries.length > 0 ? (
              <>
                <select
                  aria-label="TV library folder"
                  className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
                  value={form.tv_root}
                  onChange={(e) => setField('tv_root', e.target.value)}
                >
                  <option value="">No tv library folder…</option>
                  {tvLibraries.map((lib) => (
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
                  onClick={() => setManualTvPath(true)}
                >
                  Use a custom path instead
                </button>
              </>
            ) : (
              <>
                {textField('tv_root', 'TV library folder', '/library/tv')}
                {tvLibraries.length > 0 ? (
                  <button
                    type="button"
                    className="self-start text-xs text-gold hover:underline"
                    onClick={() => setManualTvPath(false)}
                  >
                    ← Pick from a Plex library instead
                  </button>
                ) : null}
              </>
            )}
          </div>
        </section>

        {/* ADR-0012 — disk-pressure eviction + log retention. These are the
            safety knobs the background sweep (web/app.py _eviction_loop) reads;
            north star #2 requires every one of them be reachable here, with no
            terminal/DB fallback. */}
        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Eviction &amp; logs</h2>
          <p className="mt-1 text-xs text-faint">
            Controls the automatic disk-pressure sweep and how long ops logs are kept.
          </p>
          <div className="mt-4 flex flex-col gap-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {numberField('disk_pressure_threshold_percent', 'Pressure threshold (%)', {
                min: 0,
                max: 100,
              })}
              {numberField('disk_pressure_target_percent', 'Pressure target (%)', {
                min: 0,
                max: 100,
              })}
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              {numberField('eviction_grace_days', 'Eviction grace period (days)', { min: 0 })}
              {numberField('eviction_interval_minutes', 'Eviction check interval (minutes)', {
                // The schema requires gt=0 (not ge=0 like the other knobs), so
                // the client bound must mirror that exactly: min:0 would let
                // the browser accept 0 (or an emptied field, which coerces to
                // 0) and only then get rejected by the backend's 422.
                min: 0.1,
                step: 0.1,
              })}
            </div>
            {numberField('log_retention_days', 'Log retention (days)', { min: 0 })}
            <div className="flex flex-col gap-3">
              {checkboxField(
                'eviction_enabled',
                'Enable automatic eviction',
                'Run the background pressure sweep on the interval above.',
              )}
              {checkboxField(
                'eviction_proactive_enabled',
                'Proactive eviction',
                'Reclaims eagerly: evicts every watched, past-grace, unpinned title or ' +
                  'season it can find, regardless of disk pressure — not just enough to ' +
                  "reach the target. Mark anything you don't want touched as \"Keep forever.\"",
              )}
            </div>
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
