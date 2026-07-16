import { useState } from 'react'
import { cn } from '../lib/cn'
import {
  useCheckForUpdate,
  useEvict,
  useForceResetCoordinator,
  useOpsDisk,
  useOpsHealth,
  useSettings,
  useUpdateStatus,
  useUpdateWhenReady,
} from '../api/hooks'
import type {
  DiskRootItem,
  HealthResponse,
  SubsystemHealthItem,
  UpdateStatusResponse,
} from '../api/types'
import { AdminEmptyState } from '../components/ui/AdminEmptyState'
import { AdminPageHeader } from '../components/ui/AdminPageHeader'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { Dot, type DotTone } from '../components/ui/Dot'
import { SectionHeader } from '../components/ui/SectionHeader'
import { adminRowPadding } from '../components/ui/adminStyles'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { LinkButton } from '../components/ui/LinkButton'
import { useToast } from '../components/ui/toast'
import type { ApiError } from '../lib/errors'
import { formatBytes, formatTimestamp } from '../lib/format'

// Mirror the backend's `DISK_PRESSURE_*_PERCENT_DEFAULT` (web/deps.py) — used
// only when settings haven't loaded yet, never in place of a real value.
const DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT = 90
const DISK_PRESSURE_TARGET_PERCENT_DEFAULT = 80

type UpdateState = UpdateStatusResponse['state']

const UPDATE_STATE: Record<UpdateState, { label: string; tone: DotTone }> = {
  disabled: { label: 'Automatic updates disabled', tone: 'neutral' },
  unavailable: { label: 'Updater unavailable', tone: 'error' },
  idle: { label: 'Ready', tone: 'ok' },
  checking: { label: 'Checking for an update', tone: 'warn' },
  update_available: { label: 'Update available', tone: 'warn' },
  waiting_for_window: { label: 'Waiting for update window', tone: 'warn' },
  waiting_for_idle: { label: 'Waiting for critical work', tone: 'warn' },
  draining: { label: 'Draining critical work', tone: 'warn' },
  installing: { label: 'Installing update', tone: 'warn' },
  rollback: { label: 'Rolling back', tone: 'warn' },
  succeeded: { label: 'Last update succeeded', tone: 'ok' },
  failed: { label: 'Update operation failed', tone: 'error' },
}

function readableCode(value: string): string {
  return value.replaceAll('_', ' ')
}

