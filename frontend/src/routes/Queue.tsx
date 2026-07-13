import { useEffect, useState, type ReactNode } from 'react'
import { useImportDownload, useMarkFailed, useQueue, useRelocateDownload } from '../api/hooks'
import type { DownloadStateValue, QueueItem } from '../api/types'
import { cn } from '../lib/cn'
import { downloadStatus, INTENT_CLASSES } from '../lib/status'
import type { ApiError } from '../lib/errors'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { AdminPageHeader } from '../components/ui/AdminPageHeader'
import { AdminEmptyState } from '../components/ui/AdminEmptyState'
import { adminRowPadding } from '../components/ui/adminStyles'
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

// The `failed_reason` prefix stamped by the backend's "download path not
// visible inside the container" block (issues #133/#157) — mirrors
// `import_service.PATH_NOT_VISIBLE_REASON_PREFIX` EXACTLY so this button
// recognizes the same rows the relocate endpoint itself accepts (409
// `not_relocatable` for any other reason), never a loosely-matched substring.
const PATH_NOT_VISIBLE_REASON_PREFIX = 'download path not visible inside the container '

/** Whether an import_blocked row is the specific, path-invisible shape the
 * relocate endpoint can act on (see {@link PATH_NOT_VISIBLE_REASON_PREFIX}). */
function isRelocatable(item: QueueItem): boolean {
  return item.status === 'import_blocked' && (item.failed_reason ?? '').startsWith(PATH_NOT_VISIBLE_REASON_PREFIX)
}

/**
 * The download states Mark failed / Blocklist & fail can act on without a 409:
 * every state that legally reaches `FailedPending` per the backend's
 * `TRANSITIONS` graph (`domain/state_machine.py`) -- `downloading`,
 * `metadata_fetching`, `import_pending`, `import_blocked`, `client_missing` --
 * PLUS `failed_pending` itself. That last one is not a `TRANSITIONS` edge (a
 * state can't transition to itself there) but `queue_service.mark_failed`
 * special-cases it as an "adopt": an operator call on an already-`failed_pending`
 * row (a stranded prior attempt, or one a reconcile cycle just detected) re-stamps
 * it with the fresh blocklist/remove_torrent flags instead of 409ing, so it is a
 * genuinely legal, backend-accepted operator action -- omitting it here would
 * violate "known legal actions remain available" for a real, reachable queue row.
 * `searching` and `importing` have no such edge or adopt path (mid-search / mid-import
 * can't be operator-failed) and are correctly excluded.
 *
 * Positive allowlist (issue #205), not a terminal denylist: a runtime-unknown
 * status (a future backend state this bundle predates, or corrupt/legacy data)
 * is absent from the set and fails CLOSED (no buttons shown), mirroring the
 * authoritative backend guard rather than merely excluding the one denylisted
 * `importing` value the old code checked.
 */
const MARK_FAILABLE = new Set<DownloadStateValue>([
  'downloading',
  'metadata_fetching',
  'import_pending',
  'import_blocked',
  'client_missing',
  'failed_pending',
])

function canMarkFailedStatus(status: string): boolean {
  return MARK_FAILABLE.has(status as DownloadStateValue)
}

/**
 * tv only: "S02E05" (a single episode), "S02E05-E07" (a multi-episode file) or
 * "S02 pack" (the whole season, no episodes named) — `null` for a movie
 * (`item.season` is always null there). Mirrors the naming convention in
 * `domain/naming.py::_episode_token`, but this is cosmetic only: nothing here
 * feeds back into a request.
 */
function tvScopeBadge(
  seasonNumber: number | null | undefined,
  episodeNumbers: number[] | null | undefined,
): string | null {
  if (seasonNumber == null) return null
  const season = `S${String(seasonNumber).padStart(2, '0')}`
  if (!episodeNumbers || episodeNumbers.length === 0) return `${season} pack`
  const ordered = [...new Set(episodeNumbers)].sort((a, b) => a - b)
  if (ordered.length === 1) return `${season}E${String(ordered[0]).padStart(2, '0')}`
  const contiguous = ordered[ordered.length - 1]! - ordered[0]! === ordered.length - 1
  const episodes = contiguous
    ? `E${String(ordered[0]).padStart(2, '0')}-E${String(ordered[ordered.length - 1]).padStart(2, '0')}`
    : ordered.map((e) => `E${String(e).padStart(2, '0')}`).join('')
  return `${season}${episodes}`
}

interface ScopeBadge {
  label: string
  status: string
}

function scopeBadgeLabel(
  seasonNumber: number | null | undefined,
  episodeNumbers: number[] | null | undefined,
  status: string,
): string | null {
  const badge = tvScopeBadge(seasonNumber, episodeNumbers)
  if (badge === null) return null
  return status === 'active' ? badge : `${badge} · ${downloadStatus(status).label}`
}

function seasonBadge(item: QueueItem): ScopeBadge | null {
  const label = tvScopeBadge(item.season, item.episodes)
  return label ? { label, status: 'active' } : null
}

