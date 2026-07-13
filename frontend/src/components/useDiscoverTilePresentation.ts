import { useCallback } from 'react'
import { useRequests, useRequestsInvalidated } from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { deriveTileState } from '../lib/tileState'

/**
 * Shared Discover-card presentation derived from the live request list.
 *
 * `baseDataUpdatedAt` must belong to the query that produced the cards being
 * rendered (home or search). That keeps deriveTileState's stale-base healing on
 * one client clock while allowing every Discover surface to share the same
 * safety-critical one-click request gate.
 *
 * Surfaces that mount on every route but only render tiles when visible (the
 * header search overlay) pass `enabled: false` while hidden so this hook adds
 * no /requests observer — Layout's badge already keeps that query polling, and
 * a fresh fetch starts the moment the surface becomes visible.
 */
export function useDiscoverTilePresentation(
  baseDataUpdatedAt: number | undefined,
  options?: { enabled?: boolean },
) {
  const enabled = options?.enabled ?? true
  const requests = useRequests({ poll: enabled, enabled })
  const requestsInvalidated = useRequestsInvalidated()
  const requestRows = requests.data?.requests
  const requestsSettled = requests.isSuccess && !requestsInvalidated

  const tileState = useCallback(
    (item: DiscoverResult) =>
      deriveTileState(
        item,
        requestRows,
        baseDataUpdatedAt,
        requests.dataUpdatedAt,
      ),
    [baseDataUpdatedAt, requestRows, requests.dataUpdatedAt],
  )

  const quickRequestable = useCallback(
    (item: DiscoverResult): boolean => {
      // A null tile state is not trustworthy until /requests has succeeded and
      // is no longer invalidated by a just-completed mutation.
      if (!requestsSettled) return false

      // A seasons-less TV POST means "whole aired series". Keep that shortcut
      // strictly first-request-only: any TV history (including a settled single
      // season) must return through the detail modal, which preserves scope.
      if (item.media_type === 'tv') {
        return !(requestRows ?? []).some(
          (request) =>
            request.tmdb_id === item.tmdb_id && request.media_type === 'tv',
        )
      }

      // Movie re-requests carry no season scope and remain safe from the tile.
      return true
    },
    [requestRows, requestsSettled],
  )

  return {
    tileState,
    quickRequestable,
    // Zero while stale/invalidated; a new positive revision proves the
    // post-mutation /requests refetch has settled even when its honest result is
    // a settled-bad row whose derived tile presentation is still null.
    requestStateRevision: requestsSettled ? requests.dataUpdatedAt : 0,
  }
}
