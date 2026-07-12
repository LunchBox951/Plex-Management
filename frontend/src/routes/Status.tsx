import { cn } from '../lib/cn'
import { useEvict, useOpsDisk, useOpsHealth, useSettings } from '../api/hooks'
import type { DiskRootItem, HealthResponse, SubsystemHealthItem } from '../api/types'
import { Button } from '../components/ui/Button'
import { Dot, type DotTone } from '../components/ui/Dot'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { LinkButton } from '../components/ui/LinkButton'
import { useToast } from '../components/ui/toast'
import type { ApiError } from '../lib/errors'
import { formatBytes, formatTimestamp } from '../lib/format'

// Mirror the backend's `DISK_PRESSURE_*_PERCENT_DEFAULT` (web/deps.py) — used
// only when settings haven't loaded yet, never in place of a real value.
const DISK_PRESSURE_THRESHOLD_PERCENT_DEFAULT = 90
const DISK_PRESSURE_TARGET_PERCENT_DEFAULT = 80

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
    <div className="rounded-xl border border-hairline bg-surface p-4">
      <div className="flex items-center justify-between gap-3">
        <span className="truncate font-display text-sm font-semibold text-ink capitalize">
          {subsystem.name}
        </span>
        <Dot tone={SUBSYSTEM_TONE[subsystem.status]} label={SUBSYSTEM_LABEL[subsystem.status]} />
      </div>
      {subsystem.detail ? <p className="mt-2 text-xs text-muted">{subsystem.detail}</p> : null}
      {/* Non-blocking, informational — distinct from `detail` (which only ever
          carries FAILURE diagnostics). E.g. qBittorrent's default save path not
          being visible inside this container (issues #133/#157); never flips
          `status`, so it renders even on an otherwise-healthy subsystem. */}
      {subsystem.note ? <p className="mt-2 text-xs text-searching">⚠ {subsystem.note}</p> : null}
      <p className="mt-2 font-mono text-[10px] text-faint">
        checked {formatTimestamp(subsystem.checked_at)}
      </p>
    </div>
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
    <div className="rounded-xl border border-hairline bg-surface p-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-display text-sm font-semibold text-ink">Reconcile loop</h3>
        <Dot tone={tone} label={label} />
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 font-mono text-xs text-muted">
        <dt>Last run</dt>
        <dd className="text-right">{formatTimestamp(reconcile.last_run_at)}</dd>
        <dt>Last success</dt>
        <dd className="text-right">{formatTimestamp(reconcile.last_ok_at)}</dd>
        <dt>Consecutive failures</dt>
        <dd
          className={cn(
            'text-right',
            reconcile.consecutive_failures > 0 ? 'font-semibold text-error' : '',
          )}
        >
          {reconcile.consecutive_failures}
        </dd>
        {reconcile.last_error_type ? (
          <>
            <dt>Last error</dt>
            <dd className="text-right text-error">
              {reconcile.last_error_type} · {formatTimestamp(reconcile.last_error_at)}
            </dd>
          </>
        ) : null}
      </dl>
    </div>
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
    <div className="rounded-xl border border-hairline bg-surface p-4">
      <div className="flex items-center justify-between gap-3">
        <h3 className="font-display text-sm font-semibold text-ink">Auto-grab loop</h3>
        <Dot tone={tone} label={label} />
      </div>
      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 font-mono text-xs text-muted">
        <dt>Last run</dt>
        <dd className="text-right">{formatTimestamp(autograb.last_run_at)}</dd>
        <dt>Last success</dt>
        <dd className="text-right">{formatTimestamp(autograb.last_ok_at)}</dd>
        <dt>Consecutive failures</dt>
        <dd
          className={cn(
            'text-right',
            autograb.consecutive_failures > 0 ? 'font-semibold text-error' : '',
          )}
        >
          {autograb.consecutive_failures}
        </dd>
        {/* Scopes whose grab keeps failing (GrabError) are cooled down so they don't
            starve the search budget — a non-zero count means the grab pipeline, not
            the search, is what's broken. */}
        <dt>Cooling scopes</dt>
        <dd
          className={cn(
            'text-right',
            autograb.cooled_down_scopes > 0 ? 'font-semibold text-searching' : '',
          )}
        >
          {autograb.cooled_down_scopes}
        </dd>
        {autograb.last_error_type ? (
          <>
            <dt>Last error</dt>
            <dd className="text-right text-error">
              {autograb.last_error_type} · {formatTimestamp(autograb.last_error_at)}
            </dd>
          </>
        ) : null}
      </dl>
    </div>
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
  const underPressure = root.used_percent >= thresholdPercent
  const barTone = underPressure
    ? 'bg-error'
    : root.used_percent >= targetPercent
      ? 'bg-searching'
      : 'bg-available'

  return (
    <div className="rounded-xl border border-hairline bg-surface p-4">
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-mono text-xs font-semibold text-faint">{root.root}</span>
        {root.error ? null : (
          <span className="font-mono text-xs text-muted tabular-nums">{pct}% used</span>
        )}
      </div>
      <p className="mt-0.5 truncate font-mono text-[11px] text-faint">{root.path}</p>

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
          <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-white/10">
            <div
              className={cn('h-full rounded-full transition-[width] duration-500', barTone)}
              style={{ width: `${Math.min(100, Math.max(0, pct))}%` }}
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
                className="flex items-center justify-between gap-2 font-mono text-[11px] text-faint"
              >
                <span className="truncate">
                  {c.title}
                  {c.season != null ? ` · S${c.season}` : ''}
                </span>
                <span className="shrink-0 tabular-nums">{c.size_percent.toFixed(1)}%</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
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

  return (
    <div className="mx-auto flex w-full max-w-[1160px] flex-col gap-8 px-5 py-8 sm:px-8">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-extrabold">Status</h1>
        <Button variant="secondary" onClick={() => void onFreeSpace()} loading={evict.isPending}>
          Free space now
        </Button>
      </header>

      <section>
        <h2 className="mb-3 font-mono text-xs font-semibold tracking-wide text-faint uppercase">
          Subsystems
        </h2>
        {health.isLoading ? (
          <CenteredSpinner label="Checking subsystems…" />
        ) : health.isError || !health.data ? (
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
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {health.data.subsystems.map((s) => (
              <SubsystemCard key={s.name} subsystem={s} />
            ))}
          </div>
        )}
      </section>

      {health.data ? (
        <section>
          <h2 className="mb-3 font-mono text-xs font-semibold tracking-wide text-faint uppercase">
            Background loops
          </h2>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <ReconcilePanel reconcile={health.data.reconcile} />
            <AutograbPanel autograb={health.data.autograb} />
          </div>
        </section>
      ) : null}

      <section>
        <h2 className="mb-3 font-mono text-xs font-semibold tracking-wide text-faint uppercase">
          Disk
        </h2>
        {disk.isLoading ? (
          <CenteredSpinner label="Reading disk usage…" />
        ) : disk.isError || !disk.data ? (
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
        ) : disk.data.roots.length === 0 ? (
          <StateMessage
            title="No library root configured"
            message="Set a Movies or TV library folder in Settings to see disk usage."
          />
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
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
      </section>
    </div>
  )
}
