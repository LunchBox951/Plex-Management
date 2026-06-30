import { useQualityProfile } from '../api/hooks'
import type { QualityProfileItemResponse } from '../api/types'
import { Button } from '../components/ui/Button'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { cn } from '../lib/cn'

export function QualityProfile() {
  const query = useQualityProfile()

  if (query.isPending) {
    return <CenteredSpinner label="Loading quality profile…" />
  }

  if (query.isError) {
    return (
      <StateMessage
        tone="error"
        title="Couldn't load the quality profile"
        message={query.error.message}
        action={
          <Button variant="secondary" onClick={() => void query.refetch()}>
            Retry
          </Button>
        }
      />
    )
  }

  const profile = query.data

  return (
    <div className="space-y-6">
      <header className="space-y-2">
        <h1 className="font-display text-2xl font-extrabold">{profile.name}</h1>
        <p className="font-mono text-xs text-faint">
          <span>Cutoff: {profile.cutoff_name}</span>
          <span className="px-2 text-faint/50">·</span>
          <span>Upgrades: {profile.upgrade_allowed ? 'allowed' : 'off'}</span>
        </p>
      </header>

      <p className="max-w-2xl text-sm text-muted">
        This is the ordered quality profile with a hard categorical cutoff. Qualities are ranked
        low to high, and the cutoff is the point where the system stops chasing upgrades. Disallowed
        qualities (CAM, TS, TELECINE, and the like) are rejected outright — never down-scored into a
        grab. Read-only in the alpha.
      </p>

      <div className="overflow-hidden rounded-xl border border-hairline bg-surface">
        <ul className="divide-y divide-hairline">
          {profile.items.map((item) => (
            <QualityRow
              key={item.quality_id}
              item={item}
              isCutoff={item.quality_id === profile.cutoff_quality_id}
            />
          ))}
        </ul>
      </div>
    </div>
  )
}

function QualityRow({
  item,
  isCutoff,
}: {
  item: QualityProfileItemResponse
  isCutoff: boolean
}) {
  return (
    <li
      className={cn(
        'flex items-center gap-3 px-4 py-3',
        'border-l-2 border-l-transparent',
        isCutoff && 'border-l-gold bg-gold/5',
      )}
    >
      <span
        aria-hidden
        className={cn(
          'size-2.5 shrink-0 rounded-full',
          item.allowed ? 'bg-available' : 'bg-error',
        )}
      />

      <span className="min-w-0 flex-1 truncate font-medium text-ink">{item.name}</span>

      {isCutoff ? (
        <span className="shrink-0 rounded border border-gold/40 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-gold">
          Cutoff
        </span>
      ) : null}

      <span className="shrink-0 font-mono text-xs text-faint">
        {item.source} · {item.resolution}
      </span>

      <span
        className={cn(
          'w-16 shrink-0 text-right font-mono text-xs',
          item.allowed ? 'text-available' : 'text-error',
        )}
      >
        {item.allowed ? 'allowed' : 'blocked'}
      </span>
    </li>
  )
}
