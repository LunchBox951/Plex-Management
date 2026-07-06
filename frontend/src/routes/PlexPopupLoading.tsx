import { CenteredSpinner } from '../components/ui/feedback'

/**
 * Standalone loading route for the plex.tv sign-in popup (`/login/plex/loading`)
 * — a centered spinner shown while the popup is being pointed at plex.tv's
 * hosted login.
 */
export function PlexPopupLoading() {
  return <CenteredSpinner label="Opening plex.tv…" />
}
