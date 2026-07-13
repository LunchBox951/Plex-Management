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
  RequestStatusValue,
  SeasonStatus,
} from '../api/types'
import type { ApiError } from '../lib/errors'
import { PLEX_WEB_APP_URL } from '../lib/plex'
import { isMarkFailableStatus, requestStatus, type StatusPresentation } from '../lib/status'
import { Dialog, DialogClose, DialogTitle } from './ui/Dialog'
import { ReleaseList } from './ReleaseList'
import { Button } from './ui/Button'
import { buttonClasses } from './ui/button-variants'
import { StatusBadge } from './ui/StatusBadge'
import { ProgressBar } from './ui/ProgressBar'
import { CenteredSpinner } from './ui/feedback'
import { useToast } from './ui/toast'
import { useTitleReleasePreview } from './useTitleReleasePreview'

export interface TitleDetailModalAction {
  kind: 're-search'
  requestId: number
  /**
   * The TV season the shortcut targets, resolved by the CALLER from the fresh
   * request row it was clicked on (`null` for a movie). The action effect must
   * not fall back to the modal's own `currentSeason`: when one long-mounted
   * modal instance is reused across titles, the title-reset effect has not yet
   * applied in the render this action first fires in, so `currentSeason` can
   * still read a season the operator picked on a DIFFERENT title.
   */
  season: number | null
  token: number
}

