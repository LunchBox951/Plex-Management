import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  useCreateRequest,
  useGrab,
  useImportDownload,
  useMarkFailed,
  useQueue,
  useRequests,
  useSearchPreview,
} from '../api/hooks'
import type {
  AcceptedRelease,
  DiscoverResult,
  GrabRequest,
  QueueItem,
  RequestResponse,
  SearchPreviewRequest,
  SearchPreviewResponse,
} from '../api/types'
import type { ApiError } from '../lib/errors'
import { requestStatus, type StatusPresentation } from '../lib/status'
import { Dialog } from './ui/Dialog'
import { ReleaseList } from './ReleaseList'
import { Button } from './ui/Button'
import { StatusBadge } from './ui/StatusBadge'
import { ProgressBar } from './ui/ProgressBar'
import { CenteredSpinner } from './ui/feedback'
import { useToast } from './ui/toast'

interface TitleDetailModalProps {
  title: DiscoverResult | null
  open: boolean
  onOpenChange: (open: boolean) => void
}

function asApiError(error: unknown): ApiError {
  return error as ApiError
}

/**
 * The title's live lifecycle, derived by correlating the open title against the
 * polled request list and download queue. The backend `status` is a free string
 * carrying the canonical RequestStatus enum; the switch's default keeps unknown
 * values honest rather than throwing.
 */
type DerivedState =
  | { kind: 'none' }
  | { kind: 'pending' }
  | { kind: 'searching' }
  | { kind: 'downloading' }
  | { kind: 'no_acceptable_release' }
  | { kind: 'import_blocked' }
  | { kind: 'completed' }
  | { kind: 'available' }
  | { kind: 'failed' }
  | { kind: 'unknown'; status: string }

function deriveState(request: RequestResponse | null, optimistic: boolean): DerivedState {
  if (!request) return optimistic ? { kind: 'pending' } : { kind: 'none' }
  switch (request.status) {
    case 'pending':
      return { kind: 'pending' }
    case 'searching':
      return { kind: 'searching' }
    case 'downloading':
      return { kind: 'downloading' }
    case 'no_acceptable_release':
      return { kind: 'no_acceptable_release' }
    case 'import_blocked':
      return { kind: 'import_blocked' }
    case 'completed':
      return { kind: 'completed' }
    case 'available':
      return { kind: 'available' }
    case 'failed':
      return { kind: 'failed' }
    default:
      return { kind: 'unknown', status: request.status }
  }
}

/**
 * A request is grabbable only while it is non-terminal: the backend rejects a
 * terminal request id in /queue/grab (`request_not_active`). Terminal statuses are
 * `available` (already in the library), `completed` (imported, finalizing) and
 * `failed`. POST /requests can itself hand back a terminal row — Plex already owns
 * the title, or an existing completed/failed request is reused — so the create
 * path must gate Grab on the returned status, not merely the presence of an id.
 */
function isGrabbableStatus(status: string): boolean {
  return status !== 'available' && status !== 'completed' && status !== 'failed'
}

const FINALIZING: StatusPresentation = { label: 'Finalizing', intent: 'downloading' }
const IMPORT_BLOCKED: StatusPresentation = { label: 'Import blocked', intent: 'error' }

/**
 * The headline flow: request a title, run the decision engine (search-preview),
 * and grab a ranked release into the download client. Beyond the first grab, the
 * modal derives the title's live state from the polled request + queue and offers
 * a state-aware action zone — including an in-modal "report a problem" correction
 * that blocklists the bad release and re-arms the request (north star #1).
 */
