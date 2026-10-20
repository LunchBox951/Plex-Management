import type { RequestResponse } from '../api/types'
import { requestStatus } from './status'

/** Plain words for what a request is waiting on (Interactions: honest status). */
export function onTheWayLine(request: RequestResponse): string {
  switch (request.status) {
    case 'searching':
      return 'Looking for a good copy'
    case 'downloading':
      return request.download_progress != null
        ? `Downloading · ${Math.round(request.download_progress * 100)}%`
        : 'Downloading'
    case 'completed':
      return 'Almost ready: adding it to Plex'
    case 'no_acceptable_release':
      return "Still looking: we'll keep checking"
    case 'import_blocked':
      return 'Stuck: the admin can fix this'
    case 'waiting_for_air_date':
      return "Not out yet: we'll grab it when it airs"
    case 'pending':
      return 'Requested'
    default:
      return requestStatus(request.status).label
  }
}
