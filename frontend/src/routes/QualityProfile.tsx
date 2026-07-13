import { useQualityProfile } from '../api/hooks'
import type { QualityProfileItemResponse } from '../api/types'
import { AdminPageHeader } from '../components/ui/AdminPageHeader'
import { Button } from '../components/ui/Button'
import { adminRowPadding } from '../components/ui/adminStyles'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { cn } from '../lib/cn'

const DESCRIPTION =
  'This is the ordered quality profile with a hard categorical cutoff. Qualities are ranked low to high, and the cutoff is the point where the system stops chasing upgrades. Disallowed qualities (CAM, TS, TELECINE, and the like) are rejected outright — never down-scored into a grab. Read-only in v1.'

export function QualityProfile() {
  const query = useQualityProfile()

  if (query.isPending) {
    return (
      <div className="mx-auto flex w-full max-w-[900px] flex-col gap-6 px-5 py-8 sm:px-8">
        <AdminPageHeader title="Quality profile" />
        <CenteredSpinner label="Loading quality profile…" />
      </div>
    )
  }

  if (query.isError) {
    return (
      <div className="mx-auto flex w-full max-w-[900px] flex-col gap-6 px-5 py-8 sm:px-8">
        <AdminPageHeader title="Quality profile" />
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
      </div>
    )
  }

  const profile = query.data

  return (
    <div className="mx-auto w-full max-w-[900px] space-y-6 px-5 py-8 sm:px-8">
      <AdminPageHeader
        title="Quality profile"
        status={`Cutoff: ${profile.cutoff_name} · Upgrades: ${profile.upgrade_allowed ? 'allowed' : 'off'}`}
        description={`${profile.name === 'Quality profile' ? '' : `${profile.name}. `}${DESCRIPTION}`}
      />

      <div className="overflow-hidden rounded-[10px] border border-hairline bg-surface">
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
        adminRowPadding,
        'grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2',
        'sm:grid-cols-[auto_minmax(0,1fr)_auto_auto_4rem]',
        isCutoff
          ? 'border-l-2 border-l-gold bg-gold/5'
          : 'border-l-2 border-l-transparent',
      )}
    >
      <span
        aria-hidden
        className={cn(
          'col-start-1 row-start-1 size-2 shrink-0 rounded-full',
          item.allowed ? 'bg-available' : 'bg-error',
        )}
      />

      <span className="col-start-2 row-start-1 min-w-0 text-[13px] leading-snug font-semibold break-words text-ink">
        {item.name}
      </span>

      {isCutoff ? (
        <span className="col-start-3 row-start-1 shrink-0 rounded border border-gold/40 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-gold">
          CUTOFF
        </span>
      ) : null}

      <span className="col-start-2 row-start-2 min-w-0 font-mono text-[11px] leading-snug break-words text-faint sm:col-start-4 sm:row-start-1">
        {item.source} · {item.resolution}
      </span>

      <span
        className={cn(
          'col-start-3 row-start-2 w-16 shrink-0 text-right font-mono text-[11px]',
          'sm:col-start-5 sm:row-start-1',
          item.allowed ? 'text-available' : 'text-error',
        )}
      >
        {item.allowed ? 'allowed' : 'blocked'}
      </span>
    </li>
  )
}
