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
 * opens the modal underneath it.
 */
export function QuickRequestButton({ item }: QuickRequestButtonProps) {
  const { toast } = useToast()
  const createRequest = useCreateRequest()
  // Hides the button the instant the request succeeds, ahead of the
  // /requests poll settling the tile's real badge (the invalidation this
  // mutation already fires — see useCreateRequest — refetches it shortly).
  const [justRequested, setJustRequested] = useState(false)

  if (justRequested) return null

  const onRequest = async () => {
    const body: CreateRequestBody = { tmdb_id: item.tmdb_id, media_type: item.media_type }
    try {
      await createRequest.mutateAsync(body)
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
      loading={createRequest.isPending}
      onClick={(e) => {
        e.stopPropagation()
        void onRequest()
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
