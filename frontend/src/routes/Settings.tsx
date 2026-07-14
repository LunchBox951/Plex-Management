import { type ReactNode, useEffect, useState } from 'react'
import {
  useActiveSessions,
  useAppKeyStatus,
  useOpsHealth,
  useRevokeAppKey,
  useRevokeRecoverySessions,
  useRevokeUserSessions,
  useRotateAppKey,
  usePlexLibraries,
  useSettings,
  useUpdateSettings,
} from '../api/hooks'
import type {
  ActiveSessionUser,
  AutomaticUpdateWeekday,
  SettingsResponse,
  SettingsUpdate,
  SubsystemHealthItem,
} from '../api/types'
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
// The row-count companion to log_retention_days (issue #152) — mirrors the
// backend default (web/deps.py LOG_MAX_ROWS_DEFAULT).
const LOG_MAX_ROWS_DEFAULT = 100000
// Auto-grab worker (ADR-0013) — mirrors the backend default (web/deps.py).
const AUTO_GRAB_ENABLED_DEFAULT = true
// Auto-grab timing (issue #150) — mirrors the backend defaults/bounds
// (web/deps.py AUTO_GRAB_INTERVAL_SECONDS_DEFAULT /
// AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT, web/settings_bounds.py).
const AUTO_GRAB_INTERVAL_SECONDS_DEFAULT = 60
const AUTO_GRAB_INTERVAL_SECONDS_MIN = 15
const AUTO_GRAB_INTERVAL_SECONDS_MAX = 3600
const AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT = 5
const AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX = 50
// Automatic container updates (ADR-0024) are opt-in. The scheduling defaults
// mirror the backend except for timezone: a fresh browser contributes its IANA
// zone when the operator first saves, with UTC as the fail-closed fallback.
const AUTOMATIC_UPDATES_ENABLED_DEFAULT = false
const AUTOMATIC_UPDATE_WINDOW_START_DEFAULT = '03:00'
const AUTOMATIC_UPDATE_WINDOW_END_DEFAULT = '05:00'
const AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT = true
const UPDATE_TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/
const UPDATE_WEEKDAYS: readonly {
  value: AutomaticUpdateWeekday
  label: string
}[] = [
  { value: 'monday', label: 'Mon' },
  { value: 'tuesday', label: 'Tue' },
  { value: 'wednesday', label: 'Wed' },
  { value: 'thursday', label: 'Thu' },
  { value: 'friday', label: 'Fri' },
  { value: 'saturday', label: 'Sat' },
  { value: 'sunday', label: 'Sun' },
]

function isIanaTimezone(value: string): boolean {
  if (value.trim() === '') return false
  try {
    new Intl.DateTimeFormat('en', { timeZone: value }).format()
    return true
  } catch {
    return false
  }
}

