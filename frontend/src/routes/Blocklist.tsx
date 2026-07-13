import { useState } from 'react'
import { useBlocklist, useDeleteBlocklistEntry } from '../api/hooks'
import type { BlocklistEntry } from '../api/types'
import { AdminEmptyState } from '../components/ui/AdminEmptyState'
import { AdminPageHeader } from '../components/ui/AdminPageHeader'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { adminRowPadding } from '../components/ui/adminStyles'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'
import { cn } from '../lib/cn'

/** The mono meta line under a title: "{reason} · {indexer} · {added_at}", null parts skipped. */
function metaLine(entry: BlocklistEntry): string {
  const parts: string[] = [entry.reason]
  if (entry.indexer) parts.push(entry.indexer)
  if (entry.added_at) parts.push(new Date(entry.added_at).toLocaleString())
  return parts.join(' · ')
}

/** Pull a human message off an unknown rejection (ApiError carries `.message`). */
function errorMessage(err: unknown): string {
  if (err !== null && typeof err === 'object' && 'message' in err) {
    const message = (err as { message: unknown }).message
    if (typeof message === 'string') return message
  }
  return 'The request failed for an unknown reason. Try again.'
}

export function Blocklist() {
  const { data, isLoading, error, refetch } = useBlocklist()
  const del = useDeleteBlocklistEntry()
  const { toast } = useToast()
  const [pendingRemoval, setPendingRemoval] = useState<BlocklistEntry | null>(null)

  if (isLoading) {
    return (
      <div className="mx-auto flex w-full max-w-[1060px] flex-col gap-6 px-5 py-8 sm:px-8">
        <AdminPageHeader title="Blocklist" />
        <CenteredSpinner />
      </div>
    )
  }

  if (error) {
    return (
      <div className="mx-auto flex w-full max-w-[1060px] flex-col gap-6 px-5 py-8 sm:px-8">
        <AdminPageHeader title="Blocklist" />
        <StateMessage
          tone="error"
          title="Couldn't load the blocklist"
          message={error.message}
          action={
            <Button variant="secondary" onClick={() => void refetch()}>
              Retry
            </Button>
          }
        />
      </div>
    )
  }

  const entries = data?.entries ?? []

  const confirmRemove = async () => {
    if (!pendingRemoval) return
    try {
      await del.mutateAsync(pendingRemoval.id)
      toast({ title: 'Removed from blocklist', intent: 'success' })
      setPendingRemoval(null)
    } catch (err) {
      toast({ title: 'Remove failed', description: errorMessage(err), intent: 'error' })
    }
  }

  return (
    <div className="mx-auto w-full max-w-[1060px] space-y-6 px-5 py-8 sm:px-8">
      <AdminPageHeader title="Blocklist" count={String(entries.length)} />

      {entries.length === 0 ? (
        <AdminEmptyState
          title="Nothing blocklisted"
          message="Releases you mark failed-with-blocklist will appear here."
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {entries.map((entry) => (
            <li
              key={entry.id}
              className={cn(
                adminRowPadding,
                'grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 gap-y-2',
                'rounded-[10px] border border-hairline bg-surface',
              )}
            >
              <div className="min-w-0 flex-1">
                <p
                  title={entry.source_title}
                  className="truncate text-[13px] leading-snug font-semibold text-ink"
                >
                  {entry.source_title}
                </p>
                <p className="mt-1 font-mono text-[11px] leading-snug break-words text-muted">
                  {metaLine(entry)}
                </p>
              </div>
              <Button
                variant="danger"
                size="sm"
                className="shrink-0"
                onClick={() => setPendingRemoval(entry)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}

      <Dialog
        open={pendingRemoval !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRemoval(null)
        }}
        title="Remove blocklist entry"
        description="Remove this blocklist entry? The release becomes eligible to grab again."
      >
        <p className="text-sm text-muted">
          Remove this blocklist entry? The release becomes eligible to grab again.
        </p>
        <div className="mt-6 flex justify-end gap-3">
          <Button
            variant="secondary"
            onClick={() => setPendingRemoval(null)}
            disabled={del.isPending}
          >
            Cancel
          </Button>
          <Button
            variant="danger"
            loading={del.isPending}
            disabled={del.isPending}
            aria-label="Remove blocklist entry"
            onClick={() => void confirmRemove()}
          >
            Remove
          </Button>
        </div>
      </Dialog>
    </div>
  )
}
