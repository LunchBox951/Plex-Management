import { useState, type ReactNode } from 'react'
import {
  useCancelRequest,
  useForceImport,
  useGrab,
  useMarkFailed,
  useQueue,
  useReportIssue,
  useSetKeepForever,
} from '../../api/hooks'
import type { QueueItem, RequestResponse } from '../../api/types'
import { isNotImplemented } from '../../lib/errors'
import { isMarkFailableStatus } from '../../lib/status'
import type { ActionMenuItem } from '../ui/ActionMenu'
import { ConfirmDialog, type ConfirmEffect } from '../ui/ConfirmDialog'
import { useToast } from '../ui/toast'

/** Not-yet-imported statuses the cancel verb accepts (ADR-0014). */
const CANCELLABLE = new Set(['pending', 'searching', 'no_acceptable_release', 'downloading'])
/** Statuses where a fresh search can start right away (grab top). */
const SEARCHABLE = new Set(['pending', 'no_acceptable_release'])
/** Imported/available statuses that Report a problem (report-issue) accepts. */
const REPORTABLE = new Set(['available', 'partially_available', 'completed'])

/** The TV season a per-season verb targets: the first no-release season, else the first one tracked. */
function targetSeason(request: RequestResponse, want?: string): number | null {
  if (request.media_type !== 'tv') return null
  const seasons = request.seasons ?? []
  const preferred = want ? seasons.find((season) => season.status === want) : undefined
  return (preferred ?? seasons[0])?.season_number ?? null
}

type Pending =
  | { kind: 'cancel'; requests: RequestResponse[] }
  | { kind: 'mark-bad'; request: RequestResponse; download: QueueItem }
  | { kind: 'import-anyway'; request: RequestResponse; download: QueueItem }
  | { kind: 'report'; request: RequestResponse }

interface Callbacks {
  /** Open the request's detail (TitleDetailModal) — Review the problem. */
  openRequest: (request: RequestResponse) => void
  /** Open the release list — Pick a release myself. */
  pickRelease: (request: RequestResponse) => void
}

/**
 * Every admin action on a request, in one place, for the Requests table, the
 * Needs attention cards and the phone action sheet. Reversible actions run at
 * once with a toast (Undo where it exists); anything that deletes files,
 * blocklists or affects other people opens a ConfirmDialog first
 * (Interactions: "Undo or confirm, never both"). Only legal actions are listed.
 */