function browserTimezone(): string {
  try {
    const value = Intl.DateTimeFormat().resolvedOptions().timeZone
    return typeof value === 'string' && isIanaTimezone(value) ? value : 'UTC'
  } catch {
    return 'UTC'
  }
}
const WATCHLIST_SYNC_ENABLED_DEFAULT = true
const WATCHLIST_SYNC_INTERVAL_MINUTES_DEFAULT = 15
const WATCHLIST_SYNC_INTERVAL_MINUTES_MAX = 10_080

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
  log_max_rows: string
  // Auto-grab worker (ADR-0013) — the master on/off switch.
  auto_grab_enabled: boolean
  // Auto-grab timing (issue #150) — the worker's cycle cadence and per-cycle
  // Prowlarr search budget.
  auto_grab_interval_seconds: string
  auto_grab_max_searches_per_cycle: string
  automatic_updates_enabled: boolean
  automatic_update_timezone: string
  automatic_update_weekdays: AutomaticUpdateWeekday[]
  automatic_update_window_start: string
  automatic_update_window_end: string
  automatic_update_idle_only: boolean
  watchlist_sync_enabled: boolean
  watchlist_sync_interval_minutes: string
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
    log_max_rows: String(data.log_max_rows ?? LOG_MAX_ROWS_DEFAULT),
    auto_grab_enabled: data.auto_grab_enabled ?? AUTO_GRAB_ENABLED_DEFAULT,
    auto_grab_interval_seconds: String(
      data.auto_grab_interval_seconds ?? AUTO_GRAB_INTERVAL_SECONDS_DEFAULT,
    ),
    auto_grab_max_searches_per_cycle: String(
      data.auto_grab_max_searches_per_cycle ?? AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_DEFAULT,
    ),
    automatic_updates_enabled:
      data.automatic_updates_enabled ?? AUTOMATIC_UPDATES_ENABLED_DEFAULT,
    automatic_update_timezone: data.automatic_update_timezone ?? browserTimezone(),
    automatic_update_weekdays:
      data.automatic_update_weekdays ?? UPDATE_WEEKDAYS.map(({ value }) => value),
    automatic_update_window_start:
      data.automatic_update_window_start ?? AUTOMATIC_UPDATE_WINDOW_START_DEFAULT,
    automatic_update_window_end:
      data.automatic_update_window_end ?? AUTOMATIC_UPDATE_WINDOW_END_DEFAULT,
    automatic_update_idle_only:
      data.automatic_update_idle_only ?? AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT,
    watchlist_sync_enabled: data.watchlist_sync_enabled ?? WATCHLIST_SYNC_ENABLED_DEFAULT,
    watchlist_sync_interval_minutes: String(
      data.watchlist_sync_interval_minutes ?? WATCHLIST_SYNC_INTERVAL_MINUTES_DEFAULT,
    ),
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
  | 'automatic_update_timezone'
  | 'automatic_update_window_start'
  | 'automatic_update_window_end'
type SecretKey = 'plex_token' | 'prowlarr_api_key' | 'qbittorrent_password' | 'tmdb_api_key'
type NumberKey =
  | 'disk_pressure_threshold_percent'
  | 'disk_pressure_target_percent'
  | 'eviction_grace_days'
  | 'eviction_interval_minutes'
  | 'watchlist_sync_interval_minutes'
  | 'log_retention_days'
  | 'log_max_rows'
  | 'auto_grab_interval_seconds'
  | 'auto_grab_max_searches_per_cycle'
type BoolKey =
  | 'eviction_enabled'
  | 'eviction_proactive_enabled'
  | 'auto_grab_enabled'
  | 'automatic_updates_enabled'
  | 'automatic_update_idle_only'
  | 'watchlist_sync_enabled'