interface TitleDetailModalProps {
  title: DiscoverResult | null
  open: boolean
  onOpenChange: (open: boolean) => void
  returnFocusTo?: HTMLElement | null | (() => HTMLElement | null)
  action?: TitleDetailModalAction | null
  /**
   * Pin the modal's request correlation to ONE specific row. The Requests list
   * passes the clicked row's id: an admin's list legitimately shows two
   * different users' rows for the same title (the display fold keys on user_id
   * too), and the modal's own `(tmdb_id, media_type)` correlation would
   * otherwise resolve `liveRequest` — and with it the preview/grab/report/
   * cancel/pin targets — to the FIRST matching row, which can be a DIFFERENT
   * user's request than the one that was clicked. Omitted (Discover), the
   * correlation behaves exactly as before.
   *
   * A fixed prop, not itself reactive: it never changes for the lifetime of one
   * clicked-open modal. When the clicked row is SETTLED (failed/evicted/
   * cancelled) and the operator fires "Request again"/"Re-acquire", the modal
   * internally rebinds past this prop to the freshly created row (issue #272) —
   * see `reboundRequestId` — rather than staying pinned to the dead row this
   * prop still names.
   */
  boundRequestId?: number | null
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

/**
 * The first non-terminal tracked season, else the first tracked season.
 *
 * A `failed` season counts as actionable too when ANOTHER season in the list
 * is `completed` ("Finalizing"): that combination only ever arises from
 * issue #265's rollup precedence (a finalizing season always wins the parent
 * fold outright), the exact case `isSeasonGrabbable` below carves out for its
 * failed-season retry action. Without this, the plain `isGrabbableStatus`
 * check sees neither `completed` nor `failed` as actionable and defaults the
 * picker to the finalizing season instead of the one that actually needs a
 * retry.
 */
function firstActionableSeason(seasons: SeasonStatus[] | null): number | null {
  if (!seasons || seasons.length === 0) return null
  const hasFinalizingSibling = seasons.some((s) => s.status === 'completed')
  const active = seasons.find(
    (s) => isGrabbableStatus(s.status) || (hasFinalizingSibling && s.status === 'failed'),
  )
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
 * terminal request id in /queue/grab (`request_not_active`). Positive allowlist
 * (issue #205), not a terminal denylist — an unrecognized status (a future
 * backend enum member this bundle predates, or corrupt/legacy data) is absent
 * from the set and therefore fails CLOSED (not grabbable), rather than a
 * denylist's fail-OPEN default. Mirrors the backend's non-terminal statuses:
 * everything in `RequestStatus` except `available` (already in the library),
 * `completed` (imported, finalizing), `failed`, `evicted` (ADR-0012 — the
 * disk-pressure sweep already deleted the file; re-grabbing the same id would
 * just re-arm a row the backend now treats as settled, see
 * `_SETTLED_REQUEST_STATUSES`), `waiting_for_air_date`, and `cancelled`
 * (ADR-0014 — settled and terminal for grab; a fresh "Request again" must be
 * made before grabbing). `partially_available` is included even though it is a
 * tv-only rollup NEVER set on a single season (a harmless superset — see
 * `seasonStatusFor`) because it is genuinely non-terminal at the request level.
 */
const GRABBABLE_STATUSES = new Set<RequestStatusValue>([
  'pending',
  'searching',
  'no_acceptable_release',
  'downloading',
  'import_blocked',
  'partially_available',
])

function isGrabbableStatus(status: string): boolean {
  return GRABBABLE_STATUSES.has(status as RequestStatusValue)
}

/**
 * Whether the Grab button should be live for the SELECTED season (or, for a movie,
 * the request itself). A season is grabbable when its own status is non-terminal —
 * OR when it is `failed` but the PARENT request is still non-terminal (or reads
 * `completed` only because a DIFFERENT season is finalizing, see below). The
 * backend gates /queue/grab on the parent, so a failed season under an active
 * (e.g. `partially_available`) show can be re-searched from the UI; without this
 * it would dead-end into "Request again", which dedups straight back to the same
 * failed season (an active show is never re-created), leaving the user unable to
 * retry it. A movie (`season == null` → `seasonStatusFor` returns `request.status`)
 * never enters the failed branch: its failed status equals the parent's terminal one.
 *
 * `request.status === 'completed'` is included alongside `isGrabbableStatus`
 * (issue #287 senior review, #265/#272 follow-up): a `completed` PARENT rollup
 * can ONLY arise because a DIFFERENT season is still finalizing (season_rollup's
 * precedence always lets `completed` win the fold outright) — a genuinely,
 * wholly-settled show never reads `completed` at the parent level (that fold
 * only happens per-season). So THIS season being `failed` while the parent is
 * `completed` still means a sibling is finalizing, never that every season
 * (this one included) is done; the backend's matching carve-out
 * (`grab_service.grab`) accepts exactly this shape. Every OTHER terminal parent
 * value (`available`/`failed`/`evicted`/`cancelled`) is reached only when EVERY
 * season already settled that way, so those correctly stay excluded.
 */
function isSeasonGrabbable(request: RequestResponse, season: number | null): boolean {
  const seasonStatus = seasonStatusFor(request, season)
  if (isGrabbableStatus(seasonStatus)) return true
  return (
    season != null &&
    seasonStatus === 'failed' &&
    (isGrabbableStatus(request.status) || request.status === 'completed')
  )
}

const FINALIZING: StatusPresentation = { label: 'Finalizing', intent: 'downloading' }
const IMPORT_BLOCKED: StatusPresentation = { label: 'Import blocked', intent: 'error' }
const NOT_REQUESTED: StatusPresentation = { label: 'Not requested', intent: 'neutral' }

function statePresentation(state: DerivedState): StatusPresentation {
  switch (state.kind) {
    case 'none':
      return NOT_REQUESTED
    case 'import_blocked':
      return IMPORT_BLOCKED
    case 'completed':
      return FINALIZING
    case 'unknown':
      return requestStatus(state.status)
    default:
      return requestStatus(state.kind)
  }
}

/** Plain-language copy over the already-derived lifecycle; no new state fold. */
function stateSentence(
  state: DerivedState,
  mediaType: DiscoverResult['media_type'],
  libraryState: DiscoverResult['library_state'],
  currentSeason: number | null,
  queueItem: QueueItem | null,
): string {
  switch (state.kind) {
    case 'none':
      // Presence without a tracked request (issue #131): the discovery
      // projection says Plex owns this title even though no request row exists
      // (added out-of-band, or rows pruned). Never claim it is "not in the
      // library" — for a movie the actions zone simultaneously offers
      // Re-acquire BECAUSE it is owned, and the copy must agree with it.
      if (libraryState === 'available' || libraryState === 'partially_available') {
        const presence =
          libraryState === 'partially_available' ? 'Partly in the library' : 'In the library'
        return mediaType === 'movie'
          ? `${presence}, but not tracked by a request. Re-acquire it if its file is missing or was replaced.`
          : `${presence}, but not tracked by a request.`
      }
      return 'Not in the library and not requested.'
    case 'pending':
      return 'Your request is queued and will be searched automatically.'
    case 'searching':
      return 'Scanning configured indexers for an acceptable release.'
    case 'downloading':
      return mediaType === 'tv'
        ? `Season ${currentSeason ?? 1} is downloading.`
        : 'A release was grabbed and is transferring.'
    case 'no_acceptable_release':
      return 'No acceptable release was found. Nothing was grabbed; automatic retries will continue.'
    case 'waiting_for_air_date':
      return "This season hasn't aired yet. It will be searched automatically after its air date."
    case 'import_blocked':
      return queueItem?.failed_reason
        ? `The download finished, but import is blocked: ${queueItem.failed_reason}`
        : 'The download finished, but import needs operator attention.'
    case 'completed':
      return 'Imported and awaiting Plex confirmation.'
    case 'available':
      return mediaType === 'tv'
        ? 'This season is imported and visible in Plex.'
        : 'This title is imported and visible in Plex.'
    case 'failed':
      return queueItem?.failed_reason
        ? `The request failed: ${queueItem.failed_reason}`
        : 'The request failed. Request it again to restart.'
    case 'evicted':
      return 'The disk-pressure sweep reclaimed this file. Deliberate space management — request again any time.'
    case 'cancelled':
      return 'This request was cancelled. Request it again any time.'
    case 'unknown':
      return `Plex Manager reported “${requestStatus(state.status).label}”; no additional detail is available.`
  }
}

/**
 * The headline flow: request a title, run the decision engine (search-preview),
 * and grab a ranked release into the download client. Beyond the first grab, the
 * modal derives the title's live state from the polled request + queue and offers
 * a state-aware action zone — including an in-modal "report a problem" correction
 * that blocklists the bad release and re-arms the request (north star #1).
 */
export function TitleDetailModal({
  title,
  open,
  onOpenChange,
  returnFocusTo,
  action,
  boundRequestId,
}: TitleDetailModalProps) {
  const { toast } = useToast()
  // Shared (non-admin) sessions get a REQUEST-ONLY modal: the preview / grab /
  // correction / keep-forever verbs all sit behind `require_admin` server-side,
  // so exposing them would only manufacture 403 `admin_required` failures.
  // Defaults to the restricted view until /auth/me resolves (fail closed) —
  // mirrors AdminGate's read of the same cached query.
  const auth = useAuthMe()
  const isAdmin = auth.data?.is_admin ?? auth.data?.user?.is_admin ?? false
  const createRequest = useCreateRequest()
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
  // issue #272: overrides the caller-supplied `boundRequestId` once a mutation
  // response tells us the row it should ACTUALLY be pinned to now. `boundRequestId`
  // is a plain prop, fixed at the moment the caller clicked a row — it never
  // updates on its own, so without this a "Request again" fired on a SETTLED
  // bound row (failed/evicted/cancelled) would create a fresh, active request yet
  // leave `liveRequest` (see below) resolving to the dead old row forever, even
  // after a LATER poll brings the fresh one back too (a literal id match always
  // wins there). Set directly from `created.id` in `onRequest`/`onReacquire` —
  // the mutation response IS the freshest possible signal for what this exact
  // modal instance should now be bound to — and reset alongside the rest of the
  // per-title state below so a newly opened title starts unbound again.
  const [reboundRequestId, setReboundRequestId] = useState<number | null>(null)
  // issue #287 senior review: `reboundRequestId` above must not outlive the
  // click that made it stale. The modal can stay MOUNTED across a close (issue
  // #271, separate/still-open) with `titleKey` unchanged, so the effect below
  // (keyed on `titleKey`) never fires between two clicks on the SAME title's
  // DIFFERENT duplicate rows -- e.g. rebind from "Request again" on row A (sets
  // `reboundRequestId` to the fresh row), then the operator opens row C (a
  // different existing row for the same title, possibly a different user's).
  // Without this, `liveRequest` above keeps preferring the stale rebind over
  // the just-clicked `boundRequestId`, misdirecting preview/grab/cancel/pin at
  // the WRONG row. Tracks the previous PROP value (not the resolved
  // `effectiveBoundRequestId`) so setting `reboundRequestId` ourselves from a
  // mutation response never trips this — only a genuinely new `boundRequestId`
  // supplied by the caller (a new row click) does.
  const previousBoundRequestId = useRef(boundRequestId)
  useEffect(() => {
    if (boundRequestId !== previousBoundRequestId.current) {
      previousBoundRequestId.current = boundRequestId
      setReboundRequestId(null)
    }
  }, [boundRequestId])
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
  const [backdropFailed, setBackdropFailed] = useState(false)
  const [posterFailed, setPosterFailed] = useState(false)

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
    setReboundRequestId(null)
    setCreatedGrabbable(false)
    setCreatedSeasons(null)
    setWholeSeries(true)
    setActiveSeason(null)
    setGrabbingGuid(null)
    setReportFor(null)
    setReportIssueFor(null)
    setReportReason('bad_quality')
    setCancelFor(null)
    setReacquireOpen(false)
    setBackdropFailed(false)
    setPosterFailed(false)
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
    // A caller-pinned row wins outright — the operator clicked THAT row, so its
    // state (even a settled one) is what the modal must present and act on. The
    // title-match filter above still applies: a bound id that no longer matches
    // this title (or vanished from the poll) falls through to normal resolution
    // rather than binding the modal to a foreign title's request. `reboundRequestId`
    // (issue #272) takes priority over the raw prop: once a "Request again"/
    // "Re-acquire" mutation has told us the fresh row's id, THAT is the row the
    // operator's last action actually targeted, so it must win over the original
    // (now-settled) clicked row once the poll brings it into `matches` too.
    const effectiveBoundRequestId = reboundRequestId ?? boundRequestId
    if (effectiveBoundRequestId != null) {
      const bound = matches.find((r) => r.id === effectiveBoundRequestId)
      if (bound) return bound
    }
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
  }, [requestsQuery.data, title, boundRequestId, reboundRequestId])

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
    // A disabled query may still expose data left in the shared React Query cache
    // by a previous administrator session. Treat /queue as unavailable unless the
    // CURRENT caller is an admin so a role/account transition cannot leak progress,
    // release failure reasons, or actionable download ids to a shared user.
    if (!title || !isAdmin) return null
    const items = queueQuery.data?.queue ?? []
    const matches = items.filter((q) => {
      if (effectiveRequestId === null) return q.tmdb_id === title.tmdb_id
      if (q.media_request_id !== effectiveRequestId) return false
      return title.media_type === 'tv' ? queueItemCoversSeason(q, currentSeason) : true
    })
    return matches.length > 0 ? matches[matches.length - 1]! : null
  }, [queueQuery.data, title, effectiveRequestId, currentSeason, isAdmin])

  const state = deriveState(
    liveRequest ? seasonStatusFor(liveRequest, currentSeason) : null,
    requestId !== null,
  )
  const reportTarget = reportFor
    ? ((queueQuery.data?.queue ?? []).find((item) => item.id === reportFor.downloadId) ?? null)
    : null
  // "Report a problem" drives the identical `mark_failed` mutation Queue.tsx's
  // Mark failed/Blocklist buttons do (see `runReport` below), so it is gated on
  // the SAME positive allowlist (`isMarkFailableStatus`, issue #205) rather than
  // a denylist -- an unrecognized queue-item status (a future backend state
  // this bundle predates, or corrupt/legacy data) is absent from the allowlist
  // and therefore fails CLOSED instead of exposing a control that would just
  // 409 (or worse, silently no-op on a state the backend never expected).
  const reportActionable = reportTarget !== null && isMarkFailableStatus(reportTarget.status)

  useEffect(() => {
    if (reportFor && !reportActionable) {
      setReportFor(null)
    }
  }, [reportFor, reportActionable])

  const { preview, isPending: previewPending, clearPreview, runPreview } =
    useTitleReleasePreview(title, currentSeason)

  // A route-level shortcut opens this same modal and asks it to run the same
  // preview path once. Mark the token consumed BEFORE starting the async call so
  // rerenders, changing mutation identities, and StrictMode's repeated effect
  // setup cannot issue a duplicate search. Fail closed until admin auth resolves.
  const consumedActionToken = useRef<number | null>(null)
  useEffect(() => {
    if (
      !open ||
      !isAdmin ||
      action?.kind !== 're-search' ||
      consumedActionToken.current === action.token
    ) {
      return
    }
    consumedActionToken.current = action.token
    // The season comes from the action itself (resolved by the caller from the
    // clicked request row), passed as an explicit override: `runPreview`'s
    // `currentSeason` fallback closes over pre-reset state in this effect pass
    // (see TitleDetailModalAction.season). `null` explicitly omits the season
    // (movie, or a defensive tv fallback -> request-scoped series preview).
    void runPreview(action.requestId, action.season)
  }, [action, isAdmin, open, runPreview])

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
      // issue #272: this exact click just produced (or dedup-resolved to) the row
      // the modal must now track — rebind `liveRequest`'s pin to it so a
      // "Request again" fired on a settled `boundRequestId` row never keeps
      // resolving to that dead row once the poll brings the fresh one back too.
      setReboundRequestId(created.id)
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
        clearPreview()
      }
    } catch (error) {
      if (latestTitleKey.current !== startedKey) return
      toast({ title: 'Request failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [title, createRequest, toast, runPreview, wholeSeries, currentSeason, activeSeason, effectiveSeasons, isAdmin, clearPreview])

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
      // issue #272: same rebind as `onRequest` above — Re-acquire is the movie-only
      // force-create equivalent of "Request again".
      setReboundRequestId(created.id)
      setCreatedSeasons(created.seasons ?? null)
      const grabbable = isSeasonGrabbable(created, null)
      setCreatedGrabbable(grabbable)
      setReacquireOpen(false)
      toast({ title: `Re-acquiring ${titleName}`, intent: 'success' })
      if (grabbable && isAdmin) {
        await runPreview(created.id, null)
      } else {
        clearPreview()
      }
    } catch (error) {
      if (latestTitleKey.current !== startedKey) return
      toast({ title: 'Re-acquire failed', description: asApiError(error).message, intent: 'error' })
    }
  }, [title, createRequest, toast, runPreview, isAdmin, clearPreview])

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
  const showBackdrop = Boolean(title.backdrop_url) && !backdropFailed
  const showPoster = Boolean(title.poster_url) && !posterFailed

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
      <div className="flex min-w-0 items-center gap-2 max-sm:w-full">
        <label htmlFor="season-select" className="font-mono text-xs text-faint">
          Season
        </label>
        <select
          id="season-select"
          className="h-8 min-w-0 max-w-full rounded-lg bg-bg px-2 text-xs text-ink ring-1 ring-inset ring-white/10 outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
          value={currentSeason ?? ''}
          onChange={(e) => {
            setActiveSeason(Number(e.target.value))
            // Drop the previewed releases from the OLD season: they belong to a
            // different scope, and grabbing one now would send the newly selected
            // season and 404 (release_not_found) or grab under the wrong context.
            // The operator re-searches for the newly selected season.
            clearPreview()
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
      <div className="flex flex-wrap items-center gap-4 max-sm:w-full">
        <label className="flex items-center gap-2 text-xs text-muted">
          <input
            type="checkbox"
            className="size-4 accent-gold outline-none focus-visible:ring-2 focus-visible:ring-gold/60"
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
  // AND when mark-failed is a legal move — `isMarkFailableStatus` (issue #205),
  // the SAME positive allowlist Queue.tsx's own Mark failed/Blocklist buttons
  // use, since this button drives the identical `mark_failed` mutation. During
  // the import copy/scan window the download sits in 'importing' (raw
  // DownloadState) while its owning request still reads 'downloading'; the
  // state machine only allows Importing -> Imported/ImportBlocked, so a
  // mark-failed there always 409s (invalid_state_transition) — 'importing' is
  // correctly absent from the allowlist. Don't offer an action that can't
  // succeed — once the import lands the title re-renders as completed or
  // import_blocked, where the correction paths reappear. An unrecognized
  // status (future backend state, or corrupt/legacy data) is likewise absent
  // from the allowlist and fails CLOSED, not just 'importing'. Every
  // correction verb below is admin-only server-side (`require_admin`), so each
  // button is built only for admins — a shared user gets the request-only
  // experience (Request / Request again + honest status), never a 403 machine.
  const canReport = isAdmin && queueItem !== null && isMarkFailableStatus(queueItem.status)
  const reportButton =
    canReport && queueItem ? (
      <Button variant="secondary" onClick={() => setReportFor({ downloadId: queueItem.id })}>
        Report a problem
      </Button>
    ) : null

  const retryImportButton =
    isAdmin && queueItem ? (
      <Button
        variant="secondary"
        onClick={() => void onRetryImport()}
        loading={importDownload.isPending}
      >
        Retry import
      </Button>
    ) : null

  // Report an IMPORTED/available title (ADR-0014). Distinct from `reportButton`
  // (queue mark-failed): there is no active download here, so it acts on the
  // request + selected season via the report-issue endpoint. Needs a known request.
  const reportIssueButton =
    isAdmin && effectiveRequestId !== null ? (
      <Button
        variant="secondary"
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
  const reacquirePrimary =
    title.media_type === 'movie' ? (
      <Button onClick={() => setReacquireOpen(true)}>
        Re-acquire
      </Button>
    ) : null
  const reacquireQuiet =
    title.media_type === 'movie' ? (
      <Button variant="ghost" onClick={() => setReacquireOpen(true)}>
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
      <Button variant="danger" onClick={() => setCancelFor({ requestId: liveRequest.id })}>
        Cancel request
      </Button>
    ) : null

  const reSearchNowButton = isAdmin ? (
    <Button
      onClick={() => void runPreview(effectiveRequestId)}
      loading={previewPending}
    >
      Re-search now
    </Button>
  ) : null

  // States where browsing/grabbing releases is part of the action — the decision
  // engine output stays visible (especially the honest no-acceptable-release).
  // Admin-only: search-preview and grab are `require_admin` routes, so shared
  // users never see the release browser at all. `unknown` is deliberately
  // EXCLUDED (issue #205, fail closed): a runtime-unknown status must not open
  // the release browser or expose Grab/Re-search, even though the header badge
  // and state sentence still render it honestly.
  const showReleaseSearch =
    isAdmin &&
    (state.kind === 'none' ||
      state.kind === 'pending' ||
      state.kind === 'searching' ||
      state.kind === 'no_acceptable_release' ||
      state.kind === 'failed')

  let actionZone: ReactNode
  switch (state.kind) {
    case 'none':
      actionZone = (
        <>
          {/* Owned movie with no tracked request (issue #131): "Request" would just
              short-circuit back to a terminal `available` row with no grab, so the
              honest verb here is Re-acquire (force-create). */}
          {presenceOnly ? (
            reacquirePrimary
          ) : (
            <Button onClick={() => void onRequest()} loading={createRequest.isPending}>
              + Request
            </Button>
          )}
        </>
      )
      break
    case 'pending':
    case 'searching':
      actionZone = cancelButton
      break
    case 'downloading':
      actionZone =
        reportButton || cancelButton ? (
          <>
            {reportButton}
            {cancelButton}
          </>
        ) : null
      break
    case 'no_acceptable_release':
      actionZone =
        reSearchNowButton || cancelButton ? (
          <>
            {reSearchNowButton}
            {cancelButton}
          </>
        ) : null
      break
    case 'waiting_for_air_date':
      actionZone = cancelButton
      break
    case 'import_blocked':
      actionZone = reportButton
      break
    case 'completed':
      actionZone = reportIssueButton
      break
    case 'available':
      // In the library. The download is terminal (gone from the active queue), so
      // there is no mark-failed target — instead report-issue-with-purge (ADR-0014)
      // blocklists the release, deletes it from Plex/disk, and re-searches inline.
      actionZone = (
        <>
          <a
            href={PLEX_WEB_APP_URL}
            target="_blank"
            rel="noopener noreferrer"
            className={buttonClasses()}
          >
            Open in Plex ↗<span className="sr-only"> opens in a new tab</span>
          </a>
          {/* Re-acquire (issue #131) sits beside report-issue for a movie: a shared
              user sees only Re-acquire (report-issue is admin-only), an admin sees
              both; tv shows only report-issue (reacquireQuiet is null for tv —
              per-season re-acquisition is the report-issue verb's job). */}
          {reportIssueButton}
          {reacquireQuiet}
        </>
      )
      break
    case 'failed':
      actionZone = (
        <>
          {/* The prior request is terminal — "Request again" makes a fresh, grabbable
              one (re-searching the dead id would show releases that all fail to grab). */}
          {requestAgainButton}
          {reportButton}
        </>
      )
      break
    case 'evicted':
      // ADR-0012: honest, retryable — the disk-pressure sweep freed this title's
      // file on purpose (not a failure), and re-requesting grabs it again from
      // scratch (the old id is settled, same as available/failed).
      actionZone = requestAgainButton
      break
    case 'cancelled':
      // ADR-0014: the operator cancelled this (not-yet-imported) request. Settled,
      // same as evicted/available/failed — "Request again" makes a fresh, grabbable
      // request (the old id is settled).
      actionZone = requestAgainButton
      break
    case 'unknown':
      // issue #205: a runtime-unknown status (a future backend enum member this
      // bundle predates, or corrupt/legacy data) gets NO actions — no Grab, no
      // Re-search, no release browser. Fail closed on the action, never on the
      // display: the header StatusBadge and state sentence already render it
      // honestly via `statePresentation`/`stateSentence`.
      actionZone = null
      break
  }

  const progressPercent =
    state.kind === 'downloading' &&
    isAdmin &&
    !queueQuery.isLoading &&
    !queueQuery.isError &&
    queueItem
      ? Math.round(Math.min(1, Math.max(0, queueItem.progress)) * 100)
      : null
  const progressLabel = `Download progress for ${title.title}${
    title.media_type === 'tv' ? `, season ${currentSeason ?? 1}` : ''
  }`
  const statusCopy = stateSentence(
    state,
    title.media_type,
    title.library_state,
    currentSeason,
    queueItem,
  )

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title.title}
      description={title.title}
      returnFocusTo={returnFocusTo}
      customChrome
    >
      <div className="relative h-[180px] overflow-hidden bg-poster bg-gradient-to-br from-white/8 via-surface-deep to-surface" data-testid="title-backdrop">
        {showBackdrop ? (
          <img
            src={title.backdrop_url ?? undefined}
            alt=""
            aria-hidden="true"
            className="absolute inset-0 size-full object-cover"
            onError={() => setBackdropFailed(true)}
          />
        ) : null}
        <div
          aria-hidden="true"
          className="absolute inset-x-0 bottom-0 h-24 bg-gradient-to-t from-surface to-transparent"
        />
      </div>

      <DialogClose
        aria-label="Close"
        className="absolute top-3 right-3 z-20 flex size-10 items-center justify-center rounded-full bg-black/65 text-base text-ink shadow-lg ring-1 ring-inset ring-white/15 outline-none transition-colors hover:bg-black/85 focus-visible:ring-2 focus-visible:ring-gold/70 sm:size-8"
      >
        ✕
      </DialogClose>

      <div className="relative z-10 -mt-16 px-4 sm:px-[26px]">
        <section aria-labelledby="title-detail-heading">
          <div className="grid min-w-0 grid-cols-[96px_minmax(0,1fr)] gap-x-3 gap-y-3 sm:grid-cols-[148px_minmax(0,1fr)] sm:gap-x-5">
            <div
              data-testid="title-poster"
              className="aspect-[2/3] w-24 overflow-hidden rounded-lg border border-white/12 bg-poster bg-gradient-to-b from-white/10 to-transparent shadow-xl sm:row-span-2 sm:w-[148px]"
            >
              {showPoster ? (
                <img
                  src={title.poster_url ?? undefined}
                  alt=""
                  aria-hidden="true"
                  className="size-full object-cover"
                  onError={() => setPosterFailed(true)}
                />
              ) : null}
            </div>
            <div className="min-w-0 pt-[70px]">
              <DialogTitle
                id="title-detail-heading"
                className="break-words font-display text-[24px] leading-tight font-extrabold text-ink sm:text-[26px]"
              >
                {title.title}
              </DialogTitle>
              <p className="mt-1 font-mono text-[11px] tracking-wide text-faint">{meta}</p>
            </div>
            {title.overview ? (
              <p className="col-span-2 min-w-0 break-words text-sm leading-relaxed text-muted sm:col-span-1">
                {title.overview}
              </p>
            ) : null}
          </div>
        </section>
      </div>

      <div className="px-4 pt-6 pb-7 sm:px-[26px]">
        <section
          aria-labelledby="title-state-heading"
          className="rounded-xl border border-hairline bg-surface-deep p-4 sm:p-[18px]"
        >
          <h3 id="title-state-heading" className="sr-only">
            State
          </h3>
          <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex min-w-0 items-start gap-3">
              <StatusBadge status={statePresentation(state)} className="mt-0.5 shrink-0" />
              <p className="min-w-0 break-words text-sm leading-relaxed text-muted">{statusCopy}</p>
            </div>
            {seasonSelector ? <div className="shrink-0 sm:max-w-[48%]">{seasonSelector}</div> : null}
          </div>

          {progressPercent != null && queueItem ? (
            <div className="mt-4 flex min-w-0 items-center gap-3">
              <ProgressBar
                value={queueItem.progress}
                label={progressLabel}
                className="min-w-0 flex-1"
              />
              <span className="shrink-0 font-mono text-xs text-muted tabular-nums">
                {progressPercent}%
              </span>
            </div>
          ) : null}

          {effectiveSeasons && effectiveSeasons.length > 0 ? (
            <ul aria-label="Season states" className="mt-4 flex min-w-0 flex-wrap gap-1.5">
              {effectiveSeasons.map((season) => {
                const imported = season.imported_episode_count
                const target = season.target_episode_count
                const detail =
                  imported != null && target != null && imported < target
                    ? `S${season.season_number} ${imported}/${target}`
                    : `S${season.season_number}`
                return (
                  <li key={season.season_number}>
                    <StatusBadge status={requestStatus(season.status)} detail={detail} />
                  </li>
                )
              })}
            </ul>
          ) : null}
        </section>

        {actionZone || (isAdmin && pinRequestId != null && state.kind === 'available') ? (
          <section aria-labelledby="title-actions-heading" className="mt-5">
            <h3 id="title-actions-heading" className="sr-only">
              Actions
            </h3>
            <div className="flex flex-wrap items-center gap-2.5">
              {actionZone}
              {/* Keep-forever is available only for a known, watchable pin target. */}
              {isAdmin && pinRequestId != null && state.kind === 'available' ? (
                <label className="flex min-h-10 items-center gap-2 rounded-lg px-2 text-xs text-muted">
                  <input
                    type="checkbox"
                    className="size-4 accent-gold outline-none focus-visible:ring-2 focus-visible:ring-gold/60"
                    checked={keepForever}
                    disabled={setKeepForever.isPending}
                    onChange={() => void onToggleKeepForever()}
                  />
                  Keep forever · never evicted
                </label>
              ) : null}
            </div>
          </section>
        ) : null}
      </div>

      {isAdmin ? (
        <section
          aria-labelledby="title-admin-heading"
          className="min-w-0 border-t border-hairline bg-black/20 px-4 py-5 sm:px-[26px] sm:py-6"
        >
          <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
            <h3
              id="title-admin-heading"
              className="font-mono text-[10.5px] font-semibold tracking-[0.14em] text-faint uppercase"
            >
              ADMIN · RELEASES
            </h3>
            <div className="flex flex-wrap items-center gap-2">
              {state.kind === 'import_blocked' && queueItem ? retryImportButton : null}
              {showReleaseSearch ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => void runPreview(effectiveRequestId)}
                  loading={previewPending}
                >
                  Search releases
                </Button>
              ) : null}
            </div>
          </div>

          <div className="mt-4 min-w-0">
            {showReleaseSearch && previewPending ? (
              <CenteredSpinner label="Running the decision engine…" />
            ) : showReleaseSearch && preview ? (
              <ReleaseList
                preview={preview}
                onGrab={(rel) => void onGrab(rel)}
                grabbingGuid={grabbingGuid}
                canGrab={canGrab}
                variant="admin"
              />
            ) : showReleaseSearch ? (
              <p className="text-sm text-faint">
                No release search run yet for this title.
                {title.media_type === 'tv' ? ` Season ${currentSeason ?? 1}.` : ''}
              </p>
            ) : (
              // Honest in the states where searching is deliberately closed
              // (downloading/blocked/finalizing/available/settled): a search may
              // well have run — its grab is why we're here — so never claim
              // "no search run yet"; say why the browser is shut instead.
              <p className="text-sm text-faint">
                Release search isn&apos;t available in this state.
              </p>
            )}
          </div>
        </section>
      ) : null}

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
