import { useCallback } from 'react'
import { useTileLiveStates } from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { deriveTileState } from '../lib/tileState'

/**
 * Shared Discover-card presentation derived from the live-state poll (issue
 * #370 phase 2). `items` is the set of tiles CURRENTLY VISIBLE on this
 * surface (a Discover page's spotlights + rows, or a search overlay's active
 * result set) — it drives both the compact poll's key set and the query's
 * cache identity, so the poll tracks whatever the caller renders, never the
 * whole request history.
 *
 * `baseDataUpdatedAt` must belong to the query that produced the cards being
 * rendered (home or search). That keeps deriveTileState's stale-base healing on
 * one client clock while allowing every Discover surface to share the same
 * safety-critical one-click request gate.
 *
 * Surfaces that mount on every route but only render tiles when visible (the
 * header search overlay) pass `enabled: false` while hidden so this hook adds
 * no live-state observer — Layout's badge already keeps `/requests` polling
 * for other surfaces, and a fresh fetch starts the moment this surface
 * becomes visible.
 */
export function useDiscoverTilePresentation(
  items: readonly DiscoverResult[],
  baseDataUpdatedAt: number | undefined,
  options?: { enabled?: boolean },
) {
  const enabled = options?.enabled ?? true
  const liveStates = useTileLiveStates(items, { poll: enabled, enabled })
  const states = liveStates.data?.states
  const liveSettled = liveStates.isSuccess && !liveStates.invalidated

  const tileState = useCallback(
    (item: DiscoverResult) =>
      deriveTileState(
        item,
        states?.[`${item.media_type}:${item.tmdb_id}`],
        baseDataUpdatedAt,
        liveStates.dataUpdatedAt,
      ),
    [baseDataUpdatedAt, states, liveStates.dataUpdatedAt],
  )

  const quickRequestable = useCallback(
    (item: DiscoverResult): boolean => {
      // A null tile state is not trustworthy until the live-state poll has
      // succeeded and is no longer invalidated by a just-completed mutation.
      // An item with NO visible tiles (empty `items`) never reaches here —
      // the poll is disabled and `liveSettled` stays false, so nothing is
      // ever wrongly quick-requestable while unobserved.
      if (!liveSettled) return false

      // A seasons-less TV POST means "whole aired series". Keep that shortcut
      // strictly first-request-only: any TV history (including a settled single
      // season) must return through the detail modal, which preserves scope.
      if (item.media_type === 'tv') {
        return !(states?.[`${item.media_type}:${item.tmdb_id}`]?.has_history ?? false)
      }

      // Movie re-requests carry no season scope and remain safe from the tile.
      return true
    },
    [states, liveSettled],
  )

  return {
    tileState,
    quickRequestable,
    // Zero while stale/invalidated; a new positive revision proves the
    // post-mutation live-state refetch has settled even when its honest result
    // is a settled-bad row whose derived tile presentation is still null.
    requestStateRevision: liveSettled ? liveStates.dataUpdatedAt : 0,
  }
}
