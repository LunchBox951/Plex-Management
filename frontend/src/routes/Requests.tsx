import type { ReactNode } from 'react'
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

  return (
    <li className="flex items-center gap-4 rounded-xl border border-hairline bg-surface p-4">
      <div className="aspect-[2/3] w-11 shrink-0 rounded bg-poster bg-gradient-to-b from-white/10 to-transparent" />
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
      <StatusBadge status={requestStatus(request.status)} className="shrink-0" />
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
