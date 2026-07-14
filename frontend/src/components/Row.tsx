import { useLayoutEffect, useMemo, useRef } from 'react'
import type { DiscoverResult } from '../api/types'
import type { StatusPresentation } from '../lib/status'
import { PosterCard } from './ui/PosterCard'
import { TileStatusGlyph } from './ui/TileStatusGlyph'
import { QuickRequestButton } from './QuickRequestButton'

interface RowProps {
  title: string
  subtitle?: string | undefined
  items: DiscoverResult[]
  onSelect: (item: DiscoverResult) => void
  /** Render skeletons instead of nothing while the first page loads. */
  loading?: boolean
  /** Per-tile library-state badge (issue #29); `null` leaves a tile unbadged. */
  tileState?: (item: DiscoverResult) => StatusPresentation | null
  /**
   * Whether a tile whose derived state is `null` may offer the one-click Request
   * action, gating it alongside the `state === null` check. Discover derives this
   * from the same `/requests` data the tiles' `tileState` consumes: false while
   * that query hasn't SUCCEEDED yet or is invalidated pending a refetch (a stale
   * `null` right after a request is created must not offer the button — a
   * seasons-less tv POST in that window expands the just-created single-season
   * request to the whole aired series), and false for a tv title with ANY request
   * rows at all — the tile is first-time-whole-series only; every tv
   * retry/re-request goes through the modal, which has season context. Omitted
   * (presentational/standalone use), every null-state tile is requestable.
   */
  quickRequestable?: (item: DiscoverResult) => boolean
}

const LOOP_ITEM_TARGET = 20
const LOOP_COPY_COUNT = 3
/**
 * The one copy whose original (un-padded) tiles are the real, operable set: they
 * carry the focusable controls and are the only tiles in the accessibility tree.
 * Everything else — the other two copies plus any padding repeats — is a clone:
 * aria-hidden and untabbable so screen readers and keyboard users see each item
 * exactly once, yet still mouse/touch-operable via a delegated click that routes
 * to the real source item (see `isRealTile` in the tile map). The middle copy is
 * chosen because the track initializes scrolled to its start, so the real set is
 * exactly what's on screen and in reach the moment the row mounts.
 */
const REAL_COPY_INDEX = 1
const WRAP_EPSILON = 5
/** How far each chevron scrolls — a little under one viewport of posters. */
const SCROLL_STEP = 600

interface LoopTile {
  item: DiscoverResult
  slotIndex: number
  copyIndex: number
}

/** Repeat short rows into a useful loop runway without changing the source array. */
function padLoopItems(items: DiscoverResult[]): DiscoverResult[] {
  if (items.length === 0 || items.length >= LOOP_ITEM_TARGET) return items
  return Array.from({ length: LOOP_ITEM_TARGET }, (_, index) => items[index % items.length]!)
}

function measureSetWidth(track: HTMLDivElement): number {
  const firstCopy = track.querySelector<HTMLElement>('[data-loop-copy-start="0"]')
  const secondCopy = track.querySelector<HTMLElement>('[data-loop-copy-start="1"]')
  if (!firstCopy || !secondCopy) return 0
  return secondCopy.offsetLeft - firstCopy.offsetLeft
}

/** Move between content-identical copies when a native scroll reaches a physical edge. */
function wrapAtEdge(track: HTMLDivElement, setWidth: number): void {
  if (setWidth <= 0) return
  const maxScroll = track.scrollWidth - track.clientWidth
  if (maxScroll <= 0) return

  if (track.scrollLeft <= WRAP_EPSILON) {
    track.scrollLeft += setWidth
  } else if (track.scrollLeft >= maxScroll - WRAP_EPSILON) {
    track.scrollLeft -= setWidth
  }
}

/**
 * A titled, horizontally-scrollable poster strip. Three content-identical copies
 * make native scrolling and chevron navigation appear continuous in either
 * direction; only the middle copy's original tiles are real (focusable,
 * accessible), so the loop illusion never leaks duplicate tiles into the tab
 * order or the accessibility tree — but clone tiles remain mouse-operable,
 * delegating clicks to the real source item (see the tile map's `isRealTile`),
 * because after a wrap the copy on screen IS what the user is pointing at.
 */