function UpdatePanel({
  status,
  checkPending,
  updatePending,
  recoverPending,
  onCheck,
  onUpdate,
  onRecover,
}: {
  status: UpdateStatusResponse
  checkPending: boolean
  updatePending: boolean
  recoverPending: boolean
  onCheck: () => void
  onUpdate: () => void
  onRecover: () => void
}) {
  const state = UPDATE_STATE[status.state]
  const operationActive = ['checking', 'draining', 'installing', 'rollback'].includes(status.state)
  // The coordinator landed in a state this build doesn't recognize (a
  // version-skew/rollback window): either the PHASE itself is unknown, or a
  // known phase carries a queued ACTION this build can't interpret — which
  // silently refuses every new check/install as "already in progress". Both
  // fail closed until an admin re-anchors them — the north-star #1 button,
  // surfaced only here (issue #354). Keyed off the honest backend blockers, so
  // the banner appears exactly when a guard is the thing blocking the controls.
  const phaseWedged = status.blocker === 'coordinator_state_unknown'
  const actionWedged = status.blocker === 'requested_action_unknown'
  const staleWedged = status.blocker === 'coordinator_state_stale'
  const waitingForRecovery = status.blocker === 'coordinator_recovery_not_ready'
  const liveDrain = status.blocker === 'coordinator_drain_active'
  const wedged = phaseWedged || actionWedged || staleWedged
  const recoveryBlocked = wedged || waitingForRecovery || liveDrain
  // waiting_for_window describes the AUTOMATIC policy. Keep the explicit
  // manual action available there because it intentionally bypasses that
  // window. waiting_for_idle means an install is already queued.
  const updateQueued = status.state === 'waiting_for_idle'
  const availableBuild =
    status.available_build ??
    (status.last_checked_at === null || status.last_checked_at === undefined
      ? 'Not checked yet'
      : 'No newer build reported')
  const nextWindow =
    status.next_window_start && status.next_window_end
      ? `${formatTimestamp(status.next_window_start)} – ${formatTimestamp(status.next_window_end)}`
      : 'No upcoming automatic window'

  return (
    <article
      aria-live="polite"
      className={cn(
        'min-w-0 rounded-[10px] border border-hairline bg-surface',
        adminRowPadding,
      )}
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="font-display text-sm font-semibold text-ink">Container updater</h3>
          <div className="mt-1">
            <Dot tone={state.tone} label={state.label} />
          </div>
        </div>
        <Dot
          tone={status.updater_available ? 'ok' : 'error'}
          label={status.updater_available ? 'Sidecar connected' : 'Sidecar not connected'}
        />
      </div>

      <dl className="mt-4 grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-x-4 gap-y-2 font-mono text-xs">
        <dt className="text-faint">Current build</dt>
        <dd className="min-w-0 text-right break-words text-ink tabular-nums [overflow-wrap:anywhere]">
          {status.current_build}
        </dd>
        <dt className="text-faint">Available build</dt>
        <dd className="min-w-0 text-right break-words text-ink tabular-nums [overflow-wrap:anywhere]">
          {availableBuild}
        </dd>
        <dt className="text-faint">Channel</dt>
        <dd className="min-w-0 text-right break-words text-ink tabular-nums [overflow-wrap:anywhere]">
          {status.channel}
        </dd>
        <dt className="text-faint">Last checked</dt>
        <dd className="min-w-0 text-right break-words text-ink tabular-nums [overflow-wrap:anywhere]">
          {formatTimestamp(status.last_checked_at)}
        </dd>
        <dt className="text-faint">Next window</dt>
        <dd className="min-w-0 text-right break-words text-ink tabular-nums [overflow-wrap:anywhere]">
          {nextWindow}
        </dd>
        <dt className="text-faint">Blocker</dt>
        <dd
          className={cn(
            'min-w-0 text-right break-words tabular-nums [overflow-wrap:anywhere]',
            status.blocker ? 'text-searching' : 'text-ink',
          )}
        >
          {status.blocker ? readableCode(status.blocker) : 'None reported'}
        </dd>
      </dl>

      {status.last_result ? (
        <div className="mt-4 rounded-lg border border-hairline bg-bg px-3 py-2 text-xs text-muted">
          <p className="font-semibold text-ink">Last completed operation</p>
          <p className="mt-1 font-mono [overflow-wrap:anywhere]">
            {readableCode(status.last_result.operation)} ·{' '}
            {readableCode(status.last_result.outcome)} ·{' '}
            {formatTimestamp(status.last_result.finished_at)}
          </p>
          {status.last_result.from_build || status.last_result.to_build ? (
            <p className="mt-1 font-mono [overflow-wrap:anywhere]">
              {status.last_result.from_build ?? 'unknown'} →{' '}
              {status.last_result.to_build ?? 'unknown'}
            </p>
          ) : null}
          {status.last_result.detail_code ? (
            <p className="mt-1 font-mono text-searching [overflow-wrap:anywhere]">
              {readableCode(status.last_result.detail_code)}
            </p>
          ) : null}
        </div>
      ) : (
        <p className="mt-4 text-xs text-faint">No completed updater operation has been reported.</p>
      )}

      <div className="mt-4 flex flex-wrap gap-2">
        <Button
          variant="secondary"
          size="sm"
          loading={checkPending}
          disabled={
            recoveryBlocked ||
            !status.updater_available ||
            operationActive ||
            updateQueued ||
            status.state === 'waiting_for_window' ||
            checkPending ||
            updatePending
          }
          onClick={onCheck}
        >
          Check now
        </Button>
        <Button
          size="sm"
          loading={updatePending}
          disabled={
            recoveryBlocked ||
            !status.updater_available ||
            operationActive ||
            updateQueued ||
            checkPending ||
            updatePending
          }
          onClick={onUpdate}
        >
          {updateQueued ? 'Update queued' : 'Update when ready'}
        </Button>
      </div>
      {wedged ? (
        <div className="mt-4 rounded-lg border border-error/40 bg-error/5 px-3 py-3 text-xs">
          <p className="font-semibold text-error">
            {phaseWedged
              ? 'Coordinator in an unrecognized state'
              : actionWedged
                ? 'Queued action not recognized by this version'
                : 'Coordinator state is stale'}
          </p>
          <p className="mt-1 text-muted">
            {phaseWedged
              ? 'Check and install are disabled — they can only fail until this is cleared. Recovering re-anchors the coordinator to idle so the controls work again; a queued update is preserved for retry.'
              : actionWedged
                ? 'A queued updater action isn’t recognized by this version, so every new check or install is refused. Recovering clears it and invalidates late work; nothing else is changed.'
                : 'The updater is stale. Recovery re-anchors it only after bounded evidence, preserves known actions, clears unknown actions while invalidating late work, and never removes a live lease.'}
          </p>
          <div className="mt-3">
            <Button
              variant="danger"
              size="sm"
              loading={recoverPending}
              disabled={recoverPending}
              onClick={onRecover}
            >
              Recover coordinator
            </Button>
          </div>
        </div>
      ) : null}
      {waitingForRecovery ? (
        <p className="mt-4 rounded-lg border border-searching/40 bg-searching/5 px-3 py-3 text-xs text-muted">
          Recovery is waiting for bounded age evidence that the operation was abandoned. It may still be in flight; try again after the evidence window has elapsed.
        </p>
      ) : null}
      {liveDrain ? (
        <p className="mt-4 rounded-lg border border-searching/40 bg-searching/5 px-3 py-3 text-xs text-muted">
          Recovery is refused while the maintenance lease is active. The lease is never removed by recovery; try again after it expires.
        </p>
      ) : null}
      {!status.updater_available && !recoveryBlocked ? (
        <p className="mt-3 text-xs text-faint">
          Enable the automatic-update Compose profile to connect the scoped updater sidecar.
        </p>
      ) : null}
    </article>
  )
}

