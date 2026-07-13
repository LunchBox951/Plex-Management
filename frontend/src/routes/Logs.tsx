import { useMemo, useState, type ReactNode } from 'react'
import { useExportLogs, useLogs, useLogsTail, type LogsFilter } from '../api/hooks'
import type { LiveLogRecordItem, LogEventItem } from '../api/types'
import { AdminEmptyState } from '../components/ui/AdminEmptyState'
import { AdminPageHeader } from '../components/ui/AdminPageHeader'
import { Button } from '../components/ui/Button'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'
import { cn } from '../lib/cn'
import { downloadTextFile } from '../lib/download'
import type { ApiError } from '../lib/errors'

// Mirrors the backend's `_DEFAULT_LOG_PAGE_SIZE` (its cap is 500).
const PAGE_SIZE = 100
const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']

const LEVEL_CLASS: Record<string, string> = {
  DEBUG: 'text-faint',
  INFO: 'text-searching',
  WARNING: 'text-downloading',
  ERROR: 'text-error',
  CRITICAL: 'text-error',
}

function levelClass(level: string): string {
  return LEVEL_CLASS[level.toUpperCase()] ?? 'text-muted'
}

/** A durable `log_events` row and a live ring-buffer entry render identically;
 * only the durable row carries a stable `id` (used for the list key below). */
type LogLike = LogEventItem | LiveLogRecordItem

/** Client-side filter over whatever page is currently loaded — message or
 * logger substring match, case-insensitive. Not a backend search: the durable
 * store's own filters (level/logger/correlation_id) are all exact matches, so
 * this is what actually gives the box "search" behaviour. */
function matchesSearch(event: LogLike, term: string): boolean {
  if (!term) return true
  const lower = term.toLowerCase()
  return event.message.toLowerCase().includes(lower) || event.logger.toLowerCase().includes(lower)
}

function LogRow({ event }: { event: LogLike }) {
  return (
    <li className="grid min-w-0 grid-cols-[minmax(0,1fr)_3.25rem] items-start gap-x-3 gap-y-1 border-b border-hairline px-[14px] py-[7px] last:border-b-0 sm:grid-cols-[max-content_3.25rem_minmax(8rem,17.5rem)_minmax(0,1fr)] sm:gap-y-0">
      <time
        dateTime={event.created_at}
        className="col-start-1 row-start-1 min-w-0 font-mono text-[11px] leading-5 break-words text-faint [overflow-wrap:anywhere] sm:whitespace-nowrap"
      >
        {new Date(event.created_at).toLocaleString()}
      </time>
      <span
        className={cn(
          'col-start-2 row-start-1 whitespace-nowrap font-mono text-[11px] leading-5 font-semibold',
          levelClass(event.level),
        )}
      >
        {event.level}
      </span>
      <span
        title={event.logger}
        className="col-span-2 col-start-1 row-start-2 min-w-0 truncate font-mono text-[11px] leading-5 text-faint sm:col-span-1 sm:col-start-3 sm:row-start-1"
      >
        {event.logger}
      </span>
      <div className="col-span-2 col-start-1 row-start-3 min-w-0 sm:col-span-1 sm:col-start-4 sm:row-start-1">
        <p className="min-w-0 text-sm leading-5 break-words text-ink [overflow-wrap:anywhere]">
          {event.message}
        </p>
        {event.context ? (
          <p className="mt-0.5 min-w-0 font-mono text-[10px] leading-4 break-all text-faint">
            {JSON.stringify(event.context)}
          </p>
        ) : null}
      </div>
    </li>
  )
}

function renderLogList(events: LogLike[]): ReactNode {
  const liveKeyOccurrences = new Map<string, number>()

  return (
    <ul className="overflow-hidden rounded-[10px] border border-hairline bg-surface-deep">
      {events.map((event) => {
        if ('id' in event) return <LogRow key={`row-${event.id}`} event={event} />

        // Ring-buffer DTOs deliberately have no durable id. Build identity from
        // their complete exposed payload so prepending a new tail record does not
        // make React reinterpret every existing row as a different log event.
        const fingerprint = `${event.created_at}\u0000${event.level}\u0000${event.logger}\u0000${event.message}\u0000${JSON.stringify(event.context ?? null)}`
        const occurrence = liveKeyOccurrences.get(fingerprint) ?? 0
        liveKeyOccurrences.set(fingerprint, occurrence + 1)
        return <LogRow key={`tail-${fingerprint}-${occurrence}`} event={event} />
      })}
    </ul>
  )
}

