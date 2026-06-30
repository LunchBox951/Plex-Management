import { useState } from 'react'
import { useBlocklist, useDeleteBlocklistEntry } from '../api/hooks'
import type { BlocklistEntry } from '../api/types'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useToast } from '../components/ui/toast'

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
  return 'Something went wrong. Please try again.'
}

export function Blocklist() {
  const { data, isLoading, error, refetch } = useBlocklist()
  const del = useDeleteBlocklistEntry()
  const { toast } = useToast()
  const [pendingRemoval, setPendingRemoval] = useState<BlocklistEntry | null>(null)

  if (isLoading) return <CenteredSpinner />

  if (error) {
    return (
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
    <div className="space-y-6">
      <div className="flex items-baseline gap-3">
        <h1 className="font-display text-2xl font-extrabold">Blocklist</h1>
        <span className="font-mono text-sm text-faint">{entries.length}</span>
      </div>

      {entries.length === 0 ? (
        <StateMessage
          title="Nothing blocklisted"
          message="Releases you mark failed-with-blocklist will appear here."
        />
      ) : (
        <ul className="space-y-3">
          {entries.map((entry) => (
            <li
              key={entry.id}
              className="flex items-center gap-4 rounded-xl border-hairline bg-surface p-4"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate font-medium text-ink">{entry.source_title}</p>
                <p className="mt-1 truncate font-mono text-xs text-muted">{metaLine(entry)}</p>
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
            onClick={() => void confirmRemove()}
          >
            Remove
          </Button>
        </div>
      </Dialog>
    </div>
  )
}