const SUBSYSTEM_TONE: Record<SubsystemHealthItem['status'], DotTone> = {
  ok: 'ok',
  degraded: 'warn',
  down: 'error',
  not_configured: 'neutral',
}

const SUBSYSTEM_LABEL: Record<SubsystemHealthItem['status'], string> = {
  ok: 'Healthy',
  degraded: 'Degraded',
  down: 'Down',
  not_configured: 'Not configured',
}

/** One upstream's reachability card — the three-state {@link Dot} pattern
 * widened to the four honest subsystem states (never conflating "not
 * configured" with "down"). */
function SubsystemCard({ subsystem }: { subsystem: SubsystemHealthItem }) {
  return (
    <article
      className={cn(
        'min-w-0 rounded-[10px] border border-hairline bg-surface',
        adminRowPadding,
      )}
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <h3 className="min-w-0 break-words font-display text-sm font-semibold text-ink capitalize">
          {subsystem.name}
        </h3>
        <div className="shrink-0">
          <Dot
            tone={SUBSYSTEM_TONE[subsystem.status]}
            label={SUBSYSTEM_LABEL[subsystem.status]}
          />
        </div>
      </div>
      {subsystem.detail ? (
        <p className="mt-2 min-w-0 text-xs break-words text-muted [overflow-wrap:anywhere]">
          {subsystem.detail}
        </p>
      ) : null}
      {/* Non-blocking, informational — distinct from `detail` (which only ever
          carries FAILURE diagnostics). E.g. qBittorrent's default save path not
          being visible inside this container (issues #133/#157); never flips
          `status`, so it renders even on an otherwise-healthy subsystem. */}
      {subsystem.note ? (
        <p className="mt-2 min-w-0 text-xs break-words text-searching [overflow-wrap:anywhere]">
          ⚠ {subsystem.note}
        </p>
      ) : null}
      <p className="mt-2 min-w-0 font-mono text-[10px] break-words text-faint">
        checked {formatTimestamp(subsystem.checked_at)}
      </p>
    </article>
  )
}

/** The background reconcile loop's own health — deliberately separate from the
 * subsystem cards above (a cycle can complete OK even while one upstream
 * inside it degraded). */
