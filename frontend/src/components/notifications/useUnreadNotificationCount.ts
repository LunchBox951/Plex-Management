import { useNotifications } from '../../api/hooks'

/** Unread count for the bell / tab badge; 0 while notifications aren't available. */
export function useUnreadNotificationCount(): number {
  const notifications = useNotifications()
  return notifications.data?.unread_count ?? 0
}
