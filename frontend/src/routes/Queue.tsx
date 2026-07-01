import { useState, type ReactNode } from 'react'
import { useMarkFailed, useQueue } from '../api/hooks'
import type { QueueItem } from '../api/types'
import { cn } from '../lib/cn'
import { downloadStatus } from '../lib/status'
import type { ApiError } from '../lib/errors'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { StatusBadge } from '../components/ui/StatusBadge'
import { ProgressBar } from '../components/ui/ProgressBar'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { useToast } from '../components/ui/toast'

/** A download still in flight — counted in the header, never an empty queue. */
function isActive(status: string): boolean {
  const { intent } = downloadStatus(status)
  return intent === 'downloading' || intent === 'searching'
}

/**
 * tv only: "S02E05" (a single episode), "S02E05-E07" (a multi-episode file) or
 * "S02 pack" (the whole season, no episodes named) — `null` for a movie
 * (`item.season` is always null there). Mirrors the naming convention in
 * `domain/naming.py::_episode_token`, but this is cosmetic only: nothing here
 * feeds back into a request.
 */
function seasonBadge(item: QueueItem): string | null {
  if (item.season == null) return null
  const season = `S${String(item.season).padStart(2, '0')}`
  if (!item.episodes || item.episodes.length === 0) return `${season} pack`
  const episodes = item.episodes.map((e) => `E${String(e).padStart(2, '0')}`).join('-')
  return `${season}${episodes}`
}

/** What the confirm dialog is about to do, captured when a button is pressed. */
interface PendingAction {
  item: QueueItem
  blocklist: boolean
}

export function Queue() {
  const { data, isLoading, isError, error, refetch } = useQueue({ poll: true })
  const markFailed = useMarkFailed()
  const { toast } = useToast()
  const [pending, setPending] = useState<PendingAction | null>(null)

  const items = data?.queue ?? []
  const activeCount = items.filter((item) => isActive(item.status)).length

  async function runConfirm() {
    if (!pending) return
    const { item, blocklist } = pending
    try {
      await markFailed.mutateAsync({ downloadId: item.id, blocklist })
      toast({ title: 'Marked failed', intent: 'success' })
      setPending(null)
    } catch (err) {
      toast({ title: 'Action failed', description: (err as ApiError).message, intent: 'error' })
    }
  }

  let content: ReactNode
  if (isLoading) {
    content = <CenteredSpinner label="Loading queue" />
  } else if (isError && !data) {
    // Only blank the view when there's nothing to show. A failed *background*
    // poll keeps the last good queue on screen (see the 'reconnecting' hint).
    content = (
      <StateMessage
        tone="error"
        title="Couldn't load the queue"
        message={error.message}
        action={
          <Button variant="secondary" size="sm" onClick={() => void refetch()}>
            Retry
          </Button>
        }
      />
    )
  } else if (items.length === 0) {
    content = (
      <StateMessage
        title="Nothing downloading"
        message="Grab a release from a title's detail to see it here."
      />
    )
  } else {
    content = (
      <div className="space-y-3">
        {items.map((item) => (
          <QueueCard
            key={item.id}
            item={item}
            disabled={markFailed.isPending}
            onAction={(target, blocklist) => setPending({ item: target, blocklist })}
          />
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <h1 className="font-display text-2xl font-extrabold">Queue</h1>
          {data ? (
            <span className="font-mono text-sm text-muted">
              {activeCount} active
            </span>
          ) : null}
        </div>
        <span className="inline-flex items-center gap-2 font-mono text-xs text-faint">
          <span
            className={cn(
              'size-1.5 rounded-full',
              isError ? 'bg-error' : 'animate-pulse bg-downloading',
            )}
            aria-hidden
          />
          {isError ? 'reconnecting…' : 'updating every 2s'}
        </span>
      </header>

      {content}

      {pending ? (
        <Dialog
          open
          onOpenChange={(open) => {
            if (!open) setPending(null)
          }}
          title={
            pending.blocklist
              ? "Blocklist this release and mark failed? It won't be grabbed again."
              : 'Mark this download failed?'
          }
        >
          <div className="flex justify-end gap-3">
            <Button
              variant="secondary"
              onClick={() => setPending(null)}
              disabled={markFailed.isPending}
            >
              Cancel
            </Button>
            <Button variant="danger" loading={markFailed.isPending} onClick={() => void runConfirm()}>
              {pending.blocklist ? 'Blocklist & fail' : 'Mark failed'}
            </Button>
          </div>
        </Dialog>
      ) : null}
    </div>
  )
}

function QueueCard({
  item,
  disabled,
  onAction,
}: {
  item: QueueItem
  disabled: boolean
  onAction: (item: QueueItem, blocklist: boolean) => void
}) {
  const presentation = downloadStatus(item.status)
  const isDownloadingLike = presentation.intent === 'downloading'
  const canMarkFailed = item.status !== 'importing'
  const pct = Math.round(Math.min(1, Math.max(0, item.progress ?? 0)) * 100)
  const shortHash = item.torrent_hash.slice(0, 12)
  const detail = isDownloadingLike ? `${pct}%` : undefined
  const season = seasonBadge(item)

  return (
    <div className="rounded-xl border border-hairline bg-surface p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <StatusBadge status={presentation} {...(detail ? { detail } : {})} />
          {season ? (
            <span className="rounded bg-white/8 px-1.5 py-0.5 font-mono text-[10px] font-semibold tracking-wide text-muted ring-1 ring-white/10">
              {season}
            </span>
          ) : null}
          <span className="font-mono text-xs text-faint">{shortHash}</span>
          <span className="font-mono text-xs text-faint tabular-nums">
            seed {(item.seed_ratio ?? 0).toFixed(2)}
          </span>
        </div>
        {canMarkFailed ? (
          <div className="flex shrink-0 items-center gap-2">
            <Button
              variant="danger"
              size="sm"
              disabled={disabled}
              onClick={() => onAction(item, false)}
            >
              Mark failed
            </Button>
            <Button
              variant="danger"
              size="sm"
              disabled={disabled}
              onClick={() => onAction(item, true)}
            >
              Blocklist &amp; fail
            </Button>
          </div>
        ) : null}
      </div>

      {isDownloadingLike ? (
        <div className="mt-3 flex items-center gap-3">
          <ProgressBar value={item.progress ?? 0} label="Download progress" />
          <span className="font-mono text-xs text-muted tabular-nums">{pct}%</span>
        </div>
      ) : null}

      {item.failed_reason ? (
        <p className="mt-3 text-sm text-error">{item.failed_reason}</p>
      ) : null}
    </div>
  )
}