export function Row({
  title,
  subtitle,
  items,
  onSelect,
  loading = false,
  tileState,
  quickRequestable,
}: RowProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const setWidthRef = useRef(0)
  const logicalItems = useMemo(() => padLoopItems(items), [items])
  const loopTiles = useMemo<LoopTile[]>(
    () =>
      Array.from({ length: LOOP_COPY_COUNT }, (_, copyIndex) =>
        logicalItems.map((item, slotIndex) => ({ item, slotIndex, copyIndex })),
      ).flat(),
    [logicalItems],
  )
  const logicalIdentity = useMemo(
    () => items.map((item) => `${item.media_type}:${item.tmdb_id}`).join('|'),
    [items],
  )

  useLayoutEffect(() => {
    const track = trackRef.current
    if (!track || logicalIdentity.length === 0) return

    const initialWidth = measureSetWidth(track)
    setWidthRef.current = initialWidth
    if (initialWidth > 0) track.scrollLeft = initialWidth

    const handleScroll = () => wrapAtEdge(track, setWidthRef.current)
    const handleResize = () => {
      const nextWidth = measureSetWidth(track)
      if (nextWidth <= 0) return

      const previousWidth = setWidthRef.current
      if (previousWidth > 0 && nextWidth !== previousWidth) {
        const proportionalPosition = track.scrollLeft / previousWidth
        setWidthRef.current = nextWidth
        track.scrollLeft = proportionalPosition * nextWidth
      } else {
        setWidthRef.current = nextWidth
      }
      wrapAtEdge(track, nextWidth)
    }
    const resizeObserver =
      typeof ResizeObserver === 'function' ? new ResizeObserver(handleResize) : null

    track.addEventListener('scroll', handleScroll, { passive: true })
    resizeObserver?.observe(track)
    return () => {
      track.removeEventListener('scroll', handleScroll)
      resizeObserver?.disconnect()
      setWidthRef.current = 0
    }
  }, [logicalIdentity])

  const scrollBy = (delta: number) => {
    const track = trackRef.current
    if (!track) return

    const setWidth = setWidthRef.current
    const maxScroll = track.scrollWidth - track.clientWidth
    if (setWidth > 0) {
      if (delta < 0 && track.scrollLeft < SCROLL_STEP + WRAP_EPSILON) {
        track.scrollLeft += setWidth
      } else if (delta > 0 && maxScroll - track.scrollLeft < SCROLL_STEP + WRAP_EPSILON) {
        track.scrollLeft -= setWidth
      }
    }

    const reducedMotion =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    track.scrollBy({ left: delta, behavior: reducedMotion ? 'auto' : 'smooth' })
  }

  if (items.length === 0 && !loading) return null

  return (
    <section className="mb-8">
      <div className="mb-3 flex items-center justify-between gap-4">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <h2 className="font-display text-lg font-bold text-ink">{title}</h2>
          {subtitle ? <p className="text-sm text-muted">{subtitle}</p> : null}
        </div>
        {items.length > 0 ? (
          <div className="flex gap-1.5">
            <ChevronButton direction="left" onClick={() => scrollBy(-SCROLL_STEP)} />
            <ChevronButton direction="right" onClick={() => scrollBy(SCROLL_STEP)} />
          </div>
        ) : null}
      </div>

      <div className="relative">
        <div
          ref={trackRef}
          data-row-track
          className="flex snap-x snap-mandatory gap-4 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {items.length === 0 && loading
            ? Array.from({ length: 8 }, (_, i) => (
                <div
                  key={`skeleton-${i}`}
                  className="aspect-[2/3] w-[150px] shrink-0 snap-start animate-pulse rounded-[7px] bg-poster ring-1 ring-white/5"
                />
              ))
            : loopTiles.map(({ item, slotIndex, copyIndex }) => {
                const state = tileState?.(item) ?? null
                // Exactly one copy of each source item is a real tile: the
                // un-padded slots of the middle copy. It alone renders focusable
                // controls (details trigger + quick-request) and sits in the
                // accessibility tree, so a keyboard user never tabs through
                // phantom clones and a screen reader hears each title (and the
                // row's item count) exactly once, not padded/tripled. Every other
                // rendered tile — the two mirror copies and any padding repeats —
                // is an aria-hidden clone. Clones stay MOUSE-operable (after a
                // wrap, the copy on screen is what the user is pointing at):
                // the wrapper delegates a click to the real source item's
                // details action, with no focusable control of its own.
                const isRealTile = copyIndex === REAL_COPY_INDEX && slotIndex < items.length
                // Which real slot this tile mirrors (padding repeats fold back
                // into the source range).
                const sourceSlot = slotIndex % items.length
                const openFromClone = () => {
                  // Hand focus to the real tile's trigger first (invisible to the
                  // scroll position) so the details modal opens with a sane
                  // previously-focused element — closing it returns focus to the
                  // REAL tile, never to <body> or an aria-hidden clone.
                  trackRef.current
                    ?.querySelector<HTMLElement>(
                      `[data-loop-real][data-loop-slot="${sourceSlot}"] [data-poster-card-trigger]`,
                    )
                    ?.focus({ preventScroll: true })
                  onSelect(item)
                }
                return (
                  <div
                    key={`${item.media_type}-${item.tmdb_id}-slot-${slotIndex}-copy-${copyIndex}`}
                    data-loop-copy={copyIndex}
                    data-loop-slot={slotIndex}
                    data-loop-copy-start={slotIndex === 0 ? copyIndex : undefined}
                    data-loop-real={isRealTile ? '' : undefined}
                    aria-hidden={isRealTile ? undefined : true}
                    // Delegated pointer path for clones only; the real tile's own
                    // button handles its clicks, so nothing can double-fire.
                    {...(isRealTile ? {} : { onClick: openFromClone })}
                    className="w-[150px] shrink-0 snap-start"
                  >
                    <PosterCard
                      title={item.title}
                      year={item.year ?? null}
                      plexPosterUrl={item.plex_poster_url ?? null}
                      posterUrl={item.poster_url ?? null}
                      seed={item.tmdb_id}
                      // Real tiles get the native focusable trigger. Clones omit
                      // onClick (no button, nothing tabbable inside aria-hidden)
                      // and instead restore the interactive hover affordances —
                      // lift, ring, pointer cursor — so a clone looks and feels
                      // identical to the real tile it mirrors.
                      {...(isRealTile
                        ? { onClick: () => onSelect(item) }
                        : {
                            className:
                              'cursor-pointer hover:-translate-y-1 hover:ring-white/15',
                          })}
                      badge={state ? <TileStatusGlyph status={state} /> : undefined}
                      action={
                        isRealTile && state === null && (quickRequestable?.(item) ?? true) ? (
                          <QuickRequestButton item={item} />
                        ) : undefined
                      }
                    />
                  </div>
                )
              })}
        </div>

        {/* Right-edge fade hints there's more to scroll. */}
        {items.length > 0 ? (
          <div
            data-row-end-fade
            className="pointer-events-none absolute inset-y-0 right-0 w-12 bg-gradient-to-l from-bg to-transparent"
          />
        ) : null}
      </div>
    </section>
  )
}

interface ChevronButtonProps {
  direction: 'left' | 'right'
  onClick: () => void
}

function ChevronButton({ direction, onClick }: ChevronButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={direction === 'left' ? 'Scroll left' : 'Scroll right'}
      className="flex size-8 items-center justify-center rounded-full bg-white/8 text-muted ring-1 ring-inset ring-white/10 transition-colors hover:bg-white/12 hover:text-ink"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        className="size-4"
        aria-hidden
      >
        {direction === 'left' ? (
          <polyline points="15 18 9 12 15 6" />
        ) : (
          <polyline points="9 18 15 12 9 6" />
        )}
      </svg>
    </button>
  )
}