export function useRequestActions({ openRequest, pickRelease }: Callbacks): {
  itemsFor: (request: RequestResponse) => ActionMenuItem[]
  downloadFor: (request: RequestResponse) => QueueItem | null
  searchAgain: (request: RequestResponse) => void
  confirmCancel: (requests: RequestResponse[]) => void
  confirmMarkBad: (request: RequestResponse) => void
  confirmImportAnyway: (request: RequestResponse) => void
  canSearchAgain: (request: RequestResponse) => boolean
  canCancel: (request: RequestResponse) => boolean
  dialogs: ReactNode
} {
  const { toast } = useToast()
  const queue = useQueue({ poll: true })
  const grab = useGrab()
  const cancel = useCancelRequest()
  const markFailed = useMarkFailed()
  const forceImport = useForceImport()
  const report = useReportIssue()
  const keepForever = useSetKeepForever()
  const [pending, setPending] = useState<Pending | null>(null)
  const [error, setError] = useState<string | null>(null)

  const downloadFor = (request: RequestResponse): QueueItem | null =>
    queue.data?.queue.find(
      (item) => item.media_request_id === request.id && isMarkFailableStatus(item.status),
    ) ?? null

  const canSearchAgain = (request: RequestResponse) =>
    SEARCHABLE.has(request.status) && (request.media_type !== 'tv' || targetSeason(request) != null)
  const canCancel = (request: RequestResponse) => request.can_mutate && CANCELLABLE.has(request.status)

  const searchAgain = (request: RequestResponse) => {
    grab.mutate(
      { request_id: request.id, season: targetSeason(request, 'no_acceptable_release') },
      {
        onSuccess: () => toast({ title: `Searching again for ${request.title}`, intent: 'success' }),
        onError: (err) =>
          toast({ title: `Couldn't search for ${request.title}`, description: err.message, intent: 'error' }),
      },
    )
  }

  const setKeep = (request: RequestResponse, keep: boolean) => {
    keepForever.mutate(
      { requestId: request.id, keepForever: keep },
      {
        onSuccess: () =>
          toast({
            title: keep ? `${request.title} will be kept forever` : `${request.title} can be cleaned up again`,
            intent: 'success',
            action: { label: 'Undo', onClick: () => keepForever.mutate({ requestId: request.id, keepForever: !keep }) },
          }),
        onError: (err) => toast({ title: "Couldn't change Keep forever", description: err.message, intent: 'error' }),
      },
    )
  }

  const confirmCancel = (requests: RequestResponse[]) => {
    setError(null)
    setPending({ kind: 'cancel', requests })
  }
  const confirmMarkBad = (request: RequestResponse) => {
    const download = downloadFor(request)
    if (!download) return
    setError(null)
    setPending({ kind: 'mark-bad', request, download })
  }
  const confirmImportAnyway = (request: RequestResponse) => {
    const download = downloadFor(request)
    if (!download) return
    setError(null)
    setPending({ kind: 'import-anyway', request, download })
  }

  const itemsFor = (request: RequestResponse): ActionMenuItem[] => {
    const download = downloadFor(request)
    const items: ActionMenuItem[] = []
    if (request.status === 'import_blocked' || request.status === 'no_acceptable_release') {
      items.push({
        key: 'review',
        label: 'Review the problem',
        description: 'See what happened and decide',
        onSelect: () => openRequest(request),
      })
    } else {
      items.push({ key: 'open', label: 'Open details', onSelect: () => openRequest(request) })
    }
    if (canSearchAgain(request)) {
      items.push({ key: 'search', label: 'Search again', onSelect: () => searchAgain(request) })
    }
    if (SEARCHABLE.has(request.status) || request.status === 'searching') {
      items.push({ key: 'pick', label: 'Pick a release myself', onSelect: () => pickRelease(request) })
    }
    if (request.can_mutate) {
      items.push({
        key: 'keep',
        label: request.keep_forever ? 'Stop keeping forever' : 'Keep forever',
        description: request.keep_forever ? undefined : 'Never clean this up to free space',
        onSelect: () => setKeep(request, !request.keep_forever),
      })
    }
    if (request.status === 'import_blocked' && download) {
      items.push({
        key: 'import-anyway',
        label: 'Import anyway…',
        description: 'Add the files to Plex despite the problem',
        danger: true,
        onSelect: () => confirmImportAnyway(request),
      })
    }
    if (download) {
      items.push({
        key: 'mark-bad',
        label: 'Mark download as bad…',
        description: 'Blocklist this release and find another',
        danger: true,
        onSelect: () => confirmMarkBad(request),
      })
    }
    if (request.can_mutate && REPORTABLE.has(request.status) && (request.media_type !== 'tv' || (request.seasons?.length ?? 0) === 1)) {
      items.push({
        key: 'report',
        label: 'Report a problem…',
        description: 'Remove the files and find a different copy',
        danger: true,
        onSelect: () => {
          setError(null)
          setPending({ kind: 'report', request })
        },
      })
    }
    if (canCancel(request)) {
      items.push({ key: 'cancel', label: 'Cancel request…', danger: true, onSelect: () => confirmCancel([request]) })
    }
    return items
  }

  const close = () => setPending(null)
  const fail = (err: { message: string }) => setError(err.message)

  let dialog: ReactNode = null
  if (pending?.kind === 'cancel') {
    const count = pending.requests.length
    const only = pending.requests[0]
    const shared = pending.requests.some((request) => request.has_other_participants)
    const effects: ConfirmEffect[] = [
      { tone: 'risk', text: 'Any download in progress stops and its files are deleted.' },
      { tone: 'info', text: 'Anything already in Plex stays where it is.' },
    ]
    if (shared) effects.push({ tone: 'info', text: 'Everyone who asked will see it as Cancelled and can ask again.' })
    dialog = (
      <ConfirmDialog
        open
        onOpenChange={(open) => !open && close()}
        title={count === 1 && only ? `Cancel ${only.title}?` : `Cancel ${count} requests?`}
        intro={count === 1 ? 'This stops the request.' : 'This stops every selected request.'}
        effects={effects}
        confirmLabel={count === 1 ? 'Cancel request' : `Cancel ${count} requests`}
        cancelLabel="Keep"
        pending={cancel.isPending}
        error={error}
        onConfirm={() => {
          void Promise.allSettled(pending.requests.map((request) => cancel.mutateAsync(request.id))).then(
            (results) => {
              const failed = results.filter((result) => result.status === 'rejected').length
              if (failed > 0) {
                setError(`${failed} of ${count} couldn't be cancelled. Refresh to see their state.`)
                return
              }
              close()
              toast({ title: count === 1 ? `Cancelled ${only?.title ?? ''}` : `Cancelled ${count} requests`, intent: 'success' })
            },
          )
        }}
      />
    )
  } else if (pending?.kind === 'mark-bad') {
    dialog = (
      <ConfirmDialog
        open
        onOpenChange={(open) => !open && close()}
        title="Mark this download as bad?"
        intro={pending.download.release_title ?? pending.request.title}
        effects={[
          { tone: 'risk', text: "This release is blocklisted, so it's never picked again." },
          { tone: 'risk', text: 'The download and its files are deleted from the download client.' },
          { tone: 'undo', text: 'The request goes back to searching for a different copy.' },
          { tone: 'info', text: 'You can unblock it later under Manage › Blocklist.' },
        ]}
        confirmLabel="Mark as bad"
        pending={markFailed.isPending}
        error={error}
        onConfirm={() =>
          markFailed.mutate(
            { downloadId: pending.download.id, blocklist: true },
            {
              onSuccess: () => {
                close()
                toast({ title: `Marked bad: ${pending.request.title}`, description: 'Looking for a different copy.', intent: 'success' })
              },
              onError: fail,
            },
          )
        }
      />
    )
  } else if (pending?.kind === 'import-anyway') {
    dialog = (
      <ConfirmDialog
        open
        onOpenChange={(open) => !open && close()}
        title="Import files that didn't pass the check?"
        intro={pending.download.failed_reason ?? `The download for ${pending.request.title} was blocked during import.`}
        effects={[
          { tone: 'risk', text: 'The files are added to Plex as they are, so Plex may show the wrong episodes or version.' },
          { tone: 'risk', text: "The request is marked done and won't be searched again." },
          { tone: 'undo', text: 'To undo it later, open the title and choose Report a problem: the files are removed and a new search starts.' },
        ]}
        acknowledge="I've checked the files and want them imported anyway"
        confirmLabel="Import anyway"
        pending={forceImport.isPending}
        error={error}
        onConfirm={() =>
          forceImport.mutate(pending.download.id, {
            onSuccess: () => {
              close()
              toast({ title: `Imported ${pending.request.title}`, intent: 'success' })
            },
            onError: (err) =>
              setError(isNotImplemented(err) ? "Import anyway isn't available yet on this server." : err.message),
          })
        }
      />
    )
  } else if (pending?.kind === 'report') {
    const season = targetSeason(pending.request)
    dialog = (
      <ConfirmDialog
        open
        onOpenChange={(open) => !open && close()}
        title={`Report a problem with ${pending.request.title}?`}
        intro="Use this when the file in Plex is wrong or bad quality."
        effects={[
          { tone: 'risk', text: 'The release is blocklisted and its files are removed from Plex and the download client.' },
          { tone: 'undo', text: 'A search for a different copy starts right away.' },
        ]}
        confirmLabel="Remove & search again"
        pending={report.isPending}
        error={error}
        onConfirm={() =>
          report.mutate(
            { requestId: pending.request.id, reason: 'bad_quality', season },
            {
              onSuccess: () => {
                close()
                toast({ title: `Reported ${pending.request.title}`, description: 'Looking for a different copy.', intent: 'success' })
              },
              onError: fail,
            },
          )
        }
      />
    )
  }

  return {
    itemsFor,
    downloadFor,
    searchAgain,
    confirmCancel,
    confirmMarkBad,
    confirmImportAnyway,
    canSearchAgain,
    canCancel,
    dialogs: dialog,
  }
}