function ReconcilePanel({ reconcile }: { reconcile: HealthResponse['reconcile'] }) {
  // A fresh boot (no cycle has completed yet) is neither "running clean" nor
  // "failing" — it just hasn't run. Don't claim health for a loop that hasn't
  // had its first tick.
  const hasRun = reconcile.last_run_at !== null
  const healthy = hasRun && reconcile.consecutive_failures === 0
  const tone: DotTone = !hasRun ? 'neutral' : healthy ? 'ok' : 'error'
  const label = !hasRun ? 'starting up' : healthy ? 'running clean' : 'failing'
  return (
    <article
      className={cn(
        'min-w-0 rounded-[10px] border border-hairline bg-surface',
        adminRowPadding,
      )}
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <h3 className="min-w-0 font-display text-sm font-semibold text-ink">Reconcile loop</h3>
        <div className="shrink-0">
          <Dot tone={tone} label={label} />
        </div>
      </div>
      <dl className="mt-3 grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-x-4 gap-y-1.5 font-mono text-xs">
        <dt className="min-w-0 text-faint">Last run</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {formatTimestamp(reconcile.last_run_at)}
        </dd>
        <dt className="min-w-0 text-faint">Last success</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {formatTimestamp(reconcile.last_ok_at)}
        </dd>
        <dt className="min-w-0 text-faint">Consecutive failures</dt>
        <dd
          className={cn(
            'min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]',
            reconcile.consecutive_failures > 0 ? 'font-semibold text-error' : '',
          )}
        >
          {reconcile.consecutive_failures}
        </dd>
        {reconcile.last_error_type ? (
          <>
            <dt className="min-w-0 text-faint">Last error</dt>
            <dd className="min-w-0 text-right text-error tabular-nums [overflow-wrap:anywhere]">
              {reconcile.last_error_type} · {formatTimestamp(reconcile.last_error_at)}
            </dd>
          </>
        ) : null}
      </dl>
    </article>
  )
}

/** The background auto-grab loop's own health (ADR-0013) — a Prowlarr outage
 * surfaces here as a failing loop, so the operator sees WHY nothing is being
 * grabbed rather than requests silently stuck at "pending". */
function AutograbPanel({ autograb }: { autograb: HealthResponse['autograb'] }) {
  const hasRun = autograb.last_run_at !== null
  const healthy = hasRun && autograb.consecutive_failures === 0
  const tone: DotTone = !hasRun ? 'neutral' : healthy ? 'ok' : 'error'
  const label = !hasRun ? 'starting up' : healthy ? 'running clean' : 'failing'
  return (
    <article
      className={cn(
        'min-w-0 rounded-[10px] border border-hairline bg-surface',
        adminRowPadding,
      )}
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <h3 className="min-w-0 font-display text-sm font-semibold text-ink">Auto-grab loop</h3>
        <div className="shrink-0">
          <Dot tone={tone} label={label} />
        </div>
      </div>
      <dl className="mt-3 grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-x-4 gap-y-1.5 font-mono text-xs">
        <dt className="min-w-0 text-faint">Last run</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {formatTimestamp(autograb.last_run_at)}
        </dd>
        <dt className="min-w-0 text-faint">Last success</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {formatTimestamp(autograb.last_ok_at)}
        </dd>
        <dt className="min-w-0 text-faint">Consecutive failures</dt>
        <dd
          className={cn(
            'min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]',
            autograb.consecutive_failures > 0 ? 'font-semibold text-error' : '',
          )}
        >
          {autograb.consecutive_failures}
        </dd>
        {/* Scopes whose grab keeps failing (GrabError) are cooled down so they don't
            starve the search budget — a non-zero count means the grab pipeline, not
            the search, is what's broken. */}
        <dt className="min-w-0 text-faint">Cooling scopes</dt>
        <dd
          className={cn(
            'min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]',
            autograb.cooled_down_scopes > 0 ? 'font-semibold text-searching' : '',
          )}
        >
          {autograb.cooled_down_scopes}
        </dd>
        {autograb.last_error_type ? (
          <>
            <dt className="min-w-0 text-faint">Last error</dt>
            <dd className="min-w-0 text-right text-error tabular-nums [overflow-wrap:anywhere]">
              {autograb.last_error_type} · {formatTimestamp(autograb.last_error_at)}
            </dd>
          </>
        ) : null}
      </dl>
    </article>
  )
}

