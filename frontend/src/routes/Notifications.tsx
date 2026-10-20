import { useMarkNotificationsRead, useNotifications } from '../api/hooks'
import { NotificationList } from '../components/notifications/NotificationList'

/** The Notifications page: the phone tab, and "See all" from the desktop bell. */
export function Notifications() {
  const notifications = useNotifications()
  const markRead = useMarkNotificationsRead()
  const unread = notifications.data?.unread_count ?? 0
  return (
    <div className="mx-auto w-full max-w-[720px] px-4 py-6 sm:px-8 sm:py-8">
      <header className="mb-4 flex items-center">
        <h1 className="flex-1 font-display text-[28px] leading-tight font-extrabold sm:text-2xl">
          Notifications
        </h1>
        {unread > 0 ? (
          <button
            type="button"
            onClick={() => markRead.mutate(undefined)}
            className="text-[13px] font-semibold text-gold hover:text-gold-hover"
          >
            Mark all read
          </button>
        ) : null}
      </header>
      <div className="overflow-hidden rounded-[10px] bg-surface ring-1 ring-hairline [&>ul>li:first-child>button]:border-t-0">
        <NotificationList className="m-4" />
      </div>
    </div>
  )
}