// Operator-facing label per numeric operability knob — reused by the Save
// validation below so an invalid field's toast names it the same way the form
// does (R5-1).
const NUMBER_FIELD_LABELS: Record<NumberKey, string> = {
  disk_pressure_threshold_percent: 'Pressure threshold (%)',
  disk_pressure_target_percent: 'Pressure target (%)',
  eviction_grace_days: 'Eviction grace period (days)',
  eviction_interval_minutes: 'Eviction check interval (minutes)',
  watchlist_sync_interval_minutes: 'Watchlist sync interval (minutes)',
  log_retention_days: 'Log retention (days)',
  log_max_rows: 'Log retention (max rows)',
  auto_grab_interval_seconds: 'Auto-grab check interval (seconds)',
  auto_grab_max_searches_per_cycle: 'Auto-grab searches per cycle',
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

/** Format a session's last-seen timestamp, honestly blank when never recorded. */
function formatLastSeen(value: string | null): string {
  if (value === null) return 'unknown'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return 'unknown'
  return parsed.toLocaleString()
}

/**
 * Settings → Signed-in sessions (issue #56). ADR-0016 sessions validate LOCALLY
 * — plex.tv is never on the per-request path — so a removed or demoted Plex user
 * keeps working until their session is revoked. This is the web-operable lever
 * for that: the admin sees who is currently signed in and can cut a user's
 * sessions on demand. Revoking your OWN account (flagged "you") simply signs you
 * out — never a lockout, since Plex sign-in and the recovery key can always mint
 * a fresh session.
 */
function SessionsSection() {
  const sessions = useActiveSessions()
  const revoke = useRevokeUserSessions()
  const revokeRecovery = useRevokeRecoverySessions()
  const { toast } = useToast()
  const [confirmUser, setConfirmUser] = useState<ActiveSessionUser | null>(null)
  const [confirmRecovery, setConfirmRecovery] = useState(false)

  const handleRevoke = async (user: ActiveSessionUser) => {
    try {
      await revoke.mutateAsync(user.user_id)
      setConfirmUser(null)
      toast({ title: `Revoked ${user.username}'s sessions`, intent: 'success' })
    } catch (err) {
      toast({ title: 'Revoke failed', description: (err as ApiError).message, intent: 'error' })
    }
  }

  const handleRevokeRecovery = async () => {
    try {
      await revokeRecovery.mutateAsync()
      setConfirmRecovery(false)
      toast({ title: 'Revoked recovery sessions', intent: 'success' })
    } catch (err) {
      toast({ title: 'Revoke failed', description: (err as ApiError).message, intent: 'error' })
    }
  }

  const users = sessions.data?.users ?? []
  const recovery = sessions.data?.recovery ?? null

  return (
    <section className="rounded-xl border border-hairline bg-surface p-5">
      <h2 className="font-display text-sm font-semibold text-ink">Signed-in sessions</h2>
      <p className="mt-1 text-xs text-faint">
        Everyone with an active browser session. Sessions are validated locally, so revoking is how
        you cut off a removed or demoted Plex user before their session expires.
      </p>

      <div className="mt-4">
        {sessions.isLoading ? (
          <p className="text-xs text-faint">Loading sessions…</p>
        ) : sessions.isError ? (
          <div className="flex flex-col gap-3">
            <AuthErrorCard error={sessions.error} />
            <div>
              <Button variant="secondary" onClick={() => void sessions.refetch()}>
                Retry
              </Button>
            </div>
          </div>
        ) : users.length === 0 && recovery === null ? (
          <p className="text-xs text-faint">No one is signed in right now.</p>
        ) : (
          <ul className="flex flex-col divide-y divide-hairline">
            {users.map((user) => (
              <li
                key={user.user_id}
                className="flex flex-wrap items-center justify-between gap-3 py-3"
              >
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium text-ink">{user.username}</span>
                    {user.is_admin ? (
                      <span className="rounded bg-bg px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
                        admin
                      </span>
                    ) : null}
                    {user.is_current_user ? (
                      <span className="rounded bg-gold/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-gold">
                        you
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-0.5 text-xs text-faint">
                    {user.session_count} active {user.session_count === 1 ? 'session' : 'sessions'} ·
                    last seen {formatLastSeen(user.last_seen_at)}
                  </div>
                </div>
                <Button variant="secondary" size="sm" onClick={() => setConfirmUser(user)}>
                  Revoke
                </Button>
              </li>
            ))}
            {recovery !== null ? (
              <li className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium text-ink">Recovery key</span>
                    <span className="rounded bg-bg px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
                      admin
                    </span>
                    <span className="rounded bg-bg px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-muted">
                      no Plex identity
                    </span>
                  </div>
                  <div className="mt-0.5 text-xs text-faint">
                    {recovery.session_count} active{' '}
                    {recovery.session_count === 1 ? 'session' : 'sessions'} · last seen{' '}
                    {formatLastSeen(recovery.last_seen_at)}
                  </div>
                </div>
                <Button variant="secondary" size="sm" onClick={() => setConfirmRecovery(true)}>
                  Revoke
                </Button>
              </li>
            ) : null}
          </ul>
        )}
      </div>

      <Dialog
        open={confirmUser !== null}
        onOpenChange={(open) => {
          if (!open) setConfirmUser(null)
        }}
        title={confirmUser === null ? '' : `Revoke ${confirmUser.username}'s sessions?`}
      >
        <p className="text-sm text-muted">
          {confirmUser?.is_current_user
            ? 'This is your own account — revoking signs you out of this browser. You can sign back in with Plex (or the recovery key) at any time.'
            : 'Every one of this user’s active sessions is cut immediately. They can sign in again with Plex whenever access allows.'}
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <Button variant="secondary" onClick={() => setConfirmUser(null)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={revoke.isPending}
            onClick={() => {
              if (confirmUser !== null) void handleRevoke(confirmUser)
            }}
          >
            Revoke sessions
          </Button>
        </div>
      </Dialog>

      <Dialog
        open={confirmRecovery}
        onOpenChange={setConfirmRecovery}
        title="Revoke recovery sessions?"
      >
        <p className="text-sm text-muted">
          Every active recovery session (from exchanging the recovery key) is cut immediately. If
          you are signed in with the recovery key yourself, this signs you out. Exchange the
          recovery key again to get back in.
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <Button variant="secondary" onClick={() => setConfirmRecovery(false)}>
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={revokeRecovery.isPending}
            onClick={() => void handleRevokeRecovery()}
          >
            Revoke sessions
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

  const toggleUpdateWeekday = (weekday: AutomaticUpdateWeekday, selected: boolean) =>
    setForm((prev) => {
      if (!prev) return prev
      const selectedDays = new Set(prev.automatic_update_weekdays)
      if (selected) selectedDays.add(weekday)
      else selectedDays.delete(weekday)
      return {
        ...prev,
        automatic_update_weekdays: UPDATE_WEEKDAYS.map(({ value }) => value).filter((value) =>
          selectedDays.has(value),
        ),
      }
    })

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
    const watchlistInterval = Number(form.watchlist_sync_interval_minutes)
    if (watchlistInterval <= 0 || watchlistInterval > WATCHLIST_SYNC_INTERVAL_MINUTES_MAX) {
      toast({
        title: 'Save failed',
        description: `Watchlist sync interval must be greater than 0 and at most ${WATCHLIST_SYNC_INTERVAL_MINUTES_MAX} minutes.`,
        intent: 'error',
      })
      return
    }

    if (!isIanaTimezone(form.automatic_update_timezone)) {
      toast({
        title: 'Save failed',
        description: 'Enter a valid IANA timezone, such as America/Toronto or UTC.',
        intent: 'error',
      })
      return
    }
    if (form.automatic_update_weekdays.length === 0) {
      toast({
        title: 'Save failed',
        description: 'Select at least one automatic update weekday.',
        intent: 'error',
      })
      return
    }
    if (
      !UPDATE_TIME_PATTERN.test(form.automatic_update_window_start) ||
      !UPDATE_TIME_PATTERN.test(form.automatic_update_window_end)
    ) {
      toast({
        title: 'Save failed',
        description: 'Enter automatic update times in 24-hour HH:MM format.',
        intent: 'error',
      })
      return
    }
    if (form.automatic_update_window_start === form.automatic_update_window_end) {
      toast({
        title: 'Save failed',
        description: 'Automatic update window start and end must differ.',
        intent: 'error',
      })
      return
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
      log_max_rows: Number(form.log_max_rows),
      auto_grab_enabled: form.auto_grab_enabled,
      auto_grab_interval_seconds: Number(form.auto_grab_interval_seconds),
      auto_grab_max_searches_per_cycle: Number(form.auto_grab_max_searches_per_cycle),
      automatic_updates_enabled: form.automatic_updates_enabled,
      automatic_update_timezone: form.automatic_update_timezone.trim(),
      automatic_update_weekdays: form.automatic_update_weekdays,
      automatic_update_window_start: form.automatic_update_window_start,
      automatic_update_window_end: form.automatic_update_window_end,
      automatic_update_idle_only: form.automatic_update_idle_only,
      watchlist_sync_enabled: form.watchlist_sync_enabled,
      watchlist_sync_interval_minutes: Number(form.watchlist_sync_interval_minutes),
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
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {numberField(
                'auto_grab_interval_seconds',
                'Auto-grab check interval (seconds)',
                {
                  min: AUTO_GRAB_INTERVAL_SECONDS_MIN,
                  max: AUTO_GRAB_INTERVAL_SECONDS_MAX,
                  step: 1,
                },
              )}
              {numberField('auto_grab_max_searches_per_cycle', 'Auto-grab searches per cycle', {
                min: 1,
                max: AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX,
                step: 1,
              })}
            </div>
            <p className="text-xs text-faint">
              How often the worker checks for due requests ({AUTO_GRAB_INTERVAL_SECONDS_MIN}–
              {AUTO_GRAB_INTERVAL_SECONDS_MAX}s) and how many Prowlarr searches it runs per check
              (1–{AUTO_GRAB_MAX_SEARCHES_PER_CYCLE_MAX}), protecting the indexer from a burst.
            </p>
            {checkboxField(
              'watchlist_sync_enabled',
              'Enable Plex watchlist sync',
              'Create requests from every signed-in user’s Plex watchlist and protect them from eviction.',
            )}
            {numberField(
              'watchlist_sync_interval_minutes',
              'Watchlist sync interval (minutes)',
              { min: 0.1, max: WATCHLIST_SYNC_INTERVAL_MINUTES_MAX, step: 0.1 },
            )}
          </div>
        </section>

        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h2 className="font-display text-sm font-semibold text-ink">Automatic updates</h2>
          <p className="mt-1 text-xs text-faint">
            Opt in to first-party container updates. The updater honors this local-time window and
            coordinates a short maintenance drain before replacing Plex Manager.
          </p>
          <div className="mt-4 flex flex-col gap-4">
            {checkboxField(
              'automatic_updates_enabled',
              'Enable automatic updates',
              'Disabled by default. Manual Check and Update actions still require the updater sidecar.',
            )}

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div className="sm:col-span-3">
                <Field
                  appearance="admin"
                  label="IANA timezone"
                  value={form.automatic_update_timezone}
                  onChange={(e) => setField('automatic_update_timezone', e.target.value)}
                  placeholder="America/Toronto"
                  hint="Schedule calculations use this timezone, including daylight-saving changes."
                />
              </div>
              <Field
                appearance="admin"
                label="Window start"
                type="time"
                value={form.automatic_update_window_start}
                onChange={(e) => setField('automatic_update_window_start', e.target.value)}
              />
              <Field
                appearance="admin"
                label="Window end"
                type="time"
                value={form.automatic_update_window_end}
                onChange={(e) => setField('automatic_update_window_end', e.target.value)}
              />
            </div>

            <fieldset>
              <legend className="font-mono text-[10.5px] leading-none font-semibold uppercase tracking-[0.12em] text-faint">
                Starting weekdays
              </legend>
              <div className="mt-2 flex flex-wrap gap-2">
                {UPDATE_WEEKDAYS.map(({ value, label }) => (
                  <label
                    key={value}
                    className="flex items-center gap-1.5 rounded-lg bg-surface-deep px-2.5 py-2 font-mono text-xs text-ink ring-1 ring-inset ring-white/10"
                  >
                    <input
                      type="checkbox"
                      checked={form.automatic_update_weekdays.includes(value)}
                      onChange={(e) => toggleUpdateWeekday(value, e.target.checked)}
                    />
                    {label}
                  </label>
                ))}
              </div>
              <p className="mt-2 text-xs text-faint">
                An overnight window belongs to the weekday on which it starts. Select at least one
                day.
              </p>
            </fieldset>

            {checkboxField(
              'automatic_update_idle_only',
              'Wait for critical work to become idle',
              'Playback and active downloads do not block an update. Import, move, scan, correction, purge, eviction, and administrative mutations do.',
            )}

            <p className="rounded-lg border border-hairline bg-bg px-3 py-2 text-xs text-faint">
              The update channel and image repository/tag are controlled exclusively by{' '}
              <code>PLEX_MANAGER_IMAGE</code>. These controls never switch channels or target a
              different container.
            </p>
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
              {numberField('log_max_rows', 'Log retention (max rows)', { min: 0 })}
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

      <SessionsSection />

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
