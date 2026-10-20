import { Link } from 'react-router'
import { useRequests } from '../../api/hooks'
import type { DiscoverResult, RequestResponse } from '../../api/types'
import { requestToDiscoverResult } from '../../lib/requestTitle'
import { cn } from '../../lib/cn'
import { onTheWayLine } from '../../lib/requestCopy'
import { isInFlightRequestStatus, requestStatus } from '../../lib/status'
import { useImageFallback } from '../../lib/useImageFallback'
import { ProgressBar } from '../ui/ProgressBar'
import { StatusBadge } from '../ui/StatusBadge'

/** Requests the viewer asked for (or joined). Admins see everyone's list; Home
 * only shows their own. */
function isMine(request: RequestResponse): boolean {
  return request.is_owner || request.can_withdraw
}

const READY = new Set(['available', 'partially_available'])
const WAITING = new Set(['pending', 'waiting_for_air_date'])
const READY_LIMIT = 4
const ON_THE_WAY_LIMIT = 4


function Backdrop({ request, className }: { request: RequestResponse; className?: string }) {
  const { src, onError } = useImageFallback([request.backdrop_url, request.poster_url])
  return src ? (
    <img src={src} alt="" loading="lazy" onError={onError} className={cn('object-cover', className)} />
  ) : (
    <div className={cn('bg-poster', className)} />
  )
}

function Poster({ request }: { request: RequestResponse }) {
  const { src, onError } = useImageFallback([request.poster_url])
  return src ? (
    <img
      src={src}
      alt=""
      loading="lazy"
      onError={onError}
      className="h-[51px] w-[34px] shrink-0 rounded-[5px] object-cover ring-1 ring-white/5"
    />
  ) : (
    <div className="h-[51px] w-[34px] shrink-0 rounded-[5px] bg-poster ring-1 ring-white/5" />
  )
}

/**
 * Home's two personal sections: what arrived for you (Ready to watch) and what
 * you're waiting on (On the way). Clicking a card opens the title. Hidden while
 * the requests list hasn't loaded, and each section hides when it's empty.
 */
export function ActivitySections({ onOpen }: { onOpen: (title: DiscoverResult) => void }) {
  const requests = useRequests({ poll: true })
  const mine = (requests.data?.requests ?? []).filter(isMine)
  const ready = mine.filter((request) => READY.has(request.status)).slice(0, READY_LIMIT)
  const onTheWay = mine
    .filter((request) => isInFlightRequestStatus(request.status) || WAITING.has(request.status))
    .slice(0, ON_THE_WAY_LIMIT)
  if (ready.length === 0 && onTheWay.length === 0) return null

  return (
    <div className="grid gap-6 px-4 pt-6 sm:px-8 lg:grid-cols-[1.25fr_1fr] lg:px-11">
      {ready.length > 0 ? (
        <section aria-labelledby="ready-to-watch" className="min-w-0">
          <h2 id="ready-to-watch" className="mb-3 font-display text-[19px] font-extrabold">
            Ready to watch
          </h2>
          <div className="flex gap-3 overflow-x-auto pb-1 [scrollbar-width:none]">
            {ready.map((request) => (
              <button
                key={request.id}
                type="button"
                onClick={() => onOpen(requestToDiscoverResult(request))}
                className="group w-[270px] shrink-0 overflow-hidden rounded-[10px] bg-surface text-left ring-1 ring-hairline outline-none hover:ring-white/15 focus-visible:ring-2 focus-visible:ring-gold/60 sm:w-[300px]"
              >
                <span className="relative block h-[140px]">
                  <Backdrop request={request} className="absolute inset-0 size-full" />
                  <span className="absolute inset-0 bg-gradient-to-t from-surface to-transparent" />
                  <StatusBadge status={requestStatus(request.status)} className="absolute top-2.5 left-2.5" />
                </span>
                <span className="-mt-5 block px-3 pb-3">
                  <span className="relative block font-display text-base leading-tight font-extrabold text-ink">
                    {request.title}
                  </span>
                  <span className="font-mono text-[11px] text-faint">
                    {request.media_type === 'tv' ? 'TV' : 'Movie'}
                    {request.year ? ` · ${request.year}` : ''}
                  </span>
                </span>
              </button>
            ))}
          </div>
        </section>
      ) : null}
      {onTheWay.length > 0 ? (
        <section aria-labelledby="on-the-way" className="min-w-0">
          <div className="mb-3 flex items-baseline">
            <h2 id="on-the-way" className="flex-1 font-display text-[19px] font-extrabold">
              On the way
            </h2>
            <Link to="/requests" className="text-[12.5px] font-semibold text-faint hover:text-ink">
              All my requests ›
            </Link>
          </div>
          <ul className="flex flex-col gap-2">
            {onTheWay.map((request) => (
              <li key={request.id}>
                <button
                  type="button"
                  onClick={() => onOpen(requestToDiscoverResult(request))}
                  className="flex w-full items-center gap-3 rounded-lg bg-surface px-3 py-2.5 text-left ring-1 ring-hairline outline-none hover:ring-white/15 focus-visible:ring-2 focus-visible:ring-gold/60"
                >
                  <Poster request={request} />
                  <span className="flex min-w-0 flex-1 flex-col gap-1.5">
                    <span className="truncate font-display text-sm font-bold text-ink">{request.title}</span>
                    {request.status === 'downloading' && request.download_progress != null ? (
                      <ProgressBar value={request.download_progress} label={`${request.title} download progress`} />
                    ) : null}
                    <span className="truncate font-mono text-[11px] text-faint">{onTheWayLine(request)}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}
