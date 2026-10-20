import { useNavigate } from 'react-router'
import { useMarkNotificationsRead, useNotifications } from '../../api/hooks'
import type { NotificationItem } from '../../api/types'
import { cn } from '../../lib/cn'
import { isNotImplemented } from '../../lib/errors'
import { formatRelative } from '../../lib/format'
import { CenteredSpinner, StateMessage } from '../ui/feedback'
import { NotAvailableYet } from '../ui/NotAvailableYet'
import { AlertIcon, BellIcon, CheckIcon, InfoIcon } from '../ui/icons'

const NEEDS_ACTION: ReadonlySet<NotificationItem['kind']> = new Set([
  'help_requested',
  'service_down',
  'import_failed',
])

function NotificationIcon({ item }: { item: NotificationItem }) {
  if (item.poster_url) {
    return (
      <img
        src={item.poster_url}
        alt=""
        className="h-[54px] w-9 shrink-0 rounded-[5px] object-cover ring-1 ring-white/5"
      />
    )
  }
  const tone =
    item.kind === 'service_down' || item.kind === 'import_failed' || item.kind === 'help_requested'
      ? 'bg-error/15 text-error'
      : item.kind === 'import_succeeded' || item.kind === 'ready_to_watch'
        ? 'bg-available/15 text-available'
        : 'bg-searching/15 text-searching'
  const Glyph =
    item.kind === 'import_succeeded' || item.kind === 'ready_to_watch'
      ? CheckIcon
      : NEEDS_ACTION.has(item.kind)
        ? AlertIcon
        : InfoIcon
  return (
    <span className={cn('flex size-9 shrink-0 items-center justify-center rounded-full', tone)}>
      <Glyph className="size-4" />
    </span>
  )
}

/**
 * The notification list shared by the desktop bell panel and the Notifications
 * page. Clicking an item opens what it's about and marks it read. Until the
 * backend lands (501), it shows an honest "not available yet" state.
 */
export function NotificationList({
  onNavigate,
  className,
}: {
  /** Called after an item navigates (the bell closes its panel). */
  onNavigate?: () => void
  /** Spacing for the empty / unavailable states (the list itself is edge to edge). */
  className?: string
}) {
  const notifications = useNotifications()
  const markRead = useMarkNotificationsRead()
  const navigate = useNavigate()

  if (notifications.isLoading) return <CenteredSpinner label="Loading notifications…" />
  if (notifications.isError) {
    if (isNotImplemented(notifications.error)) {
      return (
        <NotAvailableYet
          className={className}
          title="Notifications aren't available yet."
          message="Updates on your requests, like when something is ready to watch, will show up here."
        />
      )
    }
    return (
      <StateMessage
        tone="error"
        title="Couldn't load notifications"
        message={notifications.error.message}
        action={
          <button
            type="button"
            onClick={() => void notifications.refetch()}
            className="rounded-lg bg-white/8 px-4 py-2 text-sm font-semibold text-ink ring-1 ring-inset ring-white/10 hover:bg-white/12"
          >
            Retry
          </button>
        }
      />
    )
  }
  const items = notifications.data?.notifications ?? []
  if (items.length === 0) {
    return (
      <div className={cn('flex flex-col items-center gap-2 py-10 text-center', className)}>
        <BellIcon className="size-6 text-faint" />
        <p className="text-sm text-muted">You're all caught up.</p>
      </div>
    )
  }

  const open = (item: NotificationItem) => {
    if (!item.read) markRead.mutate([item.id])
    if (item.request_id != null) void navigate('/requests')
    onNavigate?.()
  }

  return (
    <ul className="flex flex-col">
      {items.map((item) => (
        <li key={item.id}>
          <button
            type="button"
            onClick={() => open(item)}
            className={cn(
              'relative flex w-full gap-3 border-t border-hairline px-4 py-3 text-left outline-none',
              'hover:bg-white/4 focus-visible:bg-white/6',
              !item.read && 'bg-gold/[0.04]',
            )}
          >
            {!item.read ? (
              <span aria-hidden className="absolute top-6 left-1.5 size-1.5 rounded-full bg-gold" />
            ) : null}
            <NotificationIcon item={item} />
            <span className="flex min-w-0 flex-1 flex-col gap-1">
              <span className="text-[13.5px] leading-[19px] text-muted">
                {!item.read ? <span className="sr-only">Unread: </span> : null}
                <span className="text-ink">{item.message}</span>
              </span>
              {item.note ? (
                <span className="rounded-lg bg-surface-deep px-2.5 py-1.5 text-[12.5px] leading-[18px] text-muted">
                  {item.note}
                </span>
              ) : null}
            </span>
            <time className="shrink-0 font-mono text-[11px] text-faint" dateTime={item.created_at}>
              {formatRelative(item.created_at)}
            </time>
          </button>
        </li>
      ))}
    </ul>
  )
}
