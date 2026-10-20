import { useMemo, useState } from 'react'
import { useRequests } from '../../api/hooks'
import type { RequestResponse } from '../../api/types'
import { useRequestActions } from '../../components/manage/useRequestActions'
import { useRequestDialog } from '../../components/requests/useRequestDialog'
import { ActionMenu } from '../../components/ui/ActionMenu'
import { AdminPageHeader } from '../../components/ui/AdminPageHeader'
import { Button } from '../../components/ui/Button'
import { ProgressBar } from '../../components/ui/ProgressBar'
import { StatusBadge } from '../../components/ui/StatusBadge'
import { CenteredSpinner, StateMessage } from '../../components/ui/feedback'
import { CloseIcon, SearchIcon } from '../../components/ui/icons'
import { cn } from '../../lib/cn'
import { describeRequestScope } from '../../lib/requestScope'
import { isInFlightRequestStatus, needsAttention, requestStatus } from '../../lib/status'

type Filter = 'all' | 'progress' | 'attention' | 'done' | 'cancelled'

const DONE = new Set(['available', 'partially_available', 'failed', 'evicted'])

const FILTERS: { key: Filter; label: string; test: (request: RequestResponse) => boolean }[] = [
  { key: 'all', label: 'All', test: () => true },
  {
    key: 'progress',
    label: 'In progress',
    test: (request) =>
      (isInFlightRequestStatus(request.status) && !needsAttention(request.status)) ||
      request.status === 'pending' ||
      request.status === 'waiting_for_air_date',
  },
  { key: 'attention', label: 'Needs attention', test: (request) => needsAttention(request.status) },
  { key: 'done', label: 'Done', test: (request) => DONE.has(request.status) },
  { key: 'cancelled', label: 'Cancelled', test: (request) => request.status === 'cancelled' },
]

function Poster({ request }: { request: RequestResponse }) {
  const [failed, setFailed] = useState(false)
  return request.poster_url && !failed ? (
    <img
      src={request.poster_url}
      alt=""
      loading="lazy"
      onError={() => setFailed(true)}
      className="h-[45px] w-[30px] shrink-0 rounded-[4px] object-cover ring-1 ring-white/5"
    />
  ) : (
    <div className="h-[45px] w-[30px] shrink-0 rounded-[4px] bg-poster ring-1 ring-white/5" />
  )
}

/**
 * Manage › Requests: every request, filterable, with a ⋯ menu of the actions
 * legal for each row and a bulk bar for selections. Clicking a row opens its
 * detail; ticking its box only selects it (Interactions: Manage).
 */