/**
 * The log/console viewer (ADR-0012): a filtered, paginated read of the
 * durable `log_events` store; an all-levels live-tail toggle over the
 * in-memory ring buffer; and the "Copy/Download for diagnosis" export — the
 * affordance for pointing an LLM at a complete, correlated trail and asking
 * "why did this fail," closing the terminal-free north star for logs.
 */
export function Logs() {
  const [level, setLevel] = useState('')
  const [search, setSearch] = useState('')
  const [liveTail, setLiveTail] = useState(false)
  const [page, setPage] = useState(0)
  const { toast } = useToast()
  const exportLogs = useExportLogs()

  const trimmedSearch = search.trim()
  // A purely-numeric search term is sent server-side as `correlation_id` (an
  // EXACT match against a request/download/tmdb id) — the one precise, indexed
  // lookup the backend supports. Anything else stays a client-side filter.
  const correlationId = /^\d+$/.test(trimmedSearch) ? trimmedSearch : undefined

  const filter: LogsFilter = {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    ...(level ? { level } : {}),
    ...(correlationId !== undefined ? { correlationId } : {}),
  }
  const logsQuery = useLogs(filter, { enabled: !liveTail })
  const tailQuery = useLogsTail({ enabled: liveTail, limit: 200 })

  const durableEvents = useMemo(() => {
    const events = logsQuery.data?.events ?? []
    // Already narrowed server-side by the exact id match — re-filtering by the
    // same numeric text client-side would just hide rows whose message text
    // doesn't happen to contain that literal digit string.
    if (correlationId !== undefined) return events
    return events.filter((e) => matchesSearch(e, trimmedSearch))
  }, [logsQuery.data, correlationId, trimmedSearch])

  const tailEvents = useMemo(() => {
    const events = tailQuery.data?.events ?? []
    return events.filter((e) => matchesSearch(e, trimmedSearch))
  }, [tailQuery.data, trimmedSearch])

  const total = logsQuery.data?.total ?? 0
  const loadedCount = logsQuery.data?.events.length ?? 0
  const hasMore = page * PAGE_SIZE + loadedCount < total

  function onLevelChange(next: string): void {
    setLevel(next)
    setPage(0)
  }

  function onSearchChange(next: string): void {
    setSearch(next)
    setPage(0)
  }

  function downloadExport(text: string): void {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    downloadTextFile(text, `plex-manager-logs-${stamp}.txt`)
  }

  async function onExport(mode: 'copy' | 'download'): Promise<void> {
    // The network export is its own step: if it fails, "Export failed" is
    // accurate and nothing below runs. Anything that goes wrong AFTER this
    // point (clipboard-only) must never be reported as an export failure —
    // the data was fetched fine, only copying it locally didn't work.
    let text: string
    try {
      const exportArgs = correlationId !== undefined ? { correlationId } : {}
      text = await exportLogs.mutateAsync(exportArgs)
    } catch (error) {
      toast({ title: 'Export failed', description: (error as ApiError).message, intent: 'error' })
      return
    }

    if (mode === 'download') {
      downloadExport(text)
      toast({ title: 'Downloaded log export', intent: 'success' })
      return
    }

    // `navigator.clipboard` is only defined in a secure context (HTTPS or
    // localhost) — undefined on plain `http://` LAN deployments, a common
    // self-hosted topology. Fall back to a download rather than losing the
    // affordance entirely, and say so honestly (the export itself worked).
    if (!navigator.clipboard?.writeText) {
      downloadExport(text)
      toast({
        title: 'Clipboard unavailable — downloaded instead',
        description: 'Copying requires a secure context (HTTPS); saved the export as a file.',
        intent: 'info',
      })
      return
    }

    try {
      await navigator.clipboard.writeText(text)
      toast({ title: 'Copied logs to clipboard', intent: 'success' })
    } catch {
      // The export succeeded; only the clipboard write was refused (e.g. a
      // permissions policy) — fall back rather than reporting a false failure.
      downloadExport(text)
      toast({
        title: 'Copy failed — downloaded instead',
        description: 'The export succeeded; only the clipboard write was blocked.',
        intent: 'info',
      })
    }
  }

  let body: ReactNode
  if (liveTail) {
    if (tailQuery.isLoading) {
      body = <CenteredSpinner label="Loading the live tail…" />
    } else if (tailQuery.isError) {
      body = (
        <StateMessage
          tone="error"
          title="Couldn't load the live tail"
          message={tailQuery.error?.message}
          action={
            <Button variant="secondary" onClick={() => void tailQuery.refetch()}>
              Retry
            </Button>
          }
        />
      )
    } else if (tailEvents.length === 0) {
      body = (
        <AdminEmptyState
          title="No matching log events"
          message="Nothing has been logged yet."
        />
      )
    } else {
      body = renderLogList(tailEvents)
    }
  } else {
    if (logsQuery.isLoading) {
      body = <CenteredSpinner label="Loading logs…" />
    } else if (logsQuery.isError) {
      body = (
        <StateMessage
          tone="error"
          title="Couldn't load logs"
          message={logsQuery.error?.message}
          action={
            <Button variant="secondary" onClick={() => void logsQuery.refetch()}>
              Retry
            </Button>
          }
        />
      )
    } else if (durableEvents.length === 0) {
      body = (
        <AdminEmptyState
          title="No matching log events"
          message="Try a wider filter or time range."
        />
      )
    } else {
      body = renderLogList(durableEvents)
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-[1160px] flex-col gap-6 px-5 py-8 sm:px-8 lg:px-11">
      <AdminPageHeader
        title="Logs"
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void onExport('copy')}
              loading={exportLogs.isPending}
            >
              Copy for diagnosis
            </Button>
            <Button
              variant="secondary"
              size="sm"
              onClick={() => void onExport('download')}
              loading={exportLogs.isPending}
            >
              Download
            </Button>
          </>
        }
      />

      <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2">
        <label htmlFor="logs-level" className="sr-only">
          Level
        </label>
        <select
          id="logs-level"
          aria-describedby="logs-mode-description"
          className="h-9 w-[6.75rem] min-w-0 rounded-lg bg-surface-deep px-2 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50 disabled:opacity-50"
          value={level}
          disabled={liveTail}
          onChange={(e) => onLevelChange(e.target.value)}
        >
          {LEVELS.map((l) => (
            <option key={l || 'all'} value={l}>
              {l || 'All levels'}
            </option>
          ))}
        </select>

        <label htmlFor="logs-search" className="sr-only">
          Search
        </label>
        <input
          id="logs-search"
          aria-describedby="logs-mode-description"
          className="h-9 w-full min-w-0 rounded-lg bg-surface-deep px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none placeholder:text-faint focus-visible:ring-2 focus-visible:ring-gold/50"
          value={search}
          placeholder="Message, logger, or a request/download/tmdb id"
          onChange={(e) => onSearchChange(e.target.value)}
        />

        <label className="flex h-9 shrink-0 items-center gap-2 whitespace-nowrap text-sm text-muted">
          <input
            type="checkbox"
            aria-describedby="logs-mode-description"
            className="size-4 accent-gold outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
            checked={liveTail}
            onChange={(e) => setLiveTail(e.target.checked)}
          />
          Live tail
        </label>
      </div>

      <p
        id="logs-mode-description"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        className="-mt-3 font-mono text-[11px] text-faint"
      >
        {liveTail
          ? `Live, all levels — the in-memory tail, lost on restart.${
              tailQuery.data && tailQuery.data.dropped_count > 0
                ? ` ${tailQuery.data.dropped_count} record(s) dropped from durable storage since startup.`
                : ''
            }`
          : 'Level filters the durable store server-side; free text narrows what is shown here.'}
      </p>

      {body}

      {!liveTail && !logsQuery.isLoading && !logsQuery.isError ? (
        <div className="flex items-center justify-between gap-3 font-mono text-xs text-faint">
          <span>
            {total === 0 ? '0 events' : `${page * PAGE_SIZE + 1}–${page * PAGE_SIZE + loadedCount} of ${total}`}
          </span>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={page === 0}
              onClick={() => setPage((p) => Math.max(0, p - 1))}
            >
              Prev
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={!hasMore}
              onClick={() => setPage((p) => p + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
