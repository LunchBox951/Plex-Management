import { type ReactNode, useEffect, useState } from 'react'
import {
  useAppKeyStatus,
  useOpsHealth,
  useRevokeAppKey,
  useRotateAppKey,
  usePlexLibraries,
  useSettings,
  useUpdateSettings,
} from '../api/hooks'
import type { SettingsResponse, SettingsUpdate, SubsystemHealthItem } from '../api/types'
import { libraryOptionNote, libraryOptionValue } from '../api/types'
import type { ApiError } from '../lib/errors'
import { AuthErrorCard } from '../components/AuthErrorCard'
import { AdminPageHeader } from '../components/ui/AdminPageHeader'
import { Button } from '../components/ui/Button'
import { LinkButton } from '../components/ui/LinkButton'
import { Field } from '../components/ui/Field'
import { Dialog } from '../components/ui/Dialog'
import { Dot, type DotTone } from '../components/ui/Dot'
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
// Auto-grab worker (ADR-0013) — mirrors the backend default (web/deps.py).
const AUTO_GRAB_ENABLED_DEFAULT = true

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
  // Anime library routing (ADR-0015) — both OPTIONAL, mirroring tv_root
  // exactly: unset means anime imports fall back to movies_root/tv_root.
  anime_movie_root: string
  anime_tv_root: string
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
  // Auto-grab worker (ADR-0013) — the master on/off switch.
  auto_grab_enabled: boolean
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
    anime_movie_root: data.anime_movie_root ?? '',
    anime_tv_root: data.anime_tv_root ?? '',
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
    auto_grab_enabled: data.auto_grab_enabled ?? AUTO_GRAB_ENABLED_DEFAULT,
  }
}

type TextKey =
  | 'plex_url'
  | 'prowlarr_url'
  | 'qbittorrent_url'
  | 'qbittorrent_username'
  | 'movies_root'
  | 'tv_root'
  | 'anime_movie_root'
  | 'anime_tv_root'
type SecretKey = 'plex_token' | 'prowlarr_api_key' | 'qbittorrent_password' | 'tmdb_api_key'
type NumberKey =
  | 'disk_pressure_threshold_percent'
  | 'disk_pressure_target_percent'
  | 'eviction_grace_days'
  | 'eviction_interval_minutes'
  | 'log_retention_days'
type BoolKey = 'eviction_enabled' | 'eviction_proactive_enabled' | 'auto_grab_enabled'

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

/** Canonical configured-service base for the frontend's credential-consent UI.
 *
 * The backend's security boundary ignores host/scheme case, explicit default
 * ports, and a trailing slash when deciding whether a stored credential stays on
 * the same configured base. WHATWG URL performs the first three normalizations;
 * trimming the path's trailing slash completes the comparison. Invalid inputs
 * return null and are conservatively treated as changed until backend validation
 * reports the URL error.
 */