function scopeBadges(item: QueueItem): ScopeBadge[] {
  if (item.scopes && item.scopes.length > 0) {
    return item.scopes
      .map((scope) => {
        // Widen to `string` (ScopeBadge's declared field type): otherwise TS
        // infers the narrower `DownloadScopeStatus | 'active'` literal union
        // here, which then fails the `.filter` type predicate below (a
        // predicate's asserted type must be assignable TO the inferred
        // parameter type, and the wider `ScopeBadge` is not assignable to
        // that narrower inferred literal type).
        const status: string = scope.status ?? 'active'
        const label = scopeBadgeLabel(scope.season, scope.episodes, status)
        return label ? { label, status } : null
      })
      .filter((badge): badge is ScopeBadge => badge !== null)
  }
  const legacyBadge = seasonBadge(item)
  return legacyBadge ? [legacyBadge] : []
}

/**
 * The row's primary heading (issue #134): the human media title when the download
 * is linked to a request that has one, falling back to the release ("download")
 * name persisted at grab time, and finally to a short hash fragment — the row
 * ALWAYS renders an identity, even for an orphaned download (its request deleted)
 * or a pre-migration row with no backfillable release_title.
 */
function queueHeading(item: QueueItem): string {
  return item.title ?? item.release_title ?? item.torrent_hash.slice(0, 12)
}

/** What the confirm dialog is about to do, captured when a button is pressed. */
interface PendingAction {
  downloadId: number
  blocklist: boolean
}

