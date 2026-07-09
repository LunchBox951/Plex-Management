import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  useAuthMe,
  useCancelRequest,
  useCreateRequest,
  useGrab,
  useImportDownload,
  useMarkFailed,
  useQueue,
  useReportIssue,
  useRequests,
  useSearchPreview,
  useSetKeepForever,
  type ReportReason,
} from '../api/hooks'
import type {
  AcceptedRelease,
  CreateRequestBody,
  DiscoverResult,
  GrabRequest,
  QueueItem,
  RequestResponse,
  SearchPreviewRequest,
  SearchPreviewResponse,
  SeasonStatus,
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
  | { kind: 'waiting_for_air_date' }
  | { kind: 'import_blocked' }
  | { kind: 'completed' }
  | { kind: 'available' }
  | { kind: 'failed' }
  // ADR-0012: the disk-pressure sweep reclaimed this title's (or, for tv, this
  // season's) file. Settled, same as available/failed — "Request again" makes
  // a fresh, grabbable request.
  | { kind: 'evicted' }
  // ADR-0014: the operator cancelled a not-yet-imported request. Settled, same as
  // available/failed/evicted — "Request again" makes a fresh, grabbable request.
  | { kind: 'cancelled' }
  | { kind: 'unknown'; status: string }

/**
 * The status that actually drives the action zone. For a movie ``season`` is
 * always ``null`` and this is just ``request.status`` (unchanged). For a tv
 * request it is the ONE selected season's own status (``request.seasons`` never
 * carries ``partially_available`` — that string only ever exists as the
 * show-level rollup fold of per-season statuses, see
 * `domain.season_rollup.rollup_status`) so a show with season 1 available and
 * season 2 still downloading shows season 2's real 'downloading' state rather
 * than the rollup. Falls back to ``request.status`` when the season isn't
 * (yet) a tracked row — e.g. the instant after create, before the season list
 * has loaded.
 */
function seasonStatusFor(request: RequestResponse, season: number | null): string {
  if (season == null) return request.status
  return request.seasons?.find((s) => s.season_number === season)?.status ?? request.status
}

function queueItemCoversSeason(item: QueueItem, season: number | null): boolean {
  if (season == null) return true
  if (item.scopes && item.scopes.length > 0) {
    return item.scopes.some((scope) => scope.season === season)
  }
  return item.season === season
}

/** The first non-terminal tracked season, else the first tracked season. */
function firstActionableSeason(seasons: SeasonStatus[] | null): number | null {
  if (!seasons || seasons.length === 0) return null
  const active = seasons.find((s) => isGrabbableStatus(s.status))
  return (active ?? seasons[0])!.season_number
}

/**
 * The season driving search/grab/view, resolved against a GIVEN season list: the
 * operator's explicit pick wins as long as it is still tracked in that list (or
 * nothing is tracked yet); otherwise the first actionable tracked season, else
 * season 1. Pulled out so `onRequest` can resolve against the BRAND NEW
 * `created.seasons` the instant a request is created, rather than the stale
 * `effectiveSeasons` from the render that preceded it — `activeSeason` predates the
 * request and was never judged against seasons that didn't exist yet.
 */
function resolveSeason(seasons: SeasonStatus[] | null, activeSeason: number | null): number {
  if (activeSeason != null && (seasons == null || seasons.some((s) => s.season_number === activeSeason))) {
    return activeSeason
  }
  return firstActionableSeason(seasons) ?? 1
}

function deriveState(status: string | null, optimistic: boolean): DerivedState {
  if (status == null) return optimistic ? { kind: 'pending' } : { kind: 'none' }
  switch (status) {
    case 'pending':
      return { kind: 'pending' }
    case 'searching':
      return { kind: 'searching' }
    case 'downloading':
      return { kind: 'downloading' }
    case 'no_acceptable_release':
      return { kind: 'no_acceptable_release' }
    case 'waiting_for_air_date':
      return { kind: 'waiting_for_air_date' }
    case 'import_blocked':
      return { kind: 'import_blocked' }
    case 'completed':
      return { kind: 'completed' }
    case 'available':
      return { kind: 'available' }
    case 'failed':
      return { kind: 'failed' }
    case 'evicted':
      return { kind: 'evicted' }
    case 'cancelled':
      return { kind: 'cancelled' }
    default:
      return { kind: 'unknown', status }
  }
}

/**
 * The not-yet-imported request statuses a cancel may act on (ADR-0014). Mirrors
 * the backend `CANCELLABLE_REQUEST_STATUS_VALUES`; gates the "Cancel request"
 * action against the PARENT request status (for tv the rollup), never the
 * per-season zone — a `partially_available` show is not cancellable wholesale.
 */
const CANCELLABLE_STATUSES = new Set([
  'pending',
  'searching',
  'no_acceptable_release',
  'waiting_for_air_date',
  'downloading',
])

/**
 * A request is grabbable only while it is non-terminal: the backend rejects a
 * terminal request id in /queue/grab (`request_not_active`). Terminal statuses are
 * `available` (already in the library), `completed` (imported, finalizing),
 * `failed`, and `evicted` (ADR-0012 — the disk-pressure sweep already deleted the
 * file; re-grabbing the same id would just re-arm a row the backend now treats as
 * settled, see `_SETTLED_REQUEST_STATUSES`). POST /requests can itself hand back a
 * terminal row — Plex already owns the title, or an existing completed/failed/
 * evicted request is reused — so the create path must gate Grab on the returned
 * status, not merely the presence of an id.
 */
function isGrabbableStatus(status: string): boolean {
  return (
    status !== 'available' &&
    status !== 'completed' &&
    status !== 'failed' &&
    status !== 'evicted' &&
    status !== 'waiting_for_air_date' &&
    // ADR-0014: a `cancelled` request is settled and terminal for grab (the backend
    // rejects a cancelled id in /queue/grab with request_not_active); a fresh
    // "Request again" must be made before grabbing. Mirrors the backend's
    // `_SETTLED_REQUEST_STATUSES` / `TERMINAL_REQUEST_STATUS_VALUES`.
    status !== 'cancelled'
  )
}

/**
 * Whether the Grab button should be live for the SELECTED season (or, for a movie,
 * the request itself). A season is grabbable when its own status is non-terminal —
 * OR when it is `failed` but the PARENT request is still non-terminal. The backend
 * gates /queue/grab on the parent, so a failed season under an active (e.g.
 * `partially_available`) show can be re-searched from the UI; without this it would
 * dead-end into "Request again", which dedups straight back to the same failed
 * season (an active show is never re-created), leaving the user unable to retry it.
 * A movie (`season == null` → `seasonStatusFor` returns `request.status`) never
 * enters the failed branch: its failed status equals the parent's terminal one.
 */
function isSeasonGrabbable(request: RequestResponse, season: number | null): boolean {
  const seasonStatus = seasonStatusFor(request, season)
  if (isGrabbableStatus(seasonStatus)) return true
  return season != null && seasonStatus === 'failed' && isGrabbableStatus(request.status)
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
  // Shared (non-admin) sessions get a REQUEST-ONLY modal: the preview / grab /
  // correction / keep-forever verbs all sit behind `require_admin` server-side,
  // so exposing them would only manufacture 403 `admin_required` failures.
  // Defaults to the restricted view until /auth/me resolves (fail closed) —
  // mirrors AdminGate's read of the same cached query.
  const auth = useAuthMe()
  const isAdmin = auth.data?.is_admin ?? auth.data?.user?.is_admin ?? false
  const createRequest = useCreateRequest()
  const searchPreview = useSearchPreview()
  const grab = useGrab()
  const markFailed = useMarkFailed()
  const importDownload = useImportDownload()
  const setKeepForever = useSetKeepForever()
  const reportIssue = useReportIssue()
  const cancelRequest = useCancelRequest()

  // Live correlation sources — poll while a title is open so the action zone
  // tracks the backend through search -> download -> import without a refresh.
  // GET /queue is admin-only: keep it entirely idle for shared sessions.
  const requestsQuery = useRequests({ poll: open })
  const queueQuery = useQueue({ poll: open, enabled: isAdmin })

  const [requestId, setRequestId] = useState<number | null>(null)
  // Whether the just-created `requestId` is still grabbable. Tracked separately from
  // the id because POST /requests can return a TERMINAL row, and Grab must not arm
  // for it in the window before the /requests poll reveals the live status.
  const [createdGrabbable, setCreatedGrabbable] = useState(false)
  // tv only: the just-created request's own per-season rollup, shown before the
  // next /requests poll lands — mirrors `createdGrabbable`'s create-then-poll gap.
  const [createdSeasons, setCreatedSeasons] = useState<SeasonStatus[] | null>(null)
  // tv only, read at Request time: track the whole aired series (default) or just
  // the one season named below. Irrelevant once a request exists.
  const [wholeSeries, setWholeSeries] = useState(true)
  // tv only: the season the operator explicitly picked (to search/grab/view).
  // `null` until they touch the control — `currentSeason` below resolves the
  // real default (the first actionable tracked season, else 1).
  const [activeSeason, setActiveSeason] = useState<number | null>(null)
  const [preview, setPreview] = useState<SearchPreviewResponse | null>(null)
  const [grabbingGuid, setGrabbingGuid] = useState<string | null>(null)
  // The confirm dialog for "report a problem"; carries the download to re-arm.
  const [reportFor, setReportFor] = useState<{ downloadId: number } | null>(null)
  // The confirm dialog for reporting an IMPORTED/available title (ADR-0014): the
  // new report-issue endpoint (blocklist + purge torrent/file + inline re-search),
  // distinct from `reportFor`'s queue mark-failed. Carries the request + season.
  const [reportIssueFor, setReportIssueFor] = useState<{
    requestId: number
    season: number | null
  } | null>(null)
  const [reportReason, setReportReason] = useState<ReportReason>('bad_quality')
  // The confirm dialog for cancelling a not-yet-imported request (ADR-0014).
  const [cancelFor, setCancelFor] = useState<{ requestId: number } | null>(null)
  // The confirm dialog for Re-acquire (issue #131) -- movie-only, force-creates a
  // fresh grabbable request even though the title still reads present in Plex.
  const [reacquireOpen, setReacquireOpen] = useState(false)

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
    setCreatedSeasons(null)
    setWholeSeries(true)
    setActiveSeason(null)
    setPreview(null)
    setGrabbingGuid(null)
    setReportFor(null)
    setReportIssueFor(null)
    setReportReason('bad_quality')
    setCancelFor(null)
    setReacquireOpen(false)
  }, [titleKey])

  // The live request for this exact title (media_type + tmdb_id), if any. /requests
  // comes back in ascending id order and the backend intentionally allows
  // re-requesting an available/failed/evicted title, so a stale terminal row must
  // not shadow a newer active re-request: prefer a non-settled match, else the
  // newest. `evicted` (ADR-0012) is excluded for the SAME reason as
  // available/failed — it must never shadow a fresh re-request created after the
  // disk-pressure sweep reclaimed the old one (mirrors the backend's own
  // `_SETTLED_REQUEST_STATUSES`).
  const liveRequest = useMemo<RequestResponse | null>(() => {
    if (!title) return null
    const matches = (requestsQuery.data?.requests ?? []).filter(
      (r) => r.tmdb_id === title.tmdb_id && r.media_type === title.media_type,
    )
    const active = matches.find(
      (r) =>
        r.status !== 'available' &&
        r.status !== 'failed' &&
        r.status !== 'evicted' &&
        // ADR-0014: `cancelled` is settled too — a stale cancelled row must not shadow
        // a fresh active re-request for the same title (which would make the modal keep
        // targeting the dead cancelled id). Mirrors the backend `_SETTLED_REQUEST_STATUSES`.
        r.status !== 'cancelled',
    )
    return active ?? matches[matches.length - 1] ?? null
  }, [requestsQuery.data, title])

  // A just-created request shows immediately even before the next poll lands. Used
  // for preview + queue correlation (a terminal request still owns its old download).
  const effectiveRequestId = requestId ?? liveRequest?.id ?? null

  // The "keep forever" pin (ADR-0012): prefers the live (polled) request's own
  // field — the canonical source — EXCEPT right after "Request again"/"Request",
  // where `requestId` updates synchronously (`setRequestId(created.id)`) but
  // `liveRequest` can still resolve the OLD, now-SETTLED request until the next
  // `/requests` poll (or the create's own cache invalidation refetch) lands —
  // see `liveRequest`'s own docstring on that lag. Pinning during that transient
  // window must target the freshly created (active) request, never the stale
  // settled one it replaced: `pinTracksFreshRequest` is true exactly while
  // `requestId` is known but `liveRequest` hasn't caught up to it yet (including
  // `liveRequest === null`, the very-first-request case). A fresh request always
  // starts unpinned, so `keepForever` reads `false` there too, rather than the
  // OLD request's (possibly `true`) pin state under a checkbox that now targets
  // a DIFFERENT id. Once the poll catches up (`liveRequest.id === requestId`),
  // or `requestId` is `null` again (a fresh `titleKey`), this collapses back to
  // the ordinary `liveRequest`-driven pin. `null` pinRequestId means no request
  // exists at all yet: nothing to pin.
  const pinTracksFreshRequest = requestId !== null && liveRequest?.id !== requestId
  const pinRequestId = pinTracksFreshRequest ? requestId : (liveRequest?.id ?? requestId)
  const keepForever = pinTracksFreshRequest ? false : (liveRequest?.keep_forever ?? false)

  // tv only: this show's per-season rollup — the live poll once it lands, else the
  // just-created request's own list (the same create-then-poll gap as
  // `effectiveRequestId`). `null` for a movie, and for a tv title with no request
  // yet (nothing tracked to enumerate).
  const effectiveSeasons: SeasonStatus[] | null =
    title?.media_type === 'tv'
      ? requestId !== null && liveRequest === null
        ? createdSeasons
        : (liveRequest?.seasons ?? null)
      : null

  // The season driving search/grab/view. The operator's explicit pick wins as long
  // as it is still a tracked season (or none are known yet, pre-request); otherwise
  // the first actionable tracked season, else season 1 — the sane default before
  // any season is tracked at all. Always `null` for a movie.
  const currentSeason: number | null =
    title?.media_type !== 'tv' ? null : resolveSeason(effectiveSeasons, activeSeason)

  // Grabbing needs a NON-terminal request: the backend rejects a terminal request id
  // in /queue/grab (request_not_active). A just-created request that came back active,
  // or the live one while still active, qualifies; a failed/available/completed one
  // does not — for those the user must "Request again" (a fresh request) before grabbing.
  // For tv this is judged against the SELECTED season's own status, never the
  // show-level rollup (`seasonStatusFor`) — `currentSeason` is `null` for a movie,
  // so this is byte-identical to the movie-only check it replaces.
  const liveRequestGrabbable = liveRequest != null && isSeasonGrabbable(liveRequest, currentSeason)
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
  // to tmdb_id only when no request is known yet. For tv, a show can have MULTIPLE
  // concurrent per-season downloads (season 1 and season 2 grab independently), so
  // also match the currently-selected season — otherwise a sibling season's
  // download would shadow the one the operator is looking at. Movies never carry a
  // season, so this filter is a no-op for them (unchanged behaviour).
  const queueItem = useMemo<QueueItem | null>(() => {
    if (!title) return null
    const items = queueQuery.data?.queue ?? []
    const matches = items.filter((q) => {
      if (effectiveRequestId === null) return q.tmdb_id === title.tmdb_id
      if (q.media_request_id !== effectiveRequestId) return false
      return title.media_type === 'tv' ? queueItemCoversSeason(q, currentSeason) : true
    })
    return matches.length > 0 ? matches[matches.length - 1]! : null
  }, [queueQuery.data, title, effectiveRequestId, currentSeason])

  const state = deriveState(
    liveRequest ? seasonStatusFor(liveRequest, currentSeason) : null,
    requestId !== null,
  )
  const reportTarget = reportFor
    ? ((queueQuery.data?.queue ?? []).find((item) => item.id === reportFor.downloadId) ?? null)
    : null
  const reportActionable = reportTarget !== null && reportTarget.status !== 'importing'

  useEffect(() => {
    if (reportFor && !reportActionable) {
      setReportFor(null)
    }
  }, [reportFor, reportActionable])

  const runPreview = useCallback(
    // `seasonOverride` lets a caller preview a season that hasn't made it into
    // `currentSeason` yet — namely `onRequest`, which must search the season the
    // BRAND NEW create resolved to, not the one `currentSeason` still reads at
    // click time (see onRequest below). Omitted (not merely `null`), it falls back
    // to `currentSeason` — every other caller's behaviour is unchanged.
    async (forRequestId: number | null, seasonOverride?: number | null) => {
      if (!title) return
      const startedKey = `${title.media_type}:${title.tmdb_id}`
      const body: SearchPreviewRequest =
        forRequestId !== null
          ? { request_id: forRequestId }
          : { tmdb_id: title.tmdb_id, media_type: title.media_type, title: title.title }
      if (forRequestId === null && typeof title.year === 'number') {
        body.year = title.year
      }
      const season = seasonOverride !== undefined ? seasonOverride : currentSeason
      // tv only: search/grab is always per-season, so a concrete season is threaded
      // whenever one is known — `season` is `null` for a movie, so this is a
      // no-op there (the field stays entirely absent, same payload as before).
      if (title.media_type === 'tv' && season != null) {
        body.season = season
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
    [title, searchPreview, toast, currentSeason],
  )

  const onRequest = useCallback(async () => {
    if (!title) return
    const startedKey = `${title.media_type}:${title.tmdb_id}`
    const titleName = title.title
    try {
      const body: CreateRequestBody = { tmdb_id: title.tmdb_id, media_type: title.media_type }
      // tv only: naming a single season narrows CreateRequestBody.seasons; leaving
      // "whole series" checked (the default) omits it entirely, which the backend
      // reads as "track every aired season" (request_service._season_numbers).
      // Movies never set this field — identical payload to before.
      //
      // A title that ALREADY has tracked seasons hides the "whole series" checkbox
      // (the season PICKER drives selection instead), so `wholeSeries` is stale-true
      // there: a "Request again" for a failed S2 must scope to the SELECTED season,
      // not silently re-request the whole show. Only the pre-request flow (no tracked
      // seasons yet, checkbox visible) honours `wholeSeries`.
      if (title.media_type === 'tv') {
        const hasTrackedSeasons = (effectiveSeasons?.length ?? 0) > 0
        if (hasTrackedSeasons || !wholeSeries) {
          body.seasons = [currentSeason ?? 1]
        }
      }
      const created = await createRequest.mutateAsync(body)
      if (latestTitleKey.current !== startedKey) return // don't apply A's request to title B
      setRequestId(created.id)
      setCreatedSeasons(created.seasons ?? null)
      // tv only: resolve the season against `created.seasons` — the BRAND NEW list —
      // rather than `currentSeason`, which still reflects the season list from
      // BEFORE this request existed. For a whole-series request that's `null`
      // (nothing tracked yet), so `currentSeason` defaults to season 1; but
      // `created.seasons` can already have season 1 terminal (already in Plex) and
      // resolve to season 2. Judging the create against the wrong season would arm
      // Grab off S1's status while the selector (next render) shows S2, and would
      // preview S1's releases under an S2 selector — exactly the bug this guards.
      // Always `null` for a movie, byte-identical to the `currentSeason` it replaces.
      const resolvedSeason =
        title.media_type === 'tv' ? resolveSeason(created.seasons ?? null, activeSeason) : null
      // A terminal create (Plex already has the title, or a reused completed/failed
      // request) must not arm Grab — gate on the returned status, not just the id.
      // For tv this is the SELECTED (resolved) season's own status (`created.seasons`
      // is now populated), never the show-level rollup.
      const grabbable = isSeasonGrabbable(created, resolvedSeason)
      setCreatedGrabbable(grabbable)
      toast({ title: `Requested ${titleName}`, intent: 'success' })
      // Search-preview is admin-only server-side: a shared user's request ends
      // here (the auto-grab worker takes it from request to download unattended).
      if (grabbable && isAdmin) {
        await runPreview(created.id, resolvedSeason)
      } else {
        setPreview(null)
      }
    } catch (error) {
      if (latestTitleKey.current !== startedKey) return
      toast({ title: 'Request failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [title, createRequest, toast, runPreview, wholeSeries, currentSeason, activeSeason, effectiveSeasons, isAdmin])

  // Re-acquire (issue #131), movie-only: force-create a fresh grabbable request
  // even though Plex still reports the title present (its file was deleted or
  // replaced out-of-band). Modeled on `onRequest`, but force-flagged and
  // season-free -- a movie request never carries `seasons`.
  const onReacquire = useCallback(async () => {
    if (!title || title.media_type !== 'movie') return
    const startedKey = `${title.media_type}:${title.tmdb_id}`
    const titleName = title.title
    try {
      const created = await createRequest.mutateAsync({
        tmdb_id: title.tmdb_id,
        media_type: 'movie',
        force: true,
      })
      if (latestTitleKey.current !== startedKey) return
      setRequestId(created.id)
      setCreatedSeasons(created.seasons ?? null)
      const grabbable = isSeasonGrabbable(created, null)
      setCreatedGrabbable(grabbable)
      setReacquireOpen(false)
      toast({ title: `Re-acquiring ${titleName}`, intent: 'success' })
      if (grabbable && isAdmin) {
        await runPreview(created.id, null)
      } else {
        setPreview(null)
      }
    } catch (error) {
      if (latestTitleKey.current !== startedKey) return
      toast({ title: 'Re-acquire failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [title, createRequest, toast, runPreview, isAdmin])

  const onGrab = useCallback(
    async (release: AcceptedRelease) => {
      // Need a grabbable (non-terminal) request; never fire a second grab in flight.
      if (grabRequestId === null || grab.isPending) return
      // Send only the GUID — it uniquely identifies the clicked row. info_hash can
      // be shared across indexers and the backend matches it BEFORE guid, so
      // including it could grab a different release that shares the hash.
      const body: GrabRequest = { request_id: grabRequestId, guid: release.guid }
      // tv only: scope the grab (and the stored Download) to the selected season —
      // `currentSeason` is `null` for a movie, so this stays absent there.
      if (title?.media_type === 'tv' && currentSeason != null) {
        body.season = currentSeason
      }
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
    [grabRequestId, grab, toast, title, currentSeason],
  )

  // Blocklist the bad release and re-arm the request to search again. Mirrors the
  // Queue screen's mark-failed confirm; no separate "issues" record is created.
  const runReport = useCallback(async () => {
    if (!reportFor || !reportActionable) {
      setReportFor(null)
      return
    }
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
  }, [reportFor, reportActionable, markFailed, toast])

  // Report an IMPORTED/available title (ADR-0014): blocklist the release, purge its
  // torrent + library file, and re-search inline. Distinct from `runReport` above
  // (which marks an in-flight queue download failed) — there is no queue row here.
  const runReportIssue = useCallback(async () => {
    if (!reportIssueFor) return
    try {
      await reportIssue.mutateAsync({
        requestId: reportIssueFor.requestId,
        reason: reportReason,
        season: reportIssueFor.season,
      })
      toast({
        title: 'Reported',
        description: 'Blocklisted that release, removed the file, and re-searching.',
        intent: 'success',
      })
      setReportIssueFor(null)
    } catch (error) {
      toast({ title: 'Report failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [reportIssueFor, reportReason, reportIssue, toast])

  // Cancel a not-yet-imported request (ADR-0014): drop any active torrent(s) and
  // settle it to `cancelled`. The honest opposite of report-issue.
  const runCancel = useCallback(async () => {
    if (!cancelFor) return
    try {
      await cancelRequest.mutateAsync(cancelFor.requestId)
      toast({ title: 'Request cancelled', intent: 'success' })
      setCancelFor(null)
    } catch (error) {
      toast({ title: 'Cancel failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [cancelFor, cancelRequest, toast])

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

  // Toggle the "keep forever" pin (ADR-0012): pinned means `domain/eviction.py`
  // will never select this title (or, for a show, any of its seasons — the pin
  // lives on the parent request) regardless of watch state or disk pressure.
  const onToggleKeepForever = useCallback(async () => {
    if (pinRequestId == null) return
    const next = !keepForever
    try {
      await setKeepForever.mutateAsync({ requestId: pinRequestId, keepForever: next })
      if (next) {
        toast({
          title: 'Pinned — kept forever',
          description: 'The disk-pressure sweep will never touch this title.',
          intent: 'success',
        })
      } else {
        toast({ title: 'Unpinned', intent: 'success' })
      }
    } catch (error) {
      toast({ title: 'Could not update pin', description: asApiError(error).message, intent: 'error' })
    }
  }, [pinRequestId, keepForever, setKeepForever, toast])

  if (!title) return null

  const canGrab = grabRequestId !== null
  const meta = [title.year, title.media_type === 'tv' ? 'TV' : 'Movie'].filter(Boolean).join(' · ')

  // Owned but no visible request (issue #131): the title is present in Plex per
  // the discovery projection, yet there is no tracked request row at all -- Plex
  // hasn't rescanned since the operator deleted the file out-of-band. "Request"
  // would be misleading here (it just short-circuits back to an `available` row
  // with no grab); Re-acquire is the honest verb. Movie-only, since a tv title's
  // per-season presence is surfaced through its own tracked seasons instead.
  const presenceOnly = title.media_type === 'movie' && title.library_state === 'available'

  // tv only: before any request exists, let the operator name a single season (and
  // whether to track just it or the whole series); once seasons are tracked,
  // enumerate them in a picker that also drives which one is searched/grabbed/shown.
  const seasonSelector: ReactNode =
    title.media_type !== 'tv' ? null : effectiveSeasons && effectiveSeasons.length > 0 ? (
      <div className="mt-3 flex items-center gap-2">
        <label htmlFor="season-select" className="font-mono text-xs text-faint">
          Season
        </label>
        <select
          id="season-select"
          className="h-8 rounded-lg bg-bg px-2 text-xs text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
          value={currentSeason ?? ''}
          onChange={(e) => {
            setActiveSeason(Number(e.target.value))
            // Drop the previewed releases from the OLD season: they belong to a
            // different scope, and grabbing one now would send the newly selected
            // season and 404 (release_not_found) or grab under the wrong context.
            // The operator re-searches for the newly selected season.
            setPreview(null)
          }}
        >
          {effectiveSeasons.map((s) => (
            <option key={s.season_number} value={s.season_number}>
              Season {s.season_number} — {requestStatus(s.status).label}
            </option>
          ))}
        </select>
      </div>
    ) : state.kind === 'none' ? (
      <div className="mt-3 flex flex-wrap items-center gap-4">
        <label className="flex items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            checked={wholeSeries}
            onChange={(e) => setWholeSeries(e.target.checked)}
          />
          Whole series
        </label>
        <label className="flex items-center gap-2 text-xs text-muted">
          Season
          <input
            type="number"
            min={1}
            aria-label="Season to search"
            className="h-8 w-16 rounded-lg bg-bg px-2 text-xs text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
            value={currentSeason ?? 1}
            onChange={(e) =>
              setActiveSeason(Math.max(1, Number.parseInt(e.target.value, 10) || 1))
            }
          />
        </label>
      </div>
    ) : null

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
  // Every correction verb below is admin-only server-side (`require_admin`), so
  // each button is built only for admins — a shared user gets the request-only
  // experience (Request / Request again + honest status), never a 403 machine.
  const canReport = isAdmin && queueItem !== null && queueItem.status !== 'importing'
  const reportButton =
    canReport && queueItem ? (
      <Button variant="danger" onClick={() => setReportFor({ downloadId: queueItem.id })}>
        Report a problem
      </Button>
    ) : null

  const retryImportButton =
    isAdmin && queueItem ? (
      <Button onClick={() => void onRetryImport()} loading={importDownload.isPending}>
        Retry import
      </Button>
    ) : null

  // Report an IMPORTED/available title (ADR-0014). Distinct from `reportButton`
  // (queue mark-failed): there is no active download here, so it acts on the
  // request + selected season via the report-issue endpoint. Needs a known request.
  const reportIssueButton =
    isAdmin && effectiveRequestId !== null ? (
      <Button
        variant="danger"
        onClick={() =>
          setReportIssueFor({ requestId: effectiveRequestId, season: currentSeason })
        }
      >
        Report a problem
      </Button>
    ) : null

  // Re-acquire (issue #131), movie-only: the title reads present in Plex but its
  // file was deleted/replaced out-of-band. Opens a confirm dialog, then
  // force-creates a fresh grabbable request (see `onReacquire`). NOT admin-gated:
  // POST /requests (force included) is at the same authZ bar as any create.
  const reacquireButton =
    title.media_type === 'movie' ? (
      <Button variant="secondary" onClick={() => setReacquireOpen(true)}>
        Re-acquire
      </Button>
    ) : null

  // Cancel the whole request (ADR-0014) — only while it is genuinely not-yet-imported.
  // Gated on the PARENT request status (for tv the rollup), so a partially_available
  // show never offers a wholesale cancel that the backend would 409.
  //
  // The parent rollup alone is NOT sufficient for tv: season_rollup precedence lets an
  // in-flight season outrank an already-DONE sibling, so a show with S1 `available` and
  // S2 `downloading` rolls up to the cancellable `downloading` even though the backend
  // `cancel_request()` deterministically refuses (not_cancellable) because S1 is
  // imported. Mirror that per-season guard here so we never offer a button that 409s.
  const anySeasonImported = (liveRequest?.seasons ?? []).some(
    (s) => s.status === 'available' || s.status === 'completed',
  )
  const canCancel =
    isAdmin &&
    liveRequest != null &&
    CANCELLABLE_STATUSES.has(liveRequest.status) &&
    !anySeasonImported
  const cancelButton =
    canCancel && liveRequest ? (
      <Button variant="secondary" onClick={() => setCancelFor({ requestId: liveRequest.id })}>
        Cancel request
      </Button>
    ) : null

  const reSearchButton = isAdmin ? (
    <Button
      variant="secondary"
      onClick={() => void runPreview(effectiveRequestId)}
      loading={searchPreview.isPending}
    >
      Re-search
    </Button>
  ) : null

  // States where browsing/grabbing releases is part of the action — the decision
  // engine output stays visible (especially the honest no-acceptable-release).
  // Admin-only: search-preview and grab are `require_admin` routes, so shared
  // users never see the release browser at all.
  const showReleases =
    isAdmin &&
    (state.kind === 'none' ||
      state.kind === 'pending' ||
      state.kind === 'searching' ||
      state.kind === 'no_acceptable_release' ||
      state.kind === 'failed' ||
      state.kind === 'unknown')

  let actionZone: ReactNode
  switch (state.kind) {
    case 'none':
      actionZone = (
        <div className="flex flex-wrap gap-2">
          {/* Owned movie with no tracked request (issue #131): "Request" would just
              short-circuit back to a terminal `available` row with no grab, so the
              honest verb here is Re-acquire (force-create). */}
          {presenceOnly ? (
            reacquireButton
          ) : (
            <Button onClick={() => void onRequest()} loading={createRequest.isPending}>
              Request
            </Button>
          )}
          {/* Preview drives admin-only /search-preview: request-only for shared users. */}
          {isAdmin ? (
            <Button
              variant="secondary"
              onClick={() => void runPreview(null)}
              loading={searchPreview.isPending}
            >
              Preview releases
            </Button>
          ) : null}
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
          {cancelButton}
        </div>
      )
      break
    case 'downloading':
      actionZone = (
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <StatusBadge status={requestStatus('downloading')} />
            {/* Progress comes from admin-only GET /queue (disabled for shared
                sessions), so a shared user gets the honest badge, never a
                fabricated stuck-at-0% bar. */}
            {isAdmin ? (
              <div className="flex flex-1 items-center gap-3">
                <ProgressBar value={queueItem?.progress ?? 0} label="Download progress" />
                <span className="font-mono text-xs text-muted tabular-nums">
                  {Math.round(Math.min(1, Math.max(0, queueItem?.progress ?? 0)) * 100)}%
                </span>
              </div>
            ) : null}
          </div>
          {reportButton || cancelButton ? (
            <div className="flex flex-wrap gap-2">
              {reportButton}
              {cancelButton}
            </div>
          ) : null}
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
          {cancelButton}
        </div>
      )
      break
    case 'waiting_for_air_date':
      actionZone = (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={requestStatus('waiting_for_air_date')} />
            <span className="text-sm text-muted">
              This season has not aired yet. Cancel the request if you no longer want it.
            </span>
          </div>
          <div className="flex flex-wrap gap-2">{cancelButton}</div>
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
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={FINALIZING} />
            <span className="text-sm text-muted">Imported — awaiting Plex confirmation.</span>
          </div>
          {/* Imported (finalizing): report-issue can already redo it (ADR-0014). */}
          {reportIssueButton ? (
            <div className="flex flex-wrap gap-2">{reportIssueButton}</div>
          ) : null}
        </div>
      )
      break
    case 'available':
      // In the library. The download is terminal (gone from the active queue), so
      // there is no mark-failed target — instead report-issue-with-purge (ADR-0014)
      // blocklists the release, deletes it from Plex/disk, and re-searches inline.
      actionZone = (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <span className="inline-flex items-center gap-1.5 rounded-lg bg-available/15 px-3 py-1 text-sm font-semibold text-available ring-1 ring-available/30">
              ✓ In your library
            </span>
          </div>
          {/* Re-acquire (issue #131) sits beside report-issue for a movie: a shared
              user sees only Re-acquire (report-issue is admin-only), an admin sees
              both; tv shows only report-issue (reacquireButton is null for tv —
              per-season re-acquisition is the report-issue verb's job). */}
          {reportIssueButton || reacquireButton ? (
            <div className="flex flex-wrap gap-2">
              {reacquireButton}
              {reportIssueButton}
            </div>
          ) : null}
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
    case 'evicted':
      // ADR-0012: honest, retryable — the disk-pressure sweep freed this title's
      // file on purpose (not a failure), and re-requesting grabs it again from
      // scratch (the old id is settled, same as available/failed).
      actionZone = (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={requestStatus('evicted')} />
            <span className="text-sm text-muted">
              Freed to relieve disk pressure. Request it again to re-grab it.
            </span>
          </div>
          <div className="flex flex-wrap gap-2">{requestAgainButton}</div>
        </div>
      )
      break
    case 'cancelled':
      // ADR-0014: the operator cancelled this (not-yet-imported) request. Settled,
      // same as evicted/available/failed — "Request again" makes a fresh, grabbable
      // request (the old id is settled).
      actionZone = (
        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <StatusBadge status={requestStatus('cancelled')} />
            <span className="text-sm text-muted">Cancelled. Request it again to restart.</span>
          </div>
          <div className="flex flex-wrap gap-2">{requestAgainButton}</div>
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
            {seasonSelector}
            {/* Keep-forever is an admin-only endpoint: hidden for shared users. */}
            {isAdmin && pinRequestId != null ? (
              <label className="mt-3 flex items-center gap-2 text-xs text-muted">
                <input
                  type="checkbox"
                  checked={keepForever}
                  disabled={setKeepForever.isPending}
                  onChange={() => void onToggleKeepForever()}
                />
                Keep forever (never auto-evicted)
              </label>
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

      {reportFor && reportActionable ? (
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

      {reportIssueFor ? (
        <Dialog
          open
          onOpenChange={(next) => {
            if (!next) setReportIssueFor(null)
          }}
          title="Report a problem with this title?"
          description="Blocklist this release, delete the file, and search again for a different one. This can't be undone, but re-searching brings the content back."
        >
          <div className="flex flex-col gap-4">
            <label className="flex flex-col gap-1.5 text-sm text-muted">
              What's wrong?
              <select
                aria-label="Reason"
                className="h-9 rounded-lg bg-bg px-2 text-sm text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
                value={reportReason}
                onChange={(e) => setReportReason(e.target.value as ReportReason)}
              >
                <option value="bad_quality">Bad quality</option>
                <option value="wrong_media">Wrong movie/episode</option>
                <option value="user_reported">Something else</option>
              </select>
            </label>
            <div className="flex justify-end gap-3">
              <Button
                variant="secondary"
                onClick={() => setReportIssueFor(null)}
                disabled={reportIssue.isPending}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                loading={reportIssue.isPending}
                onClick={() => void runReportIssue()}
              >
                Blocklist &amp; redo
              </Button>
            </div>
          </div>
        </Dialog>
      ) : null}

      {cancelFor ? (
        <Dialog
          open
          onOpenChange={(next) => {
            if (!next) setCancelFor(null)
          }}
          title="Cancel this request?"
          description="Stops any active download and removes it. You can request it again later."
        >
          <div className="flex justify-end gap-3">
            <Button
              variant="secondary"
              onClick={() => setCancelFor(null)}
              disabled={cancelRequest.isPending}
            >
              Keep it
            </Button>
            <Button variant="danger" loading={cancelRequest.isPending} onClick={() => void runCancel()}>
              Cancel request
            </Button>
          </div>
        </Dialog>
      ) : null}

      {/* Re-acquire confirm (issue #131): the only guard against the accepted
          duplicate-on-wrong-assertion tradeoff — the operator asserts the file is
          really gone before we force a fresh grab. */}
      {reacquireOpen ? (
        <Dialog
          open
          onOpenChange={(next) => {
            if (!next) setReacquireOpen(false)
          }}
          title="Re-acquire this title?"
          description="Re-download this title because its file is missing or was replaced. This makes a fresh request and searches for it again."
        >
          <div className="flex justify-end gap-3">
            <Button
              variant="secondary"
              onClick={() => setReacquireOpen(false)}
              disabled={createRequest.isPending}
            >
              Cancel
            </Button>
            <Button loading={createRequest.isPending} onClick={() => void onReacquire()}>
              Re-acquire
            </Button>
          </div>
        </Dialog>
      ) : null}
    </Dialog>
  )
}