function canonicalServiceBase(value: string): string | null {
  try {
    const parsed = new URL(value)
    if (
      (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') ||
      parsed.username !== '' ||
      parsed.password !== '' ||
      parsed.search !== '' ||
      parsed.hash !== ''
    ) {
      return null
    }
    const path = parsed.pathname.replace(/\/+$/, '')
    return `${parsed.protocol}//${parsed.host}${path}`
  } catch {
    return null
  }
}

function serviceBaseChanged(current: string, stored: string): boolean {
  // An explicit blank disables the integration; it does not authorize a new
  // credential destination. Keep the stored secret untouched so re-enabling at
  // any non-blank base still requires fresh consent.
  if (current === '' || current === stored) return false
  const currentBase = canonicalServiceBase(current)
  const storedBase = canonicalServiceBase(stored)
  return currentBase === null || storedBase === null || currentBase !== storedBase
}

function credentialReentryMessage(credential: string): string {
  return `Re-enter the ${credential} because the service address changed.`
}

type ServiceName = 'plex' | 'prowlarr' | 'qbittorrent' | 'tmdb'

interface ServiceDescriptor {
  name: ServiceName
  title: string
  validateLabel: string
}

const SERVICE_DESCRIPTORS: Record<ServiceName, ServiceDescriptor> = {
  plex: {
    name: 'plex',
    title: 'Plex',
    validateLabel: 'Validate Plex connection',
  },
  prowlarr: {
    name: 'prowlarr',
    title: 'Prowlarr',
    validateLabel: 'Validate Prowlarr connection',
  },
  qbittorrent: {
    name: 'qbittorrent',
    title: 'qBittorrent',
    validateLabel: 'Validate qBittorrent connection',
  },
  tmdb: {
    name: 'tmdb',
    title: 'TMDB',
    validateLabel: 'Validate TMDB connection',
  },
}

const HEALTH_TONE: Record<SubsystemHealthItem['status'], DotTone> = {
  ok: 'ok',
  degraded: 'warn',
  down: 'error',
  not_configured: 'neutral',
}

const HEALTH_LABEL: Record<SubsystemHealthItem['status'], string> = {
  ok: 'Connected',
  degraded: 'Degraded',
  down: 'Down',
  not_configured: 'Not configured',
}

function ServiceCard({
  descriptor,
  subsystem,
  dirty,
  validating,
  onValidate,
  children,
}: {
  descriptor: ServiceDescriptor
  subsystem: SubsystemHealthItem | undefined
  dirty: boolean
  validating: boolean
  onValidate: () => void
  children: ReactNode
}) {
  const tone: DotTone = dirty ? 'neutral' : subsystem ? HEALTH_TONE[subsystem.status] : 'neutral'
  const label = dirty
    ? 'Unsaved changes'
    : subsystem
      ? HEALTH_LABEL[subsystem.status]
      : 'Status unavailable'
  const detailTone =
    subsystem?.status === 'down'
      ? 'text-error'
      : subsystem?.status === 'degraded'
        ? 'text-searching'
        : 'text-muted'

  return (
    <section className="rounded-[10px] border border-hairline bg-surface p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="font-display text-sm font-semibold text-ink">{descriptor.title}</h2>
        <Dot tone={tone} label={label} />
        <Button
          variant="secondary"
          size="sm"
          className="ml-auto shrink-0"
          aria-label={descriptor.validateLabel}
          disabled={dirty}
          // The query is shared by all four cards. A clean card's refetch must
          // never make a dirty card look as though its unsaved candidate is
          // being validated.
          loading={!dirty && validating}
          onClick={onValidate}
        >
          Validate
        </Button>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2">{children}</div>

      {dirty ? (
        <p className="mt-3 text-xs text-faint">
          Save changes before validating this connection.
        </p>
      ) : null}
      {!dirty && subsystem?.detail ? (
        <p className={`mt-3 text-xs break-words ${detailTone}`}>{subsystem.detail}</p>
      ) : null}
      {!dirty && subsystem?.note ? (
        <p className="mt-3 text-xs break-words text-searching">⚠ {subsystem.note}</p>
      ) : null}
    </section>
  )
}

/** The exact one-time reveal caption (ADR-0016). Kept verbatim so it never
 * drifts from what the operator was promised when the key was generated. */
const RECOVERY_KEY_CAPTION =
  'Store this somewhere safe. It can sign you in if plex.tv is down and authenticates API automations.'

/**
 * Settings → Access — the OPT-IN recovery key (ADR-0016). Setup mints nothing;
 * the operator generates a recovery key here on demand. It is a break-glass
 * sign-in for when plex.tv is unreachable, and the `X-Api-Key` that API
 * automations authenticate with. The plaintext is shown EXACTLY ONCE, at
 * generate/rotate time — the status endpoint only ever reports whether a key
 * exists, never the key — so there is no persistent reveal; a lost key is
 * replaced by rotating. Every action requires a currently-authenticated caller
 * (this whole router is admin-gated), so it is not a privilege escalation.
 */
function AccessSection() {
  const status = useAppKeyStatus()
  const rotate = useRotateAppKey()
  const revoke = useRevokeAppKey()
  const { toast } = useToast()
  // The freshly-minted key, held only in this session's memory. Never seeded
  // from a query (status carries no plaintext) and never re-shown after a
  // refetch/navigation — the operator gets exactly one chance to copy it.
  const [revealedKey, setRevealedKey] = useState<string | null>(null)
  const [confirmRevokeOpen, setConfirmRevokeOpen] = useState(false)
  const [copied, setCopied] = useState(false)

  const exists = status.data?.exists === true

  // Generate (no key yet) and Rotate (replace an existing key) are the SAME
  // mint endpoint — the only difference is the label and whether a key existed.
  const mint = async (failTitle: string) => {
    // Drop any key shown by a prior mint BEFORE this one runs: if this rotate
    // fails (e.g. a 409 CAS conflict — someone else already rotated), the old
    // key on screen is now dead, and leaving it visible would have the operator
    // pair a device with a stale key. A success repaints the fresh one.
    setRevealedKey(null)
    setCopied(false)
    try {
      const result = await rotate.mutateAsync()
      setRevealedKey(result.app_api_key)
    } catch (err) {
      toast({ title: failTitle, description: (err as ApiError).message, intent: 'error' })
    }
  }

  const handleRevoke = async () => {
    try {
      await revoke.mutateAsync()
      // The key is gone; drop any still-shown plaintext and close the dialog.
      setRevealedKey(null)
      setConfirmRevokeOpen(false)
    } catch (err) {
      toast({ title: 'Revoke failed', description: (err as ApiError).message, intent: 'error' })
    }
  }

  const copyKey = async () => {
    if (revealedKey === null) return
    // `navigator.clipboard` is undefined outside a secure context (plain http://
    // LAN deployments, a common self-hosted topology); say so honestly and let
    // the operator select + copy the visible key by hand rather than failing.
    if (!navigator.clipboard?.writeText) {
      toast({
        title: 'Clipboard unavailable',
        description: 'Copying needs a secure context (HTTPS). Select the key and copy it manually.',
        intent: 'info',
      })
      return
    }
    try {
      await navigator.clipboard.writeText(revealedKey)
      setCopied(true)
      toast({ title: 'Copied the recovery key', intent: 'success' })
    } catch {
      toast({
        title: 'Copy failed',
        description: 'Select the key and copy it manually.',
        intent: 'info',
      })
    }
  }

  return (
    <section className="rounded-xl border border-hairline bg-surface p-5">
      <h2 className="font-display text-sm font-semibold text-ink">Access</h2>
      <p className="mt-1 text-xs text-faint">
        An optional recovery key. It can sign you in if plex.tv is unreachable, and it's the{' '}
        <code>X-Api-Key</code> that API automations authenticate with. Plex sign-in keeps working
        either way.
      </p>

      <div className="mt-4 flex flex-col gap-4">
        {revealedKey !== null ? (
          <div className="rounded-xl border border-gold/40 bg-bg p-4">
            <div className="text-xs font-medium text-muted">Recovery key</div>
            <code className="mt-1 block font-mono text-sm break-all text-ink">{revealedKey}</code>
            <div className="mt-3">
              <Button variant="secondary" size="sm" onClick={() => void copyKey()}>
                {copied ? 'Copied' : 'Copy'}
              </Button>
            </div>
            <p className="mt-3 text-xs text-faint">{RECOVERY_KEY_CAPTION}</p>
          </div>
        ) : null}

        {status.isLoading ? (
          <p className="text-xs text-faint">Checking for a recovery key…</p>
        ) : status.isError ? (
          // A persistent status-fetch failure must NOT fall through to the
          // no-key "Generate" control: a key may well exist, and the single mint
          // endpoint ROTATES it — silently invalidating every other device and
          // API automation. Surface the honest error and offer only a Retry;
          // never a blind destructive action on unknown state (north star #3).
          <div className="flex flex-col gap-3">
            <AuthErrorCard error={status.error} />
            <div>
              <Button variant="secondary" onClick={() => void status.refetch()}>
                Retry
              </Button>
            </div>
          </div>
        ) : exists ? (
          <div className="flex flex-wrap gap-3">
            <Button
              variant="secondary"
              loading={rotate.isPending}
              onClick={() => void mint('Rotate failed')}
            >
              Rotate
            </Button>
            <Button variant="secondary" onClick={() => setConfirmRevokeOpen(true)}>
              Revoke
            </Button>
          </div>
        ) : (
          <div>
            <Button loading={rotate.isPending} onClick={() => void mint('Generate failed')}>
              Generate recovery key
            </Button>
          </div>
        )}
      </div>

      <Dialog
        open={confirmRevokeOpen}
        onOpenChange={setConfirmRevokeOpen}
        title="Revoke the recovery key?"
      >
        <p className="text-sm text-muted">
          This deletes the recovery key. Any API automation using it — and access-key sign-in on
          every device — stops working until you generate a new one. Plex sign-in is unaffected.
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <Button variant="secondary" onClick={() => setConfirmRevokeOpen(false)}>
            Cancel
          </Button>
          <Button variant="danger" loading={revoke.isPending} onClick={() => void handleRevoke()}>
            Revoke key
          </Button>
        </div>
      </Dialog>
    </section>
  )
}

export function Settings() {
  const { data, isLoading, isError, error, refetch } = useSettings()
  const health = useOpsHealth({ poll: false })
  const update = useUpdateSettings()
  const { toast } = useToast()

  // Controlled state, seeded once the settings have loaded.
  const [form, setForm] = useState<FormState | null>(null)
  // Reveal a typed override instead of the Plex pick-list.
  const [manualPath, setManualPath] = useState(false)
  // Same, for the (optional) tv library folder.
  const [manualTvPath, setManualTvPath] = useState(false)
  // Same, for the (optional) anime library folders (ADR-0015).
  const [manualAnimeMoviePath, setManualAnimeMoviePath] = useState(false)
  const [manualAnimeTvPath, setManualAnimeTvPath] = useState(false)
  useEffect(() => {
    if (data && form === null) setForm(initialForm(data))
  }, [data, form])
  const plexBaseChanged =
    data !== undefined &&
    form !== null &&
    serviceBaseChanged(form.plex_url, data.plex_url ?? '')
  const prowlarrBaseChanged =
    data !== undefined &&
    form !== null &&
    serviceBaseChanged(form.prowlarr_url, data.prowlarr_url ?? '')
  const qbittorrentBaseChanged =
    data !== undefined &&
    form !== null &&
    serviceBaseChanged(form.qbittorrent_url, data.qbittorrent_url ?? '')
  const plexConnectionChanged =
    data !== undefined &&
    form !== null &&
    (form.plex_url !== (data.plex_url ?? '') || form.plex_token.length > 0)
  const libraries = usePlexLibraries(!plexConnectionChanged) // movie folders Plex reports

  if (isLoading || (data && form === null)) {
    return (
      <div className="mx-auto flex w-full max-w-[900px] flex-col gap-6 px-5 py-8 sm:px-8">
        <AdminPageHeader title="Settings" />
        <CenteredSpinner label="Loading settings…" />
      </div>
    )
  }

  if (isError || !data || !form) {
    return (
      <div className="mx-auto flex w-full max-w-[900px] flex-col gap-6 px-5 py-8 sm:px-8">
        <AdminPageHeader title="Settings" />
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

  const plexTokenRequired = plexBaseChanged && data.plex_token === SECRET_SET
  const prowlarrApiKeyRequired =
    prowlarrBaseChanged && data.prowlarr_api_key === SECRET_SET
  const qbittorrentPasswordReentry =
    qbittorrentBaseChanged && data.qbittorrent_password === SECRET_SET

  const subsystem = (name: ServiceName) =>
    health.isError ? undefined : health.data?.subsystems.find((item) => item.name === name)
  const serviceDirty: Record<ServiceName, boolean> = {
    plex: form.plex_url !== (data.plex_url ?? '') || form.plex_token.length > 0,
    prowlarr:
      form.prowlarr_url !== (data.prowlarr_url ?? '') || form.prowlarr_api_key.length > 0,
    qbittorrent:
      form.qbittorrent_url !== (data.qbittorrent_url ?? '') ||
      form.qbittorrent_username !== (data.qbittorrent_username ?? '') ||
      form.qbittorrent_password.length > 0,
    tmdb: form.tmdb_api_key.length > 0,
  }

  const textField = (key: TextKey, label: string, placeholder: string) => (
    <Field
      appearance="admin"
      label={label}
      value={form[key]}
      onChange={(e) => setField(key, e.target.value)}
      placeholder={placeholder}
    />
  )

  const secretField = (
    key: SecretKey,
    label: string,
    isSet: boolean,
    options?: {
      credential: string
      destinationChanged: boolean
      emptyAllowed?: boolean
    },
  ) => {
    const reentryNeeded = isSet && options?.destinationChanged === true
    const required = reentryNeeded && options?.emptyAllowed !== true
    const credential = options?.credential ?? label
    const hint = reentryNeeded
      ? options?.emptyAllowed
        ? `The service address changed. Re-enter the ${credential}, or leave it blank only if the new service uses an empty password.`
        : credentialReentryMessage(credential)
      : isSet
        ? '•••• set (leave blank to keep)'
        : undefined
    return (
      <Field
        appearance="admin"
        label={label}
        type="password"
        autoComplete="off"
        required={required}
        value={form[key]}
        onChange={(e) => setField(key, e.target.value)}
        placeholder={isSet ? '•••• set' : 'Not set'}
        hint={hint}
      />
    )
  }

  const numberField = (
    key: NumberKey,
    label: string,
    bounds: { min: number; max?: number; step?: number },
  ) => (
    <Field
      appearance="admin"
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
    // A stored credential may be reused only on the exact canonical service
    // base. Fail locally with the same correction the backend would return,
    // instead of inviting an avoidable rejected save after the URL changed.
    for (const [required, value, credential] of [
      [plexTokenRequired, form.plex_token, 'Plex token'],
      [prowlarrApiKeyRequired, form.prowlarr_api_key, 'Prowlarr API key'],
    ] as const) {
      if (required && value.trim() === '') {
        toast({
          title: 'Save failed',
          description: credentialReentryMessage(credential),
          intent: 'error',
        })
        return
      }
    }

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
    // Anime roots (ADR-0015) get the SAME treatment: a Plex reconnect must not
    // carry an old server's anime root over any more than it may for
    // movies_root/tv_root.
    const animeMovieRootReselected = form.anime_movie_root !== (data.anime_movie_root ?? '')
    const clearAnimeMovieRoot = plexConnectionChanged && !animeMovieRootReselected
    const animeTvRootReselected = form.anime_tv_root !== (data.anime_tv_root ?? '')
    const clearAnimeTvRoot = plexConnectionChanged && !animeTvRootReselected

    // Plaintext fields always written; secrets only when the user typed a value,
    // so an untouched secret stays the backend's no-op (left unchanged).
    const body: SettingsUpdate = {
      plex_url: form.plex_url,
      prowlarr_url: form.prowlarr_url,
      qbittorrent_url: form.qbittorrent_url,
      qbittorrent_username: form.qbittorrent_username,
      movies_root: clearMoviesRoot ? '' : form.movies_root,
      tv_root: clearTvRoot ? '' : form.tv_root,
      anime_movie_root: clearAnimeMovieRoot ? '' : form.anime_movie_root,
      anime_tv_root: clearAnimeTvRoot ? '' : form.anime_tv_root,
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
      auto_grab_enabled: form.auto_grab_enabled,
    }
    if (form.plex_token) body.plex_token = form.plex_token
    if (form.prowlarr_api_key) body.prowlarr_api_key = form.prowlarr_api_key
    // An empty qBittorrent password is valid. When its destination changes,
    // include the blank field explicitly so it authorizes an empty-password
    // replacement instead of ambiguously asking the backend to reuse the stored
    // password at a new base.
    if (form.qbittorrent_password || qbittorrentPasswordReentry) {
      body.qbittorrent_password = form.qbittorrent_password
    }
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
              ...(clearAnimeMovieRoot ? { anime_movie_root: '' } : {}),
              ...(clearAnimeTvRoot ? { anime_tv_root: '' } : {}),
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
    <div className="mx-auto flex w-full max-w-[900px] flex-col gap-6 px-5 py-8 sm:px-8">
      <AdminPageHeader
        title="Settings"
        actions={
          <Button loading={update.isPending} onClick={() => void handleSave()}>
            Save changes
          </Button>
        }
      />

      <div className="flex flex-col gap-5">
        <ServiceCard
          descriptor={SERVICE_DESCRIPTORS.plex}
          subsystem={subsystem('plex')}
          dirty={serviceDirty.plex}
          validating={health.isFetching}
          onValidate={() => void health.refetch()}
        >
          {textField('plex_url', 'URL', 'http://localhost:32400')}
          {secretField('plex_token', 'Token', data.plex_token === SECRET_SET, {
            credential: 'Plex token',
            destinationChanged: plexBaseChanged,
          })}
        </ServiceCard>

        <ServiceCard
          descriptor={SERVICE_DESCRIPTORS.prowlarr}
          subsystem={subsystem('prowlarr')}
          dirty={serviceDirty.prowlarr}
          validating={health.isFetching}
          onValidate={() => void health.refetch()}
        >
          {textField('prowlarr_url', 'URL', 'http://localhost:9696')}
          {secretField('prowlarr_api_key', 'API key', data.prowlarr_api_key === SECRET_SET, {
            credential: 'Prowlarr API key',
            destinationChanged: prowlarrBaseChanged,
          })}
        </ServiceCard>

        <ServiceCard
          descriptor={SERVICE_DESCRIPTORS.qbittorrent}
          subsystem={subsystem('qbittorrent')}
          dirty={serviceDirty.qbittorrent}
          validating={health.isFetching}
          onValidate={() => void health.refetch()}
        >
          {textField('qbittorrent_url', 'URL', 'http://localhost:8080')}
          {textField('qbittorrent_username', 'Username', 'admin')}
          <div className="sm:col-span-2">
            {secretField(
              'qbittorrent_password',
              'Password',
              data.qbittorrent_password === SECRET_SET,
              {
                credential: 'qBittorrent password',
                destinationChanged: qbittorrentBaseChanged,
                emptyAllowed: true,
              },
            )}
          </div>
        </ServiceCard>

        <ServiceCard
          descriptor={SERVICE_DESCRIPTORS.tmdb}
          subsystem={subsystem('tmdb')}
          dirty={serviceDirty.tmdb}
          validating={health.isFetching}
          onValidate={() => void health.refetch()}
        >
          <div className="sm:col-span-2">
            {secretField('tmdb_api_key', 'API key', data.tmdb_api_key === SECRET_SET)}
          </div>
        </ServiceCard>

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
                      value={libraryOptionValue(lib)}
                      disabled={lib.writable === false}
                    >
                      {lib.title} — {lib.path}
                      {libraryOptionNote(lib)}
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
                      value={libraryOptionValue(lib)}
                      disabled={lib.writable === false}
                    >
                      {lib.title} — {lib.path}
                      {libraryOptionNote(lib)}
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

        {/* Anime library routing (ADR-0015) — both OPTIONAL and reuse the SAME
            Plex library list as Movies/TV above (an anime library is an
            ordinary Plex movie/tv section, just a different one). Unset =
            anime imports fall back to the Movies/TV roots above, identical to
            behavior before this feature existed. Neither field gates Save. */}
        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Anime library</h2>
          <p className="mt-1 text-xs text-faint">
            Optional. Route anime movies/episodes to a separate Plex library instead of the
            Movies/TV folders above. Leave unset to keep anime in the normal libraries.
          </p>
          <div className="mt-4 flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              {plexConnectionChanged ? (
                // Same guard as Movies/TV above: while the Plex URL/token is being
                // changed, the (disabled) libraries query still serves the OLD
                // server's cached list — a stale anime root must not be selectable
                // mid-reconnect any more than a stale movies/tv root.
                <select
                  aria-label="Anime movies library folder"
                  className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none disabled:text-faint"
                  value=""
                  disabled
                  onChange={() => undefined}
                >
                  <option value="">No anime movies library folder…</option>
                </select>
              ) : !manualAnimeMoviePath && movieLibraries.length > 0 ? (
                <>
                  <select
                    aria-label="Anime movies library folder"
                    className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
                    value={form.anime_movie_root}
                    onChange={(e) => setField('anime_movie_root', e.target.value)}
                  >
                    <option value="">No anime movies library folder…</option>
                    {movieLibraries.map((lib) => (
                      <option
                        key={`${lib.section_key}:${lib.path}`}
                        value={libraryOptionValue(lib)}
                        disabled={lib.writable === false}
                      >
                        {lib.title} — {lib.path}
                        {libraryOptionNote(lib)}
                        {lib.writable === false ? ' · not writable by the app' : ''}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="self-start text-xs text-gold hover:underline"
                    onClick={() => setManualAnimeMoviePath(true)}
                  >
                    Use a custom path instead
                  </button>
                </>
              ) : (
                <>
                  {textField('anime_movie_root', 'Anime movies library folder', '/library/anime-movies')}
                  {movieLibraries.length > 0 ? (
                    <button
                      type="button"
                      className="self-start text-xs text-gold hover:underline"
                      onClick={() => setManualAnimeMoviePath(false)}
                    >
                      ← Pick from a Plex library instead
                    </button>
                  ) : null}
                </>
              )}
            </div>
            <div className="flex flex-col gap-2">
              {plexConnectionChanged ? (
                <select
                  aria-label="Anime TV library folder"
                  className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none disabled:text-faint"
                  value=""
                  disabled
                  onChange={() => undefined}
                >
                  <option value="">No anime TV library folder…</option>
                </select>
              ) : !manualAnimeTvPath && tvLibraries.length > 0 ? (
                <>
                  <select
                    aria-label="Anime TV library folder"
                    className="h-11 rounded-xl bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
                    value={form.anime_tv_root}
                    onChange={(e) => setField('anime_tv_root', e.target.value)}
                  >
                    <option value="">No anime TV library folder…</option>
                    {tvLibraries.map((lib) => (
                      <option
                        key={`${lib.section_key}:${lib.path}`}
                        value={libraryOptionValue(lib)}
                        disabled={lib.writable === false}
                      >
                        {lib.title} — {lib.path}
                        {libraryOptionNote(lib)}
                        {lib.writable === false ? ' · not writable by the app' : ''}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="self-start text-xs text-gold hover:underline"
                    onClick={() => setManualAnimeTvPath(true)}
                  >
                    Use a custom path instead
                  </button>
                </>
              ) : (
                <>
                  {textField('anime_tv_root', 'Anime TV library folder', '/library/anime-tv')}
                  {tvLibraries.length > 0 ? (
                    <button
                      type="button"
                      className="self-start text-xs text-gold hover:underline"
                      onClick={() => setManualAnimeTvPath(false)}
                    >
                      ← Pick from a Plex library instead
                    </button>
                  ) : null}
                </>
              )}
            </div>
          </div>
        </section>

        {/* ADR-0013 — the auto-grab worker's master switch. North star #1: turn
            the background request→search→grab loop off with a button, never a
            terminal. The manual Grab button still works when this is off. */}
        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Automation</h2>
          <p className="mt-1 text-xs text-faint">
            Controls the background worker that searches and grabs approved requests.
          </p>
          <div className="mt-4 flex flex-col gap-3">
            {checkboxField(
              'auto_grab_enabled',
              'Enable auto-grab',
              'Automatically search and grab pending requests. Turn off to grab ' +
                'only via the manual button on each title.',
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
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {numberField('disk_pressure_threshold_percent', 'Pressure threshold (%)', {
                min: 0,
                max: 100,
              })}
              {numberField('disk_pressure_target_percent', 'Pressure target (%)', {
                min: 0,
                max: 100,
              })}
              {numberField('eviction_grace_days', 'Eviction grace period (days)', { min: 0 })}
              {numberField('eviction_interval_minutes', 'Eviction check interval (minutes)', {
                // The schema requires gt=0 (not ge=0 like the other knobs), so
                // the client bound must mirror that exactly: min:0 would let
                // the browser accept 0 (or an emptied field, which coerces to
                // 0) and only then get rejected by the backend's 422.
                min: 0.1,
                step: 0.1,
              })}
              {numberField('log_retention_days', 'Log retention (days)', { min: 0 })}
            </div>
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

      <AccessSection />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <section className="rounded-[10px] border border-hairline bg-surface p-4">
          <h2 className="font-display text-sm font-semibold text-ink">Quality profile</h2>
          <p className="mt-1 text-xs text-faint">
            Ordered qualities with a hard cutoff. Read-only in v1.
          </p>
          <div className="mt-4">
            <LinkButton variant="secondary" size="sm" to="/quality">
              View profile
            </LinkButton>
          </div>
        </section>

        <section className="rounded-[10px] border border-hairline bg-surface p-4">
          <h2 className="font-display text-sm font-semibold text-ink">Blocklist</h2>
          <p className="mt-1 text-xs text-faint">
            Review releases that Plex Manager must not grab again.
          </p>
          <div className="mt-4">
            <LinkButton variant="secondary" size="sm" to="/blocklist">
              Manage blocklist
            </LinkButton>
          </div>
        </section>
      </div>
    </div>
  )
}