function WatchlistPanel({ watchlist }: { watchlist: HealthResponse['watchlist'] }) {
  const tone: DotTone =
    watchlist.state === 'ok'
      ? 'ok'
      : watchlist.state === 'degraded' || watchlist.state === 'probe_failed'
        ? 'warn'
        : watchlist.state === 'error'
          ? 'error'
          : 'neutral'
  const label =
    watchlist.state === 'starting'
      ? 'starting up'
      : watchlist.state === 'ok'
        ? 'running clean'
        : watchlist.state.replace('_', ' ')
  return (
    <article
      className={cn(
        'min-w-0 rounded-[10px] border border-hairline bg-surface',
        adminRowPadding,
      )}
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <h3 className="min-w-0 font-display text-sm font-semibold text-ink">Watchlist sync</h3>
        <div className="shrink-0">
          <Dot tone={tone} label={label} />
        </div>
      </div>
      <dl className="mt-3 grid min-w-0 grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-x-4 gap-y-1.5 font-mono text-xs">
        <dt className="min-w-0 text-faint">Last run</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {formatTimestamp(watchlist.last_run_at)}
        </dd>
        <dt className="min-w-0 text-faint">Last success</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {formatTimestamp(watchlist.last_ok_at)}
        </dd>
        <dt className="min-w-0 text-faint">Fetched</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {watchlist.fetched}
        </dd>
        <dt className="min-w-0 text-faint">New requests</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {watchlist.created}
        </dd>
        <dt className="min-w-0 text-faint">Existing requests</dt>
        <dd className="min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]">
          {watchlist.existing}
        </dd>
        <dt className="min-w-0 text-faint">Failed users</dt>
        <dd
          className={cn(
            'min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]',
            watchlist.failed_users > 0 ? 'font-semibold text-searching' : '',
          )}
        >
          {watchlist.failed_users}
        </dd>
        <dt className="min-w-0 text-faint">Failed entries</dt>
        <dd
          className={cn(
            'min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]',
            watchlist.failed_entries > 0 ? 'font-semibold text-searching' : '',
          )}
        >
          {watchlist.failed_entries}
        </dd>
        {/* Skipped users explain a skip-driven degraded tick (stale token after a
            repoint, or plex.tv unreachable) that would otherwise show degraded
            with zero failures and no visible cause. */}
        <dt className="min-w-0 text-faint">Skipped users</dt>
        <dd
          className={cn(
            'min-w-0 text-right text-ink tabular-nums [overflow-wrap:anywhere]',
            watchlist.skipped_users > 0 ? 'font-semibold text-searching' : '',
          )}
        >
          {watchlist.skipped_users}
        </dd>
        {watchlist.last_error_type ? (
          <>
            <dt className="min-w-0 text-faint">Last error</dt>
            <dd className="min-w-0 text-right text-error tabular-nums [overflow-wrap:anywhere]">
              {watchlist.last_error_type} · {formatTimestamp(watchlist.last_error_at)}
            </dd>
          </>
        ) : null}
      </dl>
    </article>
  )
}

/** One configured library root: a usage bar, plus a ranked preview of what a
 * pressure sweep WOULD evict from it (never evicts anything itself — the
 * preview lists every eligible title regardless of current pressure, so the
 * bar-color tiers and the note below both key off the SAME two web-editable
 * settings the sweep itself uses, rather than a second, disconnected set of
 * hardcoded percentages). */