export function Queue() {
  const { data, isLoading, isError, error, refetch } = useQueue({ poll: true })
  const markFailed = useMarkFailed()
  const importDownload = useImportDownload()
  const relocateDownload = useRelocateDownload()
  const { toast } = useToast()
  const [pending, setPending] = useState<PendingAction | null>(null)

  const items = data?.queue ?? []
  const activeCount = items.filter((item) => isActive(item.status)).length
  const pendingItem = pending ? (items.find((item) => item.id === pending.downloadId) ?? null) : null
  const pendingActionable = pendingItem !== null && canMarkFailedStatus(pendingItem.status)

  useEffect(() => {
    if (pending && !pendingActionable) {
      setPending(null)
    }
  }, [pending, pendingActionable])

  async function runConfirm() {
    if (!pending || !pendingItem || !canMarkFailedStatus(pendingItem.status)) {
      setPending(null)
      return
    }
    try {
      await markFailed.mutateAsync({ downloadId: pendingItem.id, blocklist: pending.blocklist })
      toast({ title: 'Marked failed', intent: 'success' })
      setPending(null)
    } catch (err) {
      toast({ title: 'Action failed', description: (err as ApiError).message, intent: 'error' })
    }
  }

  // Operator retry for an import_blocked row (e.g. a naming conflict the
  // operator resolved out of band) — the same /queue/{id}/import the modal's
  // "Retry import" button calls.
  async function runImport(item: QueueItem) {
    try {
      await importDownload.mutateAsync(item.id)
      toast({ title: 'Retrying import', intent: 'success' })
    } catch (err) {
      toast({
        title: 'Import retry failed',
        description: (err as ApiError).message,
        intent: 'error',
      })
    }
  }

  // Operator correction for a path-invisible import_blocked row (issues
  // #133/#157): request qBittorrent move the torrent's data into the app's own
  // downloads root. This only REQUESTS the move — the operator retries the
  // import (the same "Retry import" button, still rendered alongside this one)
  // once qBittorrent settles it.
  async function runRelocate(item: QueueItem) {
    try {
      await relocateDownload.mutateAsync(item.id)
      toast({ title: 'Relocation requested', intent: 'success' })
    } catch (err) {
      toast({
        title: 'Relocate failed',
        description: (err as ApiError).message,
        intent: 'error',
      })
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
      <AdminEmptyState
        title="Nothing downloading"
        message="Grab a release from a title's detail to see it here."
      />
    )
  } else {
    content = (
      <ul className="flex flex-col gap-2">
        {items.map((item) => (
          <QueueCard
            key={item.id}
            item={item}
            disabled={markFailed.isPending}
            importPending={importDownload.isPending}
            relocatePending={relocateDownload.isPending}
            onAction={(target, blocklist) => setPending({ downloadId: target.id, blocklist })}
            onRetry={() => void runImport(item)}
            onRelocate={() => void runRelocate(item)}
          />
        ))}
      </ul>
    )
  }

  return (
    <div className="mx-auto w-full max-w-[1160px] space-y-6 px-5 py-8 sm:px-8">
      <AdminPageHeader
        title="Queue"
        count={data ? `${activeCount} active` : undefined}
        description="Everything the download client is holding, including blocked imports."
        status={
          <span className="inline-flex items-center gap-2">
            <span
              className={cn(
                'size-1.5 rounded-full',
                isError ? 'bg-error' : 'motion-safe:animate-pulse bg-downloading',
              )}
              aria-hidden
            />
            {isError ? 'reconnecting…' : 'updating every 2s'}
          </span>
        }
      />

      {content}

      {pending && pendingActionable ? (
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
  importPending,
  relocatePending,
  onAction,
  onRetry,
  onRelocate,
}: {
  item: QueueItem
  disabled: boolean
  importPending: boolean
  relocatePending: boolean
  onAction: (item: QueueItem, blocklist: boolean) => void
  onRetry: () => void
  onRelocate: () => void
}) {
  const presentation = downloadStatus(item.status)
  const showTransferProgress = item.status === 'downloading'
  const canMarkFailed = canMarkFailedStatus(item.status)
  const progress = Math.min(1, Math.max(0, item.progress ?? 0))
  const pct = Math.round(progress * 100)
  const shortHash = item.torrent_hash.slice(0, 12)
  const scopes = scopeBadges(item)
  const heading = queueHeading(item)
  // Only worth a second line when it says something the heading doesn't already:
  // a release_title that WAS the heading (title absent) would just repeat itself.
  const showReleaseSubline = Boolean(item.release_title) && item.release_title !== heading

  // Fall back to the gradient placeholder both when there's no poster_url AND when
  // a real one fails to load (404 / expired TMDB URL) — a bad URL must never leave
  // a broken-image icon sitting in the row (mirrors Requests.tsx's RequestRow).
  const [imgFailed, setImgFailed] = useState(false)
  const showImg = Boolean(item.poster_url) && !imgFailed

  return (
    <li
      className={cn(
        adminRowPadding,
        'grid min-w-0 grid-cols-[38px_minmax(0,1fr)] items-start gap-x-[13px] gap-y-2',
        'rounded-[10px] border border-hairline bg-surface',
        'lg:grid-cols-[38px_minmax(0,1fr)_auto]',
      )}
    >
      {showImg ? (
        <img
          src={item.poster_url ?? undefined}
          alt=""
          loading="lazy"
          className="aspect-[2/3] w-[38px] shrink-0 rounded-[4px] object-cover"
          onError={() => setImgFailed(true)}
        />
      ) : (
        <div
          aria-hidden="true"
          className="aspect-[2/3] w-[38px] shrink-0 rounded-[4px] bg-poster bg-gradient-to-b from-white/10 to-transparent"
        />
      )}

      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-x-[9px] gap-y-1">
          <p className="max-w-full min-w-0 truncate font-display text-[14px] leading-[1.2] font-bold text-ink">
            {heading}
          </p>
          <StatusBadge status={presentation} />
          {scopes.map((scope, index) => {
            const scopePresentation = scope.status === 'active' ? null : downloadStatus(scope.status)
            return (
              <span
                key={`${scope.label}-${index}`}
                className={cn(
                  'rounded px-[7px] py-1 font-mono text-[9.5px] leading-none font-semibold tracking-wide whitespace-nowrap ring-1 ring-inset',
                  scopePresentation
                    ? INTENT_CLASSES[scopePresentation.intent]
                    : INTENT_CLASSES.neutral,
                )}
              >
                {scope.label}
              </span>
            )
          })}
        </div>

        {showReleaseSubline ? (
          <p
            className="mt-[5px] truncate font-mono text-[11.5px] leading-[1.4] text-muted"
            title={item.release_title ?? undefined}
          >
            {item.release_title}
          </p>
        ) : null}

        <p className="mt-[3px] font-mono text-[10px] leading-none text-faint">
          <span title={item.torrent_hash}>{shortHash}</span>
          <span className="tabular-nums"> · seed {(item.seed_ratio ?? 0).toFixed(2)}</span>
        </p>

        {showTransferProgress ? (
          <div className="mt-[9px] flex w-full max-w-[470px] items-center gap-2.5">
            <ProgressBar
              value={progress}
              label={`${heading} download progress`}
              className="h-[5px] max-w-[420px] flex-1"
            />
            <span className="w-10 shrink-0 text-right font-mono text-[11px] leading-none font-medium text-muted tabular-nums">
              {pct}%
            </span>
          </div>
        ) : null}

        {item.failed_reason ? (
          <p className="mt-2 text-[12.5px] leading-relaxed text-error [overflow-wrap:anywhere]">
            {item.failed_reason}
          </p>
        ) : null}
      </div>

      <div className="col-span-2 flex min-w-0 max-w-full flex-wrap items-center justify-end gap-[7px] lg:col-span-1 lg:col-start-3 lg:row-start-1 lg:self-start">
        {isRelocatable(item) ? (
          <Button
            variant="secondary"
            size="sm"
            loading={relocatePending}
            disabled={relocatePending || disabled}
            onClick={onRelocate}
          >
            Relocate &amp; retry
          </Button>
        ) : null}
        {item.status === 'import_blocked' ? (
          <Button
            size="sm"
            loading={importPending}
            disabled={importPending || disabled}
            onClick={onRetry}
          >
            Retry import
          </Button>
        ) : null}
        {canMarkFailed ? (
          <>
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
          </>
        ) : null}
      </div>
    </li>
  )
}
