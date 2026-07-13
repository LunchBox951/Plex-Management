import { useRef, useState, type ReactNode } from 'react'
import { useAuthMe, useRequests } from '../api/hooks'
import type { DiscoverResult, RequestResponse } from '../api/types'
import { Button } from '../components/ui/Button'
import { LinkButton } from '../components/ui/LinkButton'
import { ProgressBar } from '../components/ui/ProgressBar'
import { StatusBadge } from '../components/ui/StatusBadge'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import {
  TitleDetailModal,
  type TitleDetailModalAction,
} from '../components/TitleDetailModal'
import { requestStatus } from '../lib/status'

/**
 * Adapt a request row to the shape `TitleDetailModal` expects. The modal
 * self-correlates live state by `(tmdb_id, media_type)` off its own polled
 * `useRequests`/`useQueue`, so this is just enough to identify the title and
 * paint its poster while that correlation resolves — `overview` is omitted
 * (requests don't carry it and the modal never reads it) and `media_type` is
 * NARROWED (never cast) from the backend's free string to the literal union,
 * per the "UI only ever sets these" contract in types.ts.
 */
function requestToDiscoverResult(request: RequestResponse): DiscoverResult {
  return {
    media_type: request.media_type === 'tv' ? 'tv' : 'movie',
    tmdb_id: request.tmdb_id,
    title: request.title,
    year: request.year ?? null,
    poster_url: request.poster_url ?? null,
    backdrop_url: request.backdrop_url ?? null,
    library_state: requestStatusToLibraryState(request.status),
  }
}

/**
 * The request-status half of the server's `derive_library_state` fold
 * (services/discovery_service.py), for a synthesized `DiscoverResult`. A request
 * row carries no Plex-presence bit, so the settled/unknown fallback is an honest
 * `'none'` rather than a fabricated presence claim — the modal self-correlates
 * live state and never reads this field.
 */
function requestStatusToLibraryState(status: string): DiscoverResult['library_state'] {
  if (status === 'pending') return 'requested'
  if (
    status === 'searching' ||
    status === 'downloading' ||
    status === 'completed' ||
    status === 'no_acceptable_release' ||
    status === 'import_blocked'
  ) {
    return 'processing'
  }
  if (status === 'available') return 'available'
  if (status === 'partially_available') return 'partially_available'
  return 'none'
}

