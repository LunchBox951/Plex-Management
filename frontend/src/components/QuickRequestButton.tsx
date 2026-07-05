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
 *
 * Sits inside `PosterCard`'s clickable card, whose outer div both opens the
 * detail modal on click AND re-fires that same `onClick` for a keyboard
 * Enter/Space (see `PosterCard.tsx`). Both the mouse `onClick` and the
 * `onKeyDown` here stop propagation so activating this button never also
 * opens the modal underneath it. On success it hands keyboard focus back to
 * that card before unmounting, so a keyboard user keeps their place in the grid.
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
      // poster grid. Hand focus back to the enclosing card (still mounted, now
      // heading toward its "Requested" badge) so keyboard nav stays put. A no-op
      // for mouse users and when there's no focusable card ancestor.
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
        // Resolve the enclosing PosterCard synchronously (before the async
        // mutation unmounts this button) so `onRequest` can restore focus to it.
        const card = e.currentTarget.closest<HTMLElement>('[role="button"]')
        void onRequest(card)
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