export function TitleDetailModal({ title, open, onOpenChange }: TitleDetailModalProps) {
  const { toast } = useToast()
  const createRequest = useCreateRequest()
  const searchPreview = useSearchPreview()
  const grab = useGrab()
  const markFailed = useMarkFailed()
  const importDownload = useImportDownload()

  // Live correlation sources — poll while a title is open so the action zone
  // tracks the backend through search -> download -> import without a refresh.
  const requestsQuery = useRequests({ poll: open })
  const queueQuery = useQueue({ poll: open })

  const [requestId, setRequestId] = useState<number | null>(null)
  // Whether the just-created `requestId` is still grabbable. Tracked separately from
  // the id because POST /requests can return a TERMINAL row, and Grab must not arm
  // for it in the window before the /requests poll reveals the live status.
  const [createdGrabbable, setCreatedGrabbable] = useState(false)
  const [preview, setPreview] = useState<SearchPreviewResponse | null>(null)
  const [grabbingGuid, setGrabbingGuid] = useState<string | null>(null)
  // The confirm dialog for "report a problem"; carries the download to re-arm.
  const [reportFor, setReportFor] = useState<{ downloadId: number } | null>(null)

  // Reset the per-title flow whenever a different title is opened. Keyed on
  // media_type AND tmdb_id: TMDB movie/tv ids are independent namespaces and
  // collide, so tmdb_id alone would carry one title's request state onto another.
  const titleKey = title ? `${title.media_type}:${title.tmdb_id}` : null
  // Always-current title key, read by async handlers after an await to discard
  // results that belong to a title the modal has since moved on from.
  const latestTitleKey = useRef(titleKey)
  latestTitleKey.current = titleKey
  useEffect(() => {
    setRequestId(null)
    setCreatedGrabbable(false)
    setPreview(null)
    setGrabbingGuid(null)
    setReportFor(null)
  }, [titleKey])

  // The live request for this exact title (media_type + tmdb_id), if any. /requests
  // comes back in ascending id order and the backend intentionally allows
  // re-requesting an available/failed title, so a stale terminal row must not
  // shadow a newer active re-request: prefer a non-settled match, else the newest.
  const liveRequest = useMemo<RequestResponse | null>(() => {
    if (!title) return null
    const matches = (requestsQuery.data?.requests ?? []).filter(
      (r) => r.tmdb_id === title.tmdb_id && r.media_type === title.media_type,
    )
    const active = matches.find((r) => r.status !== 'available' && r.status !== 'failed')
    return active ?? matches[matches.length - 1] ?? null
  }, [requestsQuery.data, title])

  // A just-created request shows immediately even before the next poll lands. Used
  // for preview + queue correlation (a terminal request still owns its old download).
  const effectiveRequestId = requestId ?? liveRequest?.id ?? null

  // Grabbing needs a NON-terminal request: the backend rejects a terminal request id
  // in /queue/grab (request_not_active). A just-created request that came back active,
  // or the live one while still active, qualifies; a failed/available/completed one
  // does not — for those the user must "Request again" (a fresh request) before grabbing.
  const liveRequestGrabbable = liveRequest != null && isGrabbableStatus(liveRequest.status)
  // Prefer the just-created request (it shows before the next poll lands), but only
  // while it is grabbable AND the live request (once the poll has landed) has not since
  // gone terminal; otherwise fall through to the live request, itself gated on a
  // non-terminal status. A terminal create yields grabRequestId=null (Grab disabled).
  const grabRequestId =
    requestId !== null && createdGrabbable && (liveRequest === null || liveRequestGrabbable)
      ? requestId
      : liveRequestGrabbable
        ? liveRequest.id
        : null

  // The matching download. Prefer the request linkage (collision-free); fall back
  // to tmdb_id only when no request is known yet.
  const queueItem = useMemo<QueueItem | null>(() => {
    if (!title) return null
    const items = queueQuery.data?.queue ?? []
    const matches = items.filter((q) =>
      effectiveRequestId !== null
        ? q.media_request_id === effectiveRequestId
        : q.tmdb_id === title.tmdb_id,
    )
    return matches.length > 0 ? matches[matches.length - 1]! : null
  }, [queueQuery.data, title, effectiveRequestId])

  const state = deriveState(liveRequest, requestId !== null)

  const runPreview = useCallback(
    async (forRequestId: number | null) => {
      if (!title) return
      const startedKey = `${title.media_type}:${title.tmdb_id}`
      const body: SearchPreviewRequest =
        forRequestId !== null
          ? { request_id: forRequestId }
          : { tmdb_id: title.tmdb_id, media_type: title.media_type, title: title.title }
      if (forRequestId === null && typeof title.year === 'number') {
        body.year = title.year
      }
      try {
        const result = await searchPreview.mutateAsync(body)
        if (latestTitleKey.current !== startedKey) return // modal moved to another title
        setPreview(result)
      } catch (error) {
        if (latestTitleKey.current !== startedKey) return
        toast({ title: 'Search failed', description: asApiError(error).message, intent: 'error' })
      }
    },
    [title, searchPreview, toast],
  )

  const onRequest = useCallback(async () => {
    if (!title) return
    const startedKey = `${title.media_type}:${title.tmdb_id}`
    const titleName = title.title
    try {
      const created = await createRequest.mutateAsync({
        tmdb_id: title.tmdb_id,
        media_type: title.media_type,
      })
      if (latestTitleKey.current !== startedKey) return // don't apply A's request to title B
      setRequestId(created.id)
      // A terminal create (Plex already has the title, or a reused completed/failed
      // request) must not arm Grab — gate on the returned status, not just the id.
      setCreatedGrabbable(isGrabbableStatus(created.status))
      toast({ title: `Requested ${titleName}`, intent: 'success' })
      await runPreview(created.id)
    } catch (error) {
      if (latestTitleKey.current !== startedKey) return
      toast({ title: 'Request failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [title, createRequest, toast, runPreview])

  const onGrab = useCallback(
    async (release: AcceptedRelease) => {
      // Need a grabbable (non-terminal) request; never fire a second grab in flight.
      if (grabRequestId === null || grab.isPending) return
      // Send only the GUID — it uniquely identifies the clicked row. info_hash can
      // be shared across indexers and the backend matches it BEFORE guid, so
      // including it could grab a different release that shares the hash.
      const body: GrabRequest = { request_id: grabRequestId, guid: release.guid }
      setGrabbingGuid(release.guid)
      try {
        await grab.mutateAsync(body)
        toast({
          title: 'Grabbing release',
          description: 'Track its progress on the Queue screen.',
          intent: 'success',
        })
      } catch (error) {
        toast({ title: 'Grab failed', description: asApiError(error).message, intent: 'error' })
      } finally {
        setGrabbingGuid(null)
      }
    },
    [grabRequestId, grab, toast],
  )

  // Blocklist the bad release and re-arm the request to search again. Mirrors the
  // Queue screen's mark-failed confirm; no separate "issues" record is created.
  const runReport = useCallback(async () => {
    if (!reportFor) return
    try {
      await markFailed.mutateAsync({ downloadId: reportFor.downloadId, blocklist: true })
      toast({
        title: 'Reported',
        description: 'Blocklisted that release and re-armed the search.',
        intent: 'success',
      })
      setReportFor(null)
    } catch (error) {
      toast({
        title: 'Report failed',
        description: asApiError(error).message,
        intent: 'error',
      })
    }
  }, [reportFor, markFailed, toast])

  // Retry a blocked import (operator fixed the infra, or it was a transient Plex
  // hiccup). The reconcile loop re-runs validate -> place -> scan; an idempotent
  // re-import skips the copy if the file is already in place.
  const onRetryImport = useCallback(async () => {
    if (!queueItem) return
    try {
      await importDownload.mutateAsync(queueItem.id)
      toast({ title: 'Retrying import', intent: 'success' })
    } catch (error) {
      toast({ title: 'Import retry failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [queueItem, importDownload, toast])

  if (!title) return null

  const canGrab = grabRequestId !== null
  const meta = [title.year, title.media_type === 'tv' ? 'TV' : 'Movie'].filter(Boolean).join(' · ')

  // For a settled (failed/available) title, grabbing a release needs a FRESH request
  // (the old id is terminal) — onRequest creates one, then previews.
  const requestAgainButton = (
    <Button onClick={() => void onRequest()} loading={createRequest.isPending}>
      Request again
    </Button>
  )

  // The report button only makes sense when there's a real download to act on,
  // AND when mark-failed is a legal move. During the import copy/scan window the
  // download sits in 'importing' (raw DownloadState) while its owning request still
  // reads 'downloading'; the state machine only allows Importing -> Imported/
  // ImportBlocked, so a mark-failed there always 409s (invalid_state_transition).
  // Don't offer an action that can't succeed — once the import lands the title
  // re-renders as completed or import_blocked, where the correction paths reappear.
  const canReport = queueItem !== null && queueItem.status !== 'importing'
  const reportButton =
    canReport && queueItem ? (
      <Button variant="danger" onClick={() => setReportFor({ downloadId: queueItem.id })}>
        Report a problem
      </Button>
    ) : null

  const retryImportButton = queueItem ? (
    <Button onClick={() => void onRetryImport()} loading={importDownload.isPending}>
      Retry import
    </Button>
  ) : null

  const reSearchButton = (
    <Button
      variant="secondary"
      onClick={() => void runPreview(effectiveRequestId)}
      loading={searchPreview.isPending}
    >
      Re-search
    </Button>
  )

  // States where browsing/grabbing releases is part of the action — the decision
  // engine output stays visible (especially the honest no-acceptable-release).
  const showReleases =
    state.kind === 'none' ||
    state.kind === 'pending' ||
    state.kind === 'searching' ||
    state.kind === 'no_acceptable_release' ||
    state.kind === 'failed' ||
    state.kind === 'unknown'

  let actionZone: ReactNode
  switch (state.kind) {
    case 'none':
      actionZone = (
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => void onRequest()} loading={createRequest.isPending}>
            Request
          </Button>
          <Button
            variant="secondary"
            onClick={() => void runPreview(null)}
            loading={searchPreview.isPending}
          >
            Preview releases
          </Button>
        </div>
      )
      break
    case 'pending':
    case 'searching':
      actionZone = (
        <div className="flex flex-wrap items-center gap-4">
          <span className="inline-flex items-center gap-2 text-sm font-semibold text-searching">
            <span className="size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />
            Searching
          </span>
          {reSearchButton}
        </div>
      )
      break
    case 'downloading':
      actionZone = (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <StatusBadge status={requestStatus('downloading')} />
            <div className="flex flex-1 items-center gap-3">
              <ProgressBar value={queueItem?.progress ?? 0} />
              <span className="font-mono text-xs text-muted tabular-nums">
                {Math.round(Math.min(1, Math.max(0, queueItem?.progress ?? 0)) * 100)}%
              </span>
            </div>
          </div>
          {reportButton ? <div className="flex flex-wrap gap-2">{reportButton}</div> : null}
        </div>
      )
      break
    case 'no_acceptable_release':
      actionZone = (
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge status={requestStatus('no_acceptable_release')} />
          <span className="text-sm text-muted">
            Nothing was grabbed. Re-search to try again later.
          </span>
          {reSearchButton}
        </div>
      )
      break
    case 'import_blocked':
      // Honest, retryable: the download finished but the import was blocked (a bad
      // file or an import error). Show the reason + the two correction buttons —
      // retry the import, or reject the release (blocklist + re-search).
      actionZone = (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={IMPORT_BLOCKED} />
            {queueItem?.failed_reason ? (
              <span className="text-sm text-error">{queueItem.failed_reason}</span>
            ) : null}
          </div>
          <div className="flex flex-wrap gap-2">
            {retryImportButton}
            {reportButton}
          </div>
        </div>
      )
      break
    case 'completed':
      actionZone = (
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge status={FINALIZING} />
          <span className="text-sm text-muted">Imported — awaiting Plex confirmation.</span>
        </div>
      )
      break
    case 'available':
      // In the library. The download is terminal (gone from the active queue), so
      // there is no mark-failed target here; report-issue-with-purge (blocklist +
      // delete from Plex/disk + re-search) is a deferred next-beta capability.
      actionZone = (
        <div className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center gap-1.5 rounded-lg bg-available/15 px-3 py-1 text-sm font-semibold text-available ring-1 ring-available/30">
            ✓ In your library
          </span>
        </div>
      )
      break
    case 'failed':
      actionZone = (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={requestStatus('failed')} />
            {queueItem?.failed_reason ? (
              <span className="text-sm text-error">{queueItem.failed_reason}</span>
            ) : null}
          </div>
          {/* The prior request is terminal — "Request again" makes a fresh, grabbable
              one (re-searching the dead id would show releases that all fail to grab). */}
          <div className="flex flex-wrap gap-2">
            {requestAgainButton}
            {reportButton}
          </div>
        </div>
      )
      break
    case 'unknown':
      actionZone = (
        <div className="flex flex-wrap items-center gap-3">
          <StatusBadge status={requestStatus(state.status)} />
          {reSearchButton}
        </div>
      )
      break
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={title.title} description={title.title}>
      <div className="flex flex-col gap-6">
        <div className="flex gap-5">
          <div className="aspect-[2/3] w-28 shrink-0 overflow-hidden rounded-lg bg-poster ring-1 ring-white/10">
            {title.poster_url ? (
              <img src={title.poster_url} alt="" className="size-full object-cover" />
            ) : null}
          </div>
          <div className="min-w-0">
            <div className="font-mono text-xs text-faint">{meta}</div>
            {title.overview ? (
              <p className="mt-2 line-clamp-6 text-sm leading-relaxed text-muted">
                {title.overview}
              </p>
            ) : null}
            <div className="mt-4">{actionZone}</div>
          </div>
        </div>

        {showReleases ? (
          searchPreview.isPending && !preview ? (
            <CenteredSpinner label="Running the decision engine…" />
          ) : preview ? (
            <ReleaseList
              preview={preview}
              onGrab={(rel) => void onGrab(rel)}
              grabbingGuid={grabbingGuid}
              canGrab={canGrab}
            />
          ) : null
        ) : null}
      </div>

      {reportFor ? (
        <Dialog
          open
          onOpenChange={(next) => {
            if (!next) setReportFor(null)
          }}
          title="Blocklist this release and search again? It won't be grabbed again."
        >
          <div className="flex justify-end gap-3">
            <Button
              variant="secondary"
              onClick={() => setReportFor(null)}
              disabled={markFailed.isPending}
            >
              Cancel
            </Button>
            <Button variant="danger" loading={markFailed.isPending} onClick={() => void runReport()}>
              Blocklist &amp; re-search
            </Button>
          </div>
        </Dialog>
      ) : null}
    </Dialog>
  )
}