/** One request rendered as a row card (poster · identity · live state). */
function RequestRow({
  request,
  onOpen,
  onReSearch,
}: {
  request: RequestResponse
  onOpen: () => void
  onReSearch: (() => void) | null
}) {
  const meta: string[] = []
  if (request.year != null) meta.push(String(request.year))
  meta.push(request.media_type)

  const showProgress = request.status === 'downloading' && request.download_progress != null
  const progressPercent = showProgress
    ? Math.round(Math.min(1, Math.max(0, request.download_progress ?? 0)) * 100)
    : null

  // Fall back to the gradient placeholder both when there's no poster_url AND
  // when a real one fails to load (404 / expired TMDB URL) — a bad URL must
  // never leave a broken-image icon sitting in the row.
  const [imgFailed, setImgFailed] = useState(false)
  const showImg = Boolean(request.poster_url) && !imgFailed

  return (
    <li className="group relative grid grid-cols-[46px_minmax(0,1fr)] items-center gap-x-4 gap-y-3 rounded-xl border border-hairline bg-surface p-[13px] transition-colors hover:border-white/15 sm:grid-cols-[46px_minmax(0,1fr)_auto] sm:px-4">
      {/* A stretched native button keeps the card and the shortcut as sibling
          controls. It sits above the visual row but below the shortcut, so all
          ordinary mouse/touch/Enter/Space activation opens details exactly once. */}
      <button
        type="button"
        aria-label={`Open details for ${request.title}`}
        onClick={onOpen}
        className="absolute inset-0 z-10 cursor-pointer rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
      />

      {showImg ? (
        <img
          src={request.poster_url ?? undefined}
          alt=""
          loading="lazy"
          className="aspect-[2/3] w-[46px] rounded-[5px] object-cover"
          onError={() => setImgFailed(true)}
        />
      ) : (
        <div className="aspect-[2/3] w-[46px] rounded-[5px] bg-poster bg-gradient-to-b from-white/10 to-transparent" />
      )}

      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
          <p className="min-w-0 truncate font-display text-[15px] leading-tight font-bold text-ink">
            {request.title}
          </p>
          <span className="font-mono text-[11px] leading-none font-medium text-muted">
            {meta.join(' · ')}
          </span>
          {request.is_anime ? (
            <span className="shrink-0 rounded bg-gold/15 px-1.5 py-1 font-mono text-[9.5px] leading-none font-semibold tracking-[0.06em] text-gold">
              ANIME
            </span>
          ) : null}
        </div>

        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-2">
          <StatusBadge status={requestStatus(request.status)} />
          {showProgress && progressPercent != null ? (
            <div className="flex min-w-40 max-w-60 flex-[1_1_15rem] items-center gap-2">
              <ProgressBar
                value={request.download_progress ?? 0}
                label={`Download progress for ${request.title}`}
                className="min-w-20 flex-1"
              />
              <span className="shrink-0 font-mono text-[11px] text-muted tabular-nums">
                {progressPercent}%
              </span>
            </div>
          ) : null}
          {request.media_type === 'tv' && request.seasons && request.seasons.length > 0 ? (
            <ul className="flex min-w-0 flex-wrap gap-1">
              {request.seasons.map((season) => {
                // Episode-level fallback progress (ADR-0020, issue #178): "N/M"
                // while a whole-season request is partially assembled from a mix
                // of pack/episode grabs. Both counts are null for a season the
                // fallback has never touched (the common clean-pack-import case)
                // or once N reaches M (the badge degrades to the plain "Sxx" —
                // the status itself already reads completed/available then).
                const { imported_episode_count: imported, target_episode_count: target } =
                  season
                const detail =
                  target != null && imported != null && imported < target
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
        </div>
      </div>

      <div className="pointer-events-none relative z-20 col-start-2 row-start-2 flex shrink-0 items-center justify-end gap-2 sm:col-start-3 sm:row-start-1">
        {onReSearch ? (
          <Button
            variant="ghost"
            size="sm"
            className="pointer-events-auto bg-gold/10 text-gold ring-1 ring-inset ring-gold/30 hover:bg-gold/20 hover:text-gold focus-visible:ring-gold/60"
            aria-label={`Re-search ${request.title}`}
            onClick={(event) => {
              event.stopPropagation()
              onReSearch()
            }}
          >
            Re-search
          </Button>
        ) : null}
        <span aria-hidden className="text-lg leading-none text-faint">
          ›
        </span>
      </div>
    </li>
  )
}

export function Requests() {
  const { data, isLoading, isError, error, refetch } = useRequests({ poll: true })
  const auth = useAuthMe()
  const isAdmin = auth.data?.is_admin ?? auth.data?.user?.is_admin ?? false
  const requests = data?.requests ?? []

  // The same TitleDetailModal Discover uses — reused, not forked. It correlates
  // its own live state by (tmdb_id, media_type), so opening it from a request
  // row reproduces the full state-aware action zone (re-search, retry-import,
  // report a problem, cancel, keep-forever) right where the stuck status lives.
  const [selected, setSelected] = useState<DiscoverResult | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [modalAction, setModalAction] = useState<TitleDetailModalAction | null>(null)
  // Pin the modal to the EXACT row that was clicked. An admin's list can show
  // two different users' rows for the same title (the display fold keys on
  // user_id), and the modal's own title-based correlation would otherwise
  // resolve every verb — preview/grab/report/cancel/pin — to the FIRST match,
  // not necessarily the clicked one.
  const [boundRequestId, setBoundRequestId] = useState<number | null>(null)
  const nextActionToken = useRef(0)

  const openRequest = (request: RequestResponse) => {
    setModalAction(null)
    setSelected(requestToDiscoverResult(request))
    setBoundRequestId(request.id)
    setModalOpen(true)
  }

  const reSearchRequest = (request: RequestResponse) => {
    nextActionToken.current += 1
    // Resolve the target season from THIS request row's fresh season data — the
    // first no-release season is exactly the state the shortcut exists to fix.
    // The modal must not derive it from its own season selection: a reused modal
    // instance can still hold another title's pick when the action fires (see
    // TitleDetailModalAction.season). `null` for movies, and as the defensive tv
    // fallback (request-scoped preview) if no tracked season reads no-release.
    const season =
      request.media_type === 'tv'
        ? (request.seasons?.find((s) => s.status === 'no_acceptable_release')?.season_number ??
          null)
        : null
    setSelected(requestToDiscoverResult(request))
    setBoundRequestId(request.id)
    setModalAction({
      kind: 're-search',
      requestId: request.id,
      season,
      token: nextActionToken.current,
    })
    setModalOpen(true)
  }

  const changeModalOpen = (open: boolean) => {
    setModalOpen(open)
    if (!open) {
      setModalAction(null)
      setBoundRequestId(null)
    }
  }

  let body: ReactNode
  if (isLoading) {
    body = <CenteredSpinner label="Loading requests…" />
  } else if (isError && !data) {
    body = (
      <StateMessage
        tone="error"
        title="Couldn't load requests"
        message={error.message}
        action={
          <Button onClick={() => void refetch()} variant="secondary">
            Retry
          </Button>
        }
      />
    )
  } else if (requests.length === 0) {
    body = (
      <StateMessage
        title="No requests yet"
        message="Head to Discover to search for a movie or show and request it."
        action={<LinkButton to="/">Browse Discover</LinkButton>}
      />
    )
  } else {
    body = (
      <ul className="flex flex-col gap-3">
        {requests.map((request) => (
          <RequestRow
            key={request.id}
            request={request}
            onOpen={() => openRequest(request)}
            onReSearch={
              isAdmin && request.status === 'no_acceptable_release'
                ? () => reSearchRequest(request)
                : null
            }
          />
        ))}
      </ul>
    )
  }

  return (
    <div className="mx-auto w-full max-w-[1060px] px-5 py-8 sm:px-8">
      <header className="mb-6 flex items-baseline gap-3">
        <h1 className="font-display text-2xl font-extrabold">Requests</h1>
        {data ? (
          <span className="font-mono text-sm text-muted">
            {requests.length} {requests.length === 1 ? 'title' : 'titles'}
          </span>
        ) : null}
      </header>
      {body}
      {selected !== null ? (
        <TitleDetailModal
          title={selected}
          open={modalOpen}
          onOpenChange={changeModalOpen}
          action={modalAction}
          boundRequestId={boundRequestId}
        />
      ) : null}
    </div>
  )
}
