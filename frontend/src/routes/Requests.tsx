import { useState, type ReactNode } from 'react'
import { useRequests } from '../api/hooks'
import type { DiscoverResult, RequestResponse } from '../api/types'
import { Button } from '../components/ui/Button'
import { LinkButton } from '../components/ui/LinkButton'
import { StatusBadge } from '../components/ui/StatusBadge'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { TitleDetailModal } from '../components/TitleDetailModal'
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

/** One request rendered as a row card (poster · title/meta · status). */
function RequestRow({ request, onOpen }: { request: RequestResponse; onOpen: () => void }) {
  const meta: string[] = []
  if (request.year != null) meta.push(String(request.year))
  meta.push(request.media_type)

  // Fall back to the gradient placeholder both when there's no poster_url AND
  // when a real one fails to load (404 / expired TMDB URL) — a bad URL must
  // never leave a broken-image icon sitting in the row.
  const [imgFailed, setImgFailed] = useState(false)
  const showImg = Boolean(request.poster_url) && !imgFailed

  return (
    <li className="rounded-xl border border-hairline bg-surface">
      {/* role="button" (not a native <button>) so the block-level row content
          (div/p/ul/li) stays valid HTML5. */}
      <div
        role="button"
        tabIndex={0}
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            onOpen()
          }
        }}
        className="flex w-full cursor-pointer items-center gap-4 rounded-xl p-4 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
      >
        {showImg ? (
          <img
            src={request.poster_url ?? undefined}
            alt=""
            loading="lazy"
            className="aspect-[2/3] w-11 shrink-0 rounded object-cover"
            onError={() => setImgFailed(true)}
          />
        ) : (
          <div className="aspect-[2/3] w-11 shrink-0 rounded bg-poster bg-gradient-to-b from-white/10 to-transparent" />
        )}
        <div className="min-w-0 flex-1">
          <p className="truncate font-display font-semibold text-ink">{request.title}</p>
          <p className="mt-0.5 flex items-center gap-2 font-mono text-xs text-muted">
            <span className="truncate">{meta.join(' · ')}</span>
            {request.is_anime ? (
              <span className="shrink-0 rounded bg-gold/15 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-gold">
                ANIME
              </span>
            ) : null}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1.5">
          <StatusBadge status={requestStatus(request.status)} />
          {request.media_type === 'tv' && request.seasons && request.seasons.length > 0 ? (
            <ul className="flex flex-wrap justify-end gap-1">
              {request.seasons.map((season) => (
                <li key={season.season_number}>
                  <StatusBadge
                    status={requestStatus(season.status)}
                    detail={`S${season.season_number}`}
                  />
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      </div>
    </li>
  )
}

export function Requests() {
  const { data, isLoading, isError, error, refetch } = useRequests({ poll: true })
  const requests = data?.requests ?? []

  // The same TitleDetailModal Discover uses — reused, not forked. It correlates
  // its own live state by (tmdb_id, media_type), so opening it from a request
  // row reproduces the full state-aware action zone (re-search, retry-import,
  // report a problem, cancel, keep-forever) right where the stuck status lives.
  const [selected, setSelected] = useState<DiscoverResult | null>(null)
  const [modalOpen, setModalOpen] = useState(false)

  const openRequest = (request: RequestResponse) => {
    setSelected(requestToDiscoverResult(request))
    setModalOpen(true)
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
          <RequestRow key={request.id} request={request} onOpen={() => openRequest(request)} />
        ))}
      </ul>
    )
  }

  return (
    <div>
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
        <TitleDetailModal title={selected} open={modalOpen} onOpenChange={setModalOpen} />
      ) : null}
    </div>
  )
}