function DiskRootCard({
  root,
  thresholdPercent,
  targetPercent,
}: {
  root: DiskRootItem
  thresholdPercent: number
  targetPercent: number
}) {
  const pct = Math.round(root.used_percent)
  const gaugePct = Math.min(100, Math.max(0, pct))
  const underPressure = root.used_percent >= thresholdPercent
  const barTone = underPressure
    ? 'bg-error'
    : root.used_percent >= targetPercent
      ? 'bg-searching'
      : 'bg-available'

  return (
    <article
      className={cn(
        'min-w-0 rounded-[10px] border border-hairline bg-surface',
        adminRowPadding,
      )}
    >
      <div className="flex min-w-0 items-baseline justify-between gap-3">
        <h3 className="min-w-0 break-words font-mono text-xs font-semibold text-faint">
          {root.root}
        </h3>
        {root.error ? null : (
          <span className="shrink-0 font-mono text-xs text-muted tabular-nums">
            {pct}% used
          </span>
        )}
      </div>
      <p className="mt-0.5 min-w-0 font-mono text-[11px] break-all text-faint">{root.path}</p>

      {root.error ? (
        <div className="mt-3 flex flex-col items-start gap-2">
          <p className="text-xs text-error">This library folder isn't visible to Plex Manager.</p>
          <p className="font-mono text-[11px] break-all text-faint">{root.error}</p>
          <LinkButton variant="secondary" size="sm" to="/settings">
            Fix in Settings
          </LinkButton>
        </div>
      ) : (
        <>
          <div
            role="progressbar"
            aria-label={`${root.root} disk usage`}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={gaugePct}
            className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-white/10"
          >
            <div
              className={cn(
                'h-full rounded-full motion-safe:transition-[width] motion-safe:duration-500',
                barTone,
              )}
              style={{ width: `${gaugePct}%` }}
            />
          </div>
          <p className="mt-1 font-mono text-[11px] text-faint tabular-nums">
            {formatBytes(root.available_bytes)} free of {formatBytes(root.total_bytes)}
          </p>
        </>
      )}

      {root.candidates.length > 0 ? (
        <div className="mt-3 border-t border-hairline pt-3">
          <p className="text-xs font-semibold text-muted">
            Eviction candidates ({root.candidates.length})
          </p>
          <p className="mt-0.5 text-[11px] text-faint">
            {underPressure
              ? `This root is over the ${thresholdPercent}% pressure threshold — a sweep would evict from this list.`
              : `Eligible, but only evicted once this root reaches ${thresholdPercent}% used (currently ${pct}%).`}
          </p>
          <ul className="mt-1.5 flex flex-col gap-1">
            {root.candidates.slice(0, 5).map((c) => (
              <li
                key={`${c.request_id}-${c.season ?? 'movie'}`}
                className="flex min-w-0 items-center justify-between gap-2 font-mono text-[11px] text-faint"
              >
                <span className="min-w-0 truncate">
                  {c.title}
                  {c.season != null ? ` · S${c.season}` : ''}
                </span>
                <span className="shrink-0 tabular-nums">{c.size_percent.toFixed(1)}%</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </article>
  )
}

/** A failed *background* refresh keeps the last good snapshot on screen
 * (stale beats blank) — but never silently (north star #3): name the failing
 * probe, show the error, and offer an immediate retry, mirroring Queue's
 * "reconnecting…" idiom. Rendered only when cached data exists; a failure
 * with no cache still gets the section's full error state instead. */
function RefreshFailedNotice({
  what,
  message,
  onRetry,
}: {
  what: string
  message?: string
  onRetry: () => void
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px] text-error"
    >
      <span className="size-1.5 shrink-0 rounded-full bg-error" aria-hidden />
      <span className="min-w-0 [overflow-wrap:anywhere]">
        Couldn&apos;t refresh {what}
        {message ? ` (${message})` : ''} — showing the last known snapshot.
      </span>
      <Button variant="secondary" size="sm" onClick={onRetry}>
        Retry
      </Button>
    </div>
  )
}

/**
 * One operator view answering "is every subsystem healthy, is the reconcile
 * loop running, how full is the disk" — without `docker logs` (ADR-0012,
 * north star #2: the terminal is never required). "Free space now" is the
 * north-star #1 button: an operator-triggered pressure sweep on demand, even
 * with the automatic background sweep disabled.
 */
export function Status() {
  const health = useOpsHealth()
  const disk = useOpsDisk()
  const evict = useEvict()
  const settings = useSettings()
  const updates = useUpdateStatus()
  const checkForUpdate = useCheckForUpdate()
  const updateWhenReady = useUpdateWhenReady()
  const forceReset = useForceResetCoordinator()
  const [confirmRecover, setConfirmRecover] = useState(false)
  const { toast } = useToast()

  // Same two settings the pressure sweep itself reads (web/deps.py) — falls
  // back to the backend's own defaults only until settings have loaded, never
  // as a substitute for the real, possibly-edited value.
  const thresholdPercent =
    settings.data?.disk_pressure_threshold_percent ?? DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT
  const targetPercent =
    settings.data?.disk_pressure_target_percent ?? DISK_PRESSURE_TARGET_PERCENT_DEFAULT

  const onFreeSpace = async () => {
    try {
      const result = await evict.mutateAsync()
      toast({
        title:
          result.evicted.length > 0
            ? `Freed ${result.evicted.length} title${result.evicted.length === 1 ? '' : 's'}`
            : 'Nothing to free',
        description:
          result.evicted.length > 0
            ? result.evicted.map((o) => o.title).join(', ')
            : 'No root is under pressure, or nothing eligible was found.',
        intent: 'success',
      })
      // `errors` is optional in the generated type (it has a server-side
      // default of `[]`) but always present on the wire -- guard anyway so a
      // contract regen never turns this into a runtime crash.
      const sweepErrors = result.errors ?? []
      if (sweepErrors.length > 0) {
        // A root's sweep can fail AFTER an earlier root already deleted files
        // and committed — the success toast above already reflects whatever
        // freed, so this is a SEPARATE, additional warning naming exactly
        // which root(s) failed, never a silent partial outcome (north star
        // #2). Queries were already invalidated on success above, so the
        // disk/requests views reflect whatever the sweep DID accomplish.
        toast({
          title: `Sweep failed for ${sweepErrors.map((e) => e.root).join(', ')}`,
          description: sweepErrors.map((e) => e.detail).join('; '),
          intent: 'warning',
        })
      }
    } catch (error) {
      toast({
        title: 'Free space failed',
        description: (error as ApiError).message,
        intent: 'error',
      })
    }
  }

  const onCheckForUpdate = async () => {
    try {
      await checkForUpdate.mutateAsync()
      toast({
        title: 'Update check requested',
        description: 'The sidecar will report the result here when the check finishes.',
        intent: 'success',
      })
    } catch (error) {
      toast({
        title: 'Update check failed',
        description: (error as ApiError).message,
        intent: 'error',
      })
    }
  }

  const onUpdateWhenReady = async () => {
    try {
      await updateWhenReady.mutateAsync()
      toast({
        title: 'Update requested',
        description:
          'The sidecar will install when critical work is idle; this manual action does not wait for the automatic schedule window.',
        intent: 'success',
      })
    } catch (error) {
      toast({
        title: 'Update request failed',
        description: (error as ApiError).message,
        intent: 'error',
      })
    }
  }

  const onRecoverCoordinator = async () => {
    try {
      await forceReset.mutateAsync()
      setConfirmRecover(false)
      toast({
        title: 'Coordinator recovered',
        description: 'The updater is back to idle; checks and installs are available again.',
        intent: 'success',
      })
    } catch (error) {
      // Leave the dialog open on failure so the operator sees why (e.g. a 409
      // if the phase healed on its own between opening and confirming).
      toast({
        title: 'Recovery failed',
        description: (error as ApiError).message,
        intent: 'error',
      })
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-[1160px] flex-col gap-8 px-5 py-8 sm:px-8 lg:px-11">
      <AdminPageHeader
        title="Status"
        actions={
          <Button
            variant="secondary"
            size="sm"
            onClick={() => void onFreeSpace()}
            loading={evict.isPending}
          >
            Free space now
          </Button>
        }
      />

      <section className="flex flex-col gap-[10px]">
        <SectionHeader>Updates</SectionHeader>
        {updates.data ? (
          <>
            {updates.isError ? (
              <RefreshFailedNotice
                what="update status"
                {...(updates.error ? { message: updates.error.message } : {})}
                onRetry={() => void updates.refetch()}
              />
            ) : null}
            <UpdatePanel
              status={updates.data}
              checkPending={checkForUpdate.isPending}
              updatePending={updateWhenReady.isPending}
              recoverPending={forceReset.isPending}
              onCheck={() => void onCheckForUpdate()}
              onUpdate={() => void onUpdateWhenReady()}
              onRecover={() => setConfirmRecover(true)}
            />
          </>
        ) : updates.isLoading ? (
          <CenteredSpinner label="Checking updater…" />
        ) : (
          <StateMessage
            tone="error"
            title="Couldn't load update status"
            {...(updates.error ? { message: updates.error.message } : {})}
            action={
              <Button variant="secondary" onClick={() => void updates.refetch()}>
                Retry
              </Button>
            }
          />
        )}
      </section>

      <Dialog
        open={confirmRecover}
        onOpenChange={(next) => {
          if (!next) setConfirmRecover(false)
        }}
        title="Recover the update coordinator?"
        description="Clears whatever this version can't interpret: an unrecognized coordinator phase is re-anchored to idle, an unrecognized queued action is cleared. A recognizable queued update is preserved for retry; this is refused if there is nothing to recover or an update maintenance lease is still active."
      >
        <div className="flex flex-col gap-4">
          <p className="text-sm text-muted">
            This clears a stuck coordinator state (typically left by a version rollback): an
            unrecognized phase is re-anchored to idle, and an unrecognized queued action is cleared
            so new checks and installs are accepted again. It does nothing if an update is genuinely
            in flight — the server refuses while an operation is running or an update maintenance
            lease is still active (a possibly-live install). If that happens, wait a few minutes for
            the lease to expire and try again.
          </p>
          <div className="flex justify-end gap-3">
            <Button
              variant="secondary"
              onClick={() => setConfirmRecover(false)}
              disabled={forceReset.isPending}
            >
              Cancel
            </Button>
            <Button
              variant="danger"
              loading={forceReset.isPending}
              onClick={() => void onRecoverCoordinator()}
            >
              Recover coordinator
            </Button>
          </div>
        </div>
      </Dialog>

      <section className="flex flex-col gap-[10px]">
        <SectionHeader>Subsystems</SectionHeader>
        {health.data ? (
          <>
            {/* A failed background poll must not silently freeze the health
                picture — the cards below cover Background loops too (same
                query), so this one notice speaks for both sections. */}
            {health.isError ? (
              <RefreshFailedNotice
                what="health"
                {...(health.error ? { message: health.error.message } : {})}
                onRetry={() => void health.refetch()}
              />
            ) : null}
            <div className="grid grid-cols-1 gap-[10px] sm:grid-cols-2 lg:grid-cols-3">
              {health.data.subsystems.map((s) => (
                <SubsystemCard key={s.name} subsystem={s} />
              ))}
            </div>
          </>
        ) : health.isLoading ? (
          <CenteredSpinner label="Checking subsystems…" />
        ) : (
          <StateMessage
            tone="error"
            title="Couldn't load health"
            {...(health.error ? { message: health.error.message } : {})}
            action={
              <Button variant="secondary" onClick={() => void health.refetch()}>
                Retry
              </Button>
            }
          />
        )}
      </section>

      {health.data ? (
        <section className="flex flex-col gap-[10px]">
          <SectionHeader>Background loops</SectionHeader>
          <div className="grid grid-cols-1 gap-[10px] sm:grid-cols-2 lg:grid-cols-3">
            <ReconcilePanel reconcile={health.data.reconcile} />
            <AutograbPanel autograb={health.data.autograb} />
            <WatchlistPanel watchlist={health.data.watchlist} />
          </div>
        </section>
      ) : null}

      <section className="flex flex-col gap-[10px]">
        <SectionHeader>Disk</SectionHeader>
        {disk.data ? (
          <>
            {/* Same honesty rule as health: a stale gauge (and stale eviction
                candidates) must say it's stale, or a filling disk looks fine
                right up until it isn't. */}
            {disk.isError ? (
              <RefreshFailedNotice
                what="disk usage"
                {...(disk.error ? { message: disk.error.message } : {})}
                onRetry={() => void disk.refetch()}
              />
            ) : null}
            {disk.data.roots.length === 0 ? (
              <AdminEmptyState
                title="No library root configured"
                message="Set a Movies or TV library folder in Settings to see disk usage."
              />
            ) : (
              <div className="grid grid-cols-1 gap-[10px] sm:grid-cols-2">
                {disk.data.roots.map((r) => (
                  <DiskRootCard
                    key={r.root}
                    root={r}
                    thresholdPercent={thresholdPercent}
                    targetPercent={targetPercent}
                  />
                ))}
              </div>
            )}
          </>
        ) : disk.isLoading ? (
          <CenteredSpinner label="Reading disk usage…" />
        ) : (
          <StateMessage
            tone="error"
            title="Couldn't load disk usage"
            {...(disk.error ? { message: disk.error.message } : {})}
            action={
              <Button variant="secondary" onClick={() => void disk.refetch()}>
                Retry
              </Button>
            }
          />
        )}
      </section>
    </div>
  )
}
