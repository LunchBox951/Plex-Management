import { useState, type ReactNode } from 'react'
import { useNotifyAdmin } from '../../api/hooks'
import type { QueueItem, RequestResponse } from '../../api/types'
import { cn } from '../../lib/cn'
import { isNotImplemented } from '../../lib/errors'
import { requestStatus } from '../../lib/status'
import { Button } from '../ui/Button'
import { NotAvailableYet } from '../ui/NotAvailableYet'
import { StatusBadge } from '../ui/StatusBadge'
import { AlertIcon, CheckIcon, InfoIcon } from '../ui/icons'

interface Copy {
  headline: string
  effect: string
  /** `error`: something needs a decision. `info`: waiting that sorts itself out. */
  intent: 'error' | 'info'
}

/** What went wrong, in plain words, for the admin (internals allowed). */
function adminCopy(request: RequestResponse, download: QueueItem | null): Copy {
  if (request.status === 'import_blocked') {
    return {
      headline: "The download couldn't be added to Plex",
      effect: download?.failed_reason
        ? `${download.failed_reason} Nothing was added to Plex.`
        : 'The files failed a check during import. Nothing was added to Plex.',
      intent: 'error',
    }
  }
  return {
    headline: 'No good copy found yet',
    effect:
      'Nothing found matched your quality profile. It keeps searching on a schedule; you can pick a release yourself.',
    intent: 'error',
  }
}

/** The same problem for the person who asked: no internals, no fixes. */
function familyCopy(request: RequestResponse): Copy {
  if (request.status === 'import_blocked') {
    return {
      headline: 'Something went wrong with the download',
      effect: "It wasn't added to Plex. The admin can fix this.",
      intent: 'error',
    }
  }
  return {
    headline: "We haven't found a good copy yet",
    effect: "We'll keep checking and let you know when it's ready. Nothing for you to do.",
    intent: 'info',
  }
}

function Poster({ request }: { request: RequestResponse }) {
  const [failed, setFailed] = useState(false)
  return request.poster_url && !failed ? (
    <img
      src={request.poster_url}
      alt=""
      onError={() => setFailed(true)}
      className="h-[60px] w-10 shrink-0 rounded-[5px] object-cover ring-1 ring-white/5"
    />
  ) : (
    <div className="h-[60px] w-10 shrink-0 rounded-[5px] bg-poster ring-1 ring-white/5" />
  )
}

interface ProblemCardProps {
  request: RequestResponse
  /** `admin` sees fixes and internals; `family` sees plain words and Let the admin know. */
  audience: 'admin' | 'family'
  /** The request's active download (admin only), for the reason and release name. */
  download?: QueueItem | null | undefined
  /** Admin fixes and the family Cancel; each opens its own confirm when risky. */
  actions?: ReactNode
  /** "Asked by Jamie" / "You asked" line under the title. */
  subtitle?: string | undefined
  onOpen?: (() => void) | undefined
}

/**
 * A stuck request explaining itself (design system: ProblemCard). Only admins
 * see fixes; the person who asked gets **Let the admin know**, which turns into
 * "The admin knows" once sent.
 */
export function ProblemCard({
  request,
  audience,
  download,
  actions,
  subtitle,
  onOpen,
}: ProblemCardProps) {
  const copy = audience === 'admin' ? adminCopy(request, download ?? null) : familyCopy(request)
  const notifyAdmin = useNotifyAdmin()
  const sent = notifyAdmin.isSuccess
  const notImplemented = notifyAdmin.isError && isNotImplemented(notifyAdmin.error)
  const title =
    request.media_type === 'tv' && request.seasons?.length === 1
      ? `${request.title} · Season ${request.seasons[0]!.season_number}`
      : request.title

  return (
    <article
      className={cn(
        'flex flex-col gap-3 rounded-[10px] bg-surface p-4 ring-1',
        copy.intent === 'error' ? 'ring-error/30' : 'ring-hairline',
      )}
    >
      <div className="flex items-center gap-3">
        <Poster request={request} />
        <div className="min-w-0 flex-1">
          {onOpen ? (
            <button
              type="button"
              onClick={onOpen}
              className="block max-w-full truncate text-left font-display text-[15px] leading-tight font-bold text-ink outline-none hover:underline focus-visible:underline"
            >
              {title}
            </button>
          ) : (
            <p className="truncate font-display text-[15px] leading-tight font-bold text-ink">{title}</p>
          )}
          {subtitle ? <p className="mt-0.5 font-mono text-[11px] text-faint">{subtitle}</p> : null}
        </div>
        <StatusBadge
          status={
            audience === 'admin'
              ? requestStatus(request.status)
              : copy.intent === 'error'
                ? { label: 'Stuck', intent: 'error' }
                : { label: 'Still looking', intent: 'searching' }
          }
          className="shrink-0"
        />
      </div>

      <div className="flex items-start gap-2.5">
        <span
          className={cn(
            'flex size-7 shrink-0 items-center justify-center rounded-full',
            sent
              ? 'bg-available/15 text-available'
              : copy.intent === 'error'
                ? 'bg-error/15 text-error'
                : 'bg-searching/15 text-searching',
          )}
        >
          {sent ? (
            <CheckIcon className="size-4" />
          ) : copy.intent === 'error' ? (
            <AlertIcon className="size-4" />
          ) : (
            <InfoIcon className="size-4" />
          )}
        </span>
        <div className="min-w-0">
          <h3 className="font-display text-base leading-[22px] font-bold text-ink">
            {sent ? 'The admin knows' : copy.headline}
          </h3>
          <p className="mt-0.5 text-[13.5px] leading-5 text-muted">
            {sent ? "You'll get a notification when it's fixed or ready to watch." : copy.effect}
          </p>
        </div>
      </div>

      {audience === 'admin' && download?.release_title ? (
        <p className="ml-[38px] truncate rounded-lg bg-surface-deep px-3 py-2 font-mono text-[11px] text-muted">
          {download.release_title}
        </p>
      ) : null}

      {notImplemented ? (
        <NotAvailableYet
          className="ml-[38px]"
          title="Letting the admin know isn't available yet."
          message="The admin can still see this problem in Manage."
        />
      ) : null}
      {notifyAdmin.isError && !notImplemented ? (
        <p role="alert" className="ml-[38px] text-xs text-error">
          {notifyAdmin.error.message}
        </p>
      ) : null}

      <div className="ml-[38px] flex flex-wrap gap-2">
        {audience === 'family' && !sent && !notImplemented ? (
          <Button
            loading={notifyAdmin.isPending}
            onClick={() => notifyAdmin.mutate({ requestId: request.id })}
          >
            Let the admin know
          </Button>
        ) : null}
        {actions}
      </div>
    </article>
  )
}