export function ManageRequests() {
  const { data, isLoading, isError, error, refetch } = useRequests({ poll: true })
  const [filter, setFilter] = useState<Filter>('all')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set())
  const { openRequest, pickRelease, dialog } = useRequestDialog()
  const actions = useRequestActions({ openRequest, pickRelease })

  const all = useMemo(() => data?.requests ?? [], [data])
  const counts = useMemo(
    () => Object.fromEntries(FILTERS.map((f) => [f.key, all.filter(f.test).length])) as Record<Filter, number>,
    [all],
  )
  const needle = query.trim().toLowerCase()
  const rows = all
    .filter(FILTERS.find((f) => f.key === filter)!.test)
    .filter((request) => !needle || request.title.toLowerCase().includes(needle))
  const selectedRows = all.filter((request) => selected.has(request.id))
  const allVisibleSelected = rows.length > 0 && rows.every((request) => selected.has(request.id))

  const toggle = (id: number) => {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  const toggleAll = () =>
    setSelected(allVisibleSelected ? new Set() : new Set(rows.map((request) => request.id)))

  const searchable = selectedRows.filter(actions.canSearchAgain)
  const cancellable = selectedRows.filter(actions.canCancel)

  let body
  if (isLoading) {
    body = <CenteredSpinner label="Loading requests…" />
  } else if (isError && !data) {
    body = (
      <StateMessage
        tone="error"
        title="Couldn't load requests"
        message={error.message}
        action={
          <Button variant="secondary" onClick={() => void refetch()}>
            Retry
          </Button>
        }
      />
    )
  } else if (rows.length === 0) {
    body = (
      <StateMessage
        title={needle ? `No requests match "${query.trim()}"` : 'Nothing here'}
        message={needle ? 'Try a different title.' : 'Requests in this group will show up here.'}
      />
    )
  } else {
    body = (
      <div className="overflow-hidden rounded-[10px] bg-surface ring-1 ring-hairline">
        <div className="hidden grid-cols-[28px_30px_minmax(0,1fr)_120px_200px_36px] items-center gap-3 px-3.5 py-3 font-mono text-[10px] leading-none font-semibold tracking-[0.12em] text-faint uppercase sm:grid">
          <input
            type="checkbox"
            aria-label="Select all shown requests"
            checked={allVisibleSelected}
            onChange={toggleAll}
            className="size-[18px] accent-gold"
          />
          <span />
          <span>Title</span>
          <span>Asked by</span>
          <span>Status</span>
          <span />
        </div>
        <ul>
          {rows.map((request) => (
            <li
              key={request.id}
              className={cn(
                'relative grid grid-cols-[28px_30px_minmax(0,1fr)_36px] items-center gap-3 border-t border-hairline px-3.5 py-2.5 first:border-t-0 sm:grid-cols-[28px_30px_minmax(0,1fr)_120px_200px_36px] sm:first:border-t',
                selected.has(request.id) && 'bg-gold/[0.05]',
              )}
            >
              <input
                type="checkbox"
                aria-label={`Select ${request.title}`}
                checked={selected.has(request.id)}
                onChange={() => toggle(request.id)}
                className="relative z-20 size-[18px] accent-gold"
              />
              <Poster request={request} />
              <div className="min-w-0">
                <button
                  type="button"
                  onClick={() => openRequest(request)}
                  className="block max-w-full truncate text-left font-display text-sm font-bold text-ink outline-none after:absolute after:inset-0 after:z-10 hover:underline focus-visible:underline"
                >
                  {request.title}
                </button>
                <span className="block truncate font-mono text-[11px] text-faint">
                  {describeRequestScope(request)}
                </span>
                <span className="mt-1 flex items-center gap-2 sm:hidden">
                  <StatusBadge status={requestStatus(request.status)} />
                </span>
              </div>
              <span className="hidden text-[13px] text-muted sm:block">
                {request.is_owner ? 'You' : 'Someone else'}
              </span>
              <span className="hidden min-w-0 flex-col items-start gap-1.5 sm:flex">
                <StatusBadge
                  status={requestStatus(request.status)}
                  {...(request.status === 'downloading' && request.download_progress != null
                    ? { detail: `${Math.round(request.download_progress * 100)}%` }
                    : {})}
                />
                {request.status === 'downloading' && request.download_progress != null ? (
                  <ProgressBar
                    value={request.download_progress}
                    label={`${request.title} download progress`}
                    className="w-32"
                  />
                ) : null}
              </span>
              <span className="relative z-20">
                <ActionMenu
                  label={`Actions for ${request.title}`}
                  heading={request.title}
                  items={actions.itemsFor(request)}
                />
              </span>
            </li>
          ))}
        </ul>
      </div>
    )
  }

  return (
    <div className="mx-auto w-full max-w-[1180px] px-4 py-6 pb-28 sm:px-8">
      <AdminPageHeader title="Requests" count={data ? all.length : undefined} />
      <div className="mt-4 mb-3 flex flex-wrap items-center gap-2">
        <div role="tablist" aria-label="Filter requests" className="flex flex-wrap gap-1.5">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              role="tab"
              aria-selected={filter === f.key}
              onClick={() => setFilter(f.key)}
              className={cn(
                'h-8 rounded-full px-3 text-[12.5px] font-semibold outline-none focus-visible:ring-2 focus-visible:ring-white/40',
                filter === f.key
                  ? 'bg-ink text-bg'
                  : f.key === 'attention' && counts.attention > 0
                    ? 'text-error ring-1 ring-inset ring-error/30'
                    : 'text-faint ring-1 ring-inset ring-white/10 hover:text-ink',
              )}
            >
              {f.label}
              {f.key !== 'all' && counts[f.key] > 0 ? ` ${counts[f.key]}` : ''}
            </button>
          ))}
        </div>
        <label className="relative ml-auto w-full sm:w-60">
          <span className="sr-only">Search requests</span>
          <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" />
          <input
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search requests"
            className="h-9 w-full rounded-lg bg-surface-deep pr-3 pl-9 text-[13px] text-ink ring-1 ring-inset ring-white/10 outline-none placeholder:text-faint focus-visible:ring-2 focus-visible:ring-gold/50"
          />
        </label>
      </div>
      {body}

      {selectedRows.length > 0 ? (
        <div
          role="region"
          aria-label="Bulk actions"
          className="fixed inset-x-3 bottom-[calc(96px+env(safe-area-inset-bottom))] z-30 mx-auto flex max-w-max items-center gap-2 rounded-xl bg-ink py-2 pr-2 pl-4 text-[13px] font-semibold text-bg shadow-[0_12px_40px_rgb(0_0_0/60%)] sm:bottom-6"
        >
          <span className="mr-1">{selectedRows.length} selected</span>
          {searchable.length > 0 ? (
            <button
              type="button"
              onClick={() => searchable.forEach(actions.searchAgain)}
              className="h-8 rounded-lg bg-bg/8 px-3 hover:bg-bg/14"
            >
              Search again{searchable.length < selectedRows.length ? ` (${searchable.length})` : ''}
            </button>
          ) : null}
          {cancellable.length > 0 ? (
            <button
              type="button"
              onClick={() => actions.confirmCancel(cancellable)}
              className="h-8 rounded-lg px-3 text-[#8a3a33] hover:bg-bg/8"
            >
              Cancel requests{cancellable.length < selectedRows.length ? ` (${cancellable.length})` : ''}
            </button>
          ) : null}
          <button
            type="button"
            aria-label="Clear selection"
            onClick={() => setSelected(new Set())}
            className="flex size-8 items-center justify-center rounded-lg hover:bg-bg/8"
          >
            <CloseIcon className="size-4" />
          </button>
        </div>
      ) : null}
      {dialog}
      {actions.dialogs}
    </div>
  )
}
