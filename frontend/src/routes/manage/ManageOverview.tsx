import { Link } from 'react-router'
import { useOpsHealth, useRequests } from '../../api/hooks'
import type { SubsystemHealthItem } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatBytes } from '../../lib/format'
import { isInFlightRequestStatus, needsAttention } from '../../lib/status'
import { AttentionList } from './NeedsAttention'

const DOT: Record<SubsystemHealthItem['status'], string> = {
  ok: 'bg-available',
  degraded: 'bg-searching',
  down: 'bg-error',
  not_configured: 'bg-faint',
}

function humanName(name: string): string {
  const known: Record<string, string> = {
    plex: 'Plex',
    qbittorrent: 'qBittorrent',
    prowlarr: 'Indexers',
    tmdb: 'TMDB',
    database: 'Database',
  }
  return known[name] ?? name.charAt(0).toUpperCase() + name.slice(1)
}

/**
 * Manage › Overview — where Manage opens on a phone: a health grid, three
 * headline numbers, and the stuck requests with their fixes.
 */
export function ManageOverview() {
  const health = useOpsHealth({ poll: true })
  const requests = useRequests({ poll: true })
  const all = requests.data?.requests ?? []
  const inProgress = all.filter(
    (request) => isInFlightRequestStatus(request.status) && !needsAttention(request.status),
  ).length
  const attention = all.filter((request) => needsAttention(request.status)).length
  // Distinct roots can share a filesystem; the smallest free figure is the honest
  // "how much room is left" headline rather than a double-counted sum.
  const disks = (health.data?.disks ?? []).filter((disk) => disk.error == null)
  const free = disks.length > 0 ? Math.min(...disks.map((disk) => disk.available_bytes)) : 0

  return (
    <div className="mx-auto flex w-full max-w-[860px] flex-col gap-4 px-4 py-6 sm:px-8">
      <h1 className="font-display text-[28px] leading-tight font-extrabold">Manage</h1>
      <Link
        to="/manage/health"
        aria-label="Open system health"
        className="grid grid-cols-2 gap-x-3 gap-y-2.5 rounded-[10px] bg-surface p-3.5 ring-1 ring-hairline outline-none hover:ring-white/15 focus-visible:ring-2 focus-visible:ring-gold/60"
      >
        {health.data ? (
          health.data.subsystems.map((item) => (
            <span key={item.name} className="flex items-center gap-2 text-[12.5px] font-medium text-muted">
              <span aria-hidden className={cn('size-2 rounded-full', DOT[item.status])} />
              {humanName(item.name)}
              <span className="sr-only">: {item.status.replace('_', ' ')}</span>
            </span>
          ))
        ) : (
          <span className="col-span-2 font-mono text-[11px] text-faint">
            {health.isError ? "Couldn't check services" : 'Checking services…'}
          </span>
        )}
      </Link>
      <div className="grid grid-cols-3 gap-2">
        {[
          { value: String(inProgress), label: 'in progress' },
          { value: String(attention), label: 'need you' },
          { value: free > 0 ? formatBytes(free) : '—', label: 'free space' },
        ].map((stat) => (
          <div key={stat.label} className="rounded-[10px] bg-surface px-3 py-2.5 ring-1 ring-hairline">
            <b className="block font-display text-[22px] leading-tight font-extrabold">{stat.value}</b>
            <span className="font-mono text-[11px] text-faint">{stat.label}</span>
          </div>
        ))}
      </div>
      <h2 className="pt-2 font-mono text-[10.5px] leading-none font-semibold tracking-[0.14em] text-faint uppercase">
        Needs attention
      </h2>
      <AttentionList limit={5} />
    </div>
  )
}
