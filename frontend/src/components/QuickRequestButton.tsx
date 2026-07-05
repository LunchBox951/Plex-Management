import { useState } from 'react'
import { useCreateRequest } from '../api/hooks'
import type { CreateRequestBody, DiscoverResult } from '../api/types'
import type { ApiError } from '../lib/errors'
import { Button } from './ui/Button'
import { useToast } from './ui/toast'

interface QuickRequestButtonProps {
  item: DiscoverResult
}

function asApiError(error: unknown): ApiError {
  return error as ApiError
}

/**
 * One-click Request, rendered in a tile's action slot (issue #42) when the
 * caller has already determined the tile is unbadged/requestable
 * (`deriveTileState(...) === null`) — this component never re-derives that
 * state itself.
 *
 * Reuses the SAME `useCreateRequest()` mutation `TitleDetailModal` uses; there
 * is only one create-request code path. For a tv title this always omits
 * `seasons`, which the backend reads as "track every aired season" (matches
 * the modal's own default) — targeting a single season from the tile would
 * need the season picker, so that stays a one-click-away modal action.
 * BECAUSE of that whole-series default, the caller's gate (Discover's
 * `quickRequestable`) renders this for a tv title only when the fresh
 * `/requests` list holds NO rows at all for it — a true first-time request. A
 * tv title with a settled season-scoped row (failed/cancelled/evicted season)
 * re-derives `state === null` too, but a seasons-less POST there would EXPAND
 * the tracked set to the whole aired series, where the modal's "Request again"
 * deliberately narrows to the selected season — so every tv retry/re-request
 * stays a modal action.
 *
 * Sits in `PosterCard`'s action layer beside the card's own native details
 * button. Both the mouse `onClick` and the `onKeyDown` here stop propagation so
 * activating this button never also opens the modal underneath it. On success it
 * hands keyboard focus back to the card details button before unmounting, so a
 * keyboard user keeps their place in the grid.
 */
export function QuickRequestButton({ item }: QuickRequestButtonProps) {
  const { toast } = useToast()
  const createRequest = useCreateRequest()
  // Hides the button the instant the request succeeds, ahead of the
  // /requests poll settling the tile's real badge (the invalidation this
  // mutation already fires — see useCreateRequest — refetches it shortly).
  const [justRequested, setJustRequested] = useState(false)

  if (justRequested) return null

  const onRequest = async (returnFocusTo: HTMLElement | null) => {
    const body: CreateRequestBody = { tmdb_id: item.tmdb_id, media_type: item.media_type }
    try {
      await createRequest.mutateAsync(body)
      // This button is about to unmount (`justRequested` -> `return null`). Left
      // alone that drops keyboard focus to <body>, losing the user's place in the
      // poster grid. Hand focus back to the card details trigger (still mounted,
      // now heading toward its "Requested" badge) so keyboard nav stays put. A
      // no-op for mouse users and when there's no focusable card ancestor.
      returnFocusTo?.focus()
      setJustRequested(true)
      toast({ title: `Requested ${item.title}`, intent: 'success' })
    } catch (error) {
      // A stray double-click is harmless (the backend dedups active requests),
      // so there's nothing more to do here than tell the operator what happened.
      toast({ title: 'Request failed', description: asApiError(error).message, intent: 'error' })
    }
  }

  return (
    <Button
      size="sm"
      // Every tile's action reads "Request" — give assistive tech the title so a
      // screen-reader user isn't left with a grid of identical "Request" buttons.
      aria-label={`Request ${item.title}`}
      loading={createRequest.isPending}
      onClick={(e) => {
        e.stopPropagation()
        // Resolve the sibling PosterCard details trigger synchronously (before the
        // async mutation unmounts this button) so `onRequest` can restore focus.
        const card = e.currentTarget.closest<HTMLElement>('[data-poster-card]')
        const trigger = card?.querySelector<HTMLElement>('[data-poster-card-trigger]') ?? null
        void onRequest(trigger)
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.stopPropagation()
        }
      }}
    >
      Request
    </Button>
  )
}
