import { useRef, useState } from 'react'
import { useCreateRequest } from '../api/hooks'
import type { CreateRequestBody, DiscoverResult } from '../api/types'
import { cn } from '../lib/cn'
import type { ApiError } from '../lib/errors'
import { TvRequestPicker } from './TvRequestPicker'
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
 * Presented as a circular "+" (issue #135: no text pills on cards), hidden
 * with `opacity-0` and revealed on `group-hover`/`group-focus-within` (the
 * `group` class lives on `PosterCard`'s root, see PosterCard.tsx) — NEVER
 * `hidden`/`display:none`, which would drop it from the tab order and make
 * the one-click Request keyboard-unreachable. A keyboard user tabbing onto
 * the button lands inside the group, so `group-focus-within` reveals it at
 * the same moment it gains focus.
 *
 * While invisible it is also `pointer-events-none`: opacity alone leaves the
 * button hit-testable, and on touch/coarse-pointer devices (no hover reveal)
 * a tap on the card's top-right corner would silently submit a request
 * instead of opening details. Pointer events return with the reveal
 * (`group-hover`/`group-focus-within`); keyboard focus and activation are
 * unaffected by pointer-events, so the tab-order guarantee above holds.
 *
 * Reuses the SAME `useCreateRequest()` mutation `TitleDetailModal` uses; there
 * is only one create-request code path. A movie is requested at once. A tv
 * title instead opens the TV request sheet (`TvRequestPicker`, Interactions:
 * "Gold + on a poster — TV: opens the TV request sheet"), which sends the
 * request itself — what part of a show to ask for is never guessed from a tile.
 * The caller's gate (Discover's `quickRequestable`) still renders this for a
 * tv title only when the compact live-state poll (issue #370 phase 2) reports
 * `has_history: false` for it — a true first-time request. A tv title with a
 * settled season-scoped row (failed/cancelled/evicted season) re-derives
 * `state === null` too, but its retry is the modal's "Request again", which
 * deliberately narrows to the selected season — so every tv retry/re-request
 * stays a modal action.
 *
 * Sits in `PosterCard`'s action layer as a DOM SIBLING of the card's own native
 * details trigger — not nested inside it — and stacks above it (`z-30` vs.
 * `z-10`), so activating this button can never also open the modal underneath
 * (see `PosterCard.tsx`; `PosterCard.test.tsx` pins that contract). On success
 * it hands keyboard focus back to the card details button before unmounting,
 * so a keyboard user keeps their place in the grid.
 */
export function QuickRequestButton({ item }: QuickRequestButtonProps) {
  const { toast } = useToast()
  const createRequest = useCreateRequest()
  // Hides the button the instant the request succeeds, ahead of the
  // /requests poll settling the tile's real badge (the invalidation this
  // mutation already fires — see useCreateRequest — refetches it shortly).
  const [justRequested, setJustRequested] = useState(false)
  // tv only: the TV request sheet, and the card trigger to hand focus back to
  // once the sheet's request succeeds (resolved at click time, like `onRequest`).
  const [tvPickerOpen, setTvPickerOpen] = useState(false)
  const returnFocusTo = useRef<HTMLElement | null>(null)

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

  const button = (
    <button
      type="button"
      // Every tile's action reads "Request" — give assistive tech the title so a
      // screen-reader user isn't left with a grid of identical "Request" buttons.
      // Kept even while invisible: this is a REVEAL (opacity), not a hide, so
      // the accessible name must stay meaningful the moment it gets focus.
      aria-label={`Request ${item.title}`}
      disabled={createRequest.isPending}
      onClick={(e) => {
        // Resolve the sibling PosterCard details trigger synchronously (before the
        // async mutation unmounts this button) so `onRequest` can restore focus.
        const card = e.currentTarget.closest<HTMLElement>('[data-poster-card]')
        const trigger = card?.querySelector<HTMLElement>('[data-poster-card-trigger]') ?? null
        if (item.media_type === 'tv') {
          returnFocusTo.current = trigger
          setTvPickerOpen(true)
          return
        }
        void onRequest(trigger)
      }}
      className={cn(
        'flex size-8 items-center justify-center rounded-full',
        'bg-gold text-gold-ink ring-1 ring-inset ring-black/10',
        'pointer-events-none opacity-0 transition-opacity duration-150 group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100',
        'hover:bg-gold-hover focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60',
        'disabled:cursor-not-allowed disabled:opacity-100 disabled:hover:bg-gold',
      )}
    >
      {createRequest.isPending ? (
        <span
          aria-hidden
          className="size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      ) : (
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2.5}
          strokeLinecap="round"
          aria-hidden
          className="size-4"
        >
          <path d="M12 5v14M5 12h14" />
        </svg>
      )}
    </button>
  )

  if (item.media_type !== 'tv') return button

  return (
    <>
      {button}
      <TvRequestPicker
        open={tvPickerOpen}
        onOpenChange={setTvPickerOpen}
        tmdbId={item.tmdb_id}
        title={item.title}
        onRequested={() => {
          // The sheet already toasted. Same hand-off as the movie path: this
          // button is about to unmount, so return focus to the card trigger --
          // deferred a tick so it lands AFTER the closing sheet restores focus
          // to this (unmounting) button.
          const trigger = returnFocusTo.current
          setJustRequested(true)
          window.setTimeout(() => trigger?.focus(), 0)
        }}
      />
    </>
  )
}
