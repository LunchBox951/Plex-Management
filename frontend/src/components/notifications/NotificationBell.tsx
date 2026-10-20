import * as Popover from '@radix-ui/react-popover'
import { useState } from 'react'
import { Link } from 'react-router'
import { useMarkNotificationsRead, useNotifications } from '../../api/hooks'
import { cn } from '../../lib/cn'
import { CountBadge } from '../ui/CountBadge'
import { BellIcon } from '../ui/icons'
import { NotificationList } from './NotificationList'

/** The desktop header bell: opens the notification panel over the page. */
export function NotificationBell() {
  const [open, setOpen] = useState(false)
  const notifications = useNotifications()
  const markRead = useMarkNotificationsRead()
  const unread = notifications.data?.unread_count ?? 0
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label="Notifications"
          className={cn(
            'relative flex size-9 items-center justify-center rounded-full text-muted outline-none',
            'hover:bg-white/8 hover:text-ink focus-visible:ring-2 focus-visible:ring-white/40',
            open && 'bg-white/10 text-ink',
          )}
        >
          <BellIcon />
          <CountBadge
            count={unread}
            label="unread notifications"
            className="absolute -top-0.5 -right-0.5 h-4 min-w-4 px-1 text-[10px] shadow-[0_0_0_2px_var(--color-bg)]"
          />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          className="z-50 flex max-h-[min(640px,80vh)] w-[420px] flex-col overflow-hidden rounded-2xl bg-surface shadow-[0_20px_60px_rgb(0_0_0/65%)] ring-1 ring-white/10 outline-none"
        >
          <div className="flex items-center px-4 pt-4 pb-3">
            <h2 className="flex-1 font-display text-[17px] font-extrabold">Notifications</h2>
            {unread > 0 ? (
              <button
                type="button"
                onClick={() => markRead.mutate(undefined)}
                className="text-[12.5px] font-semibold text-gold hover:text-gold-hover"
              >
                Mark all as read
              </button>
            ) : null}
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto pb-1">
            <NotificationList className="mx-4 mb-3" onNavigate={() => setOpen(false)} />
          </div>
          <div className="flex justify-end border-t border-hairline px-4 py-3">
            <Link
              to="/notifications"
              onClick={() => setOpen(false)}
              className="text-[12.5px] font-semibold text-faint hover:text-ink"
            >
              See all ›
            </Link>
          </div>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  )
}
