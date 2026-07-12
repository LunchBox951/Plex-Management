import { useMemo, useState, type ReactNode } from 'react'
import { useExportLogs, useLogs, useLogsTail, type LogsFilter } from '../api/hooks'
import type { LiveLogRecordItem, LogEventItem } from '../api/types'
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
  INFO: 'text-muted',
  WARNING: 'text-searching',
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
    <li className="flex flex-col gap-1 border-b border-hairline px-1 py-2 last:border-0">
      <div className="flex flex-wrap items-baseline gap-2 font-mono text-[11px] text-faint">
        <span>{new Date(event.created_at).toLocaleString()}</span>
        <span className={cn('font-semibold', levelClass(event.level))}>{event.level}</span>
        <span className="truncate">{event.logger}</span>
      </div>
      <p className="text-sm break-words text-ink">{event.message}</p>
      {event.context ? (
        <p className="font-mono text-[10px] break-all text-faint">{JSON.stringify(event.context)}</p>
      ) : null}
    </li>
  )
}

function renderLogList(events: LogLike[]): ReactNode {
  return (
    <ul className="rounded-xl border border-hairline bg-surface px-3">
      {events.map((event, index) => (
        <LogRow key={'id' in event ? `row-${event.id}` : `tail-${index}`} event={event} />
      ))}
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
      body = <StateMessage title="No matching log events" message="Nothing has been logged yet." />
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
        <StateMessage title="No matching log events" message="Try a wider filter or time range." />
      )
    } else {
      body = renderLogList(durableEvents)
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-[1160px] flex-col gap-6 px-5 py-8 sm:px-8">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="font-display text-2xl font-extrabold">Logs</h1>
        <div className="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            onClick={() => void onExport('copy')}
            loading={exportLogs.isPending}
          >
            Copy for diagnosis
          </Button>
          <Button
            variant="secondary"
            onClick={() => void onExport('download')}
            loading={exportLogs.isPending}
          >
            Download
          </Button>
        </div>
      </header>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1.5 text-sm font-medium text-muted">
          Level
          <select
            className="h-10 rounded-lg bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50 disabled:opacity-50"
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
        </label>

        <label className="flex min-w-48 flex-1 flex-col gap-1.5 text-sm font-medium text-muted">
          Search
          <input
            className="h-10 rounded-lg bg-bg px-3 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none placeholder:text-faint focus-visible:ring-2 focus-visible:ring-gold/50"
            value={search}
            placeholder="Message, logger, or a request/download/tmdb id"
            onChange={(e) => onSearchChange(e.target.value)}
          />
        </label>

        <label className="flex h-10 items-center gap-2 text-sm text-muted">
          <input
            type="checkbox"
            checked={liveTail}
            onChange={(e) => setLiveTail(e.target.checked)}
          />
          Live tail
        </label>
      </div>

      <p className="-mt-3 font-mono text-[11px] text-faint">
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
