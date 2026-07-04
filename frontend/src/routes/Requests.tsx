import { useState, type ReactNode } from 'react'
import { useRequests } from '../api/hooks'
import type { RequestResponse } from '../api/types'
import { Button } from '../components/ui/Button'
import { LinkButton } from '../components/ui/LinkButton'
import { StatusBadge } from '../components/ui/StatusBadge'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { requestStatus } from '../lib/status'

/** One request rendered as a row card (poster · title/meta · status). */
function RequestRow({ request }: { request: RequestResponse }) {
  const meta: string[] = []
  if (request.year != null) meta.push(String(request.year))
  meta.push(request.media_type)

  // Fall back to the gradient placeholder both when there's no poster_url AND
  // when a real one fails to load (404 / expired TMDB URL) — a bad URL must
  // never leave a broken-image icon sitting in the row.
  const [imgFailed, setImgFailed] = useState(false)
  const showImg = Boolean(request.poster_url) && !imgFailed

  return (
    <li className="flex items-center gap-4 rounded-xl border border-hairline bg-surface p-4">
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
    </li>
  )
}

export function Requests() {
  const { data, isLoading, isError, error, refetch } = useRequests({ poll: true })
  const requests = data?.requests ?? []

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
          <RequestRow key={request.id} request={request} />
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
    </div>
  )
}
