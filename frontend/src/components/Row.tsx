import { useCallback, useEffect, useRef, useState } from 'react'
import type { DiscoverResult } from '../api/types'
import { PosterCard } from './ui/PosterCard'

interface RowProps {
  title: string
  items: DiscoverResult[]
  onSelect: (item: DiscoverResult) => void
  /** Render skeletons instead of nothing while the first page loads. */
  loading?: boolean
}

/** How far each chevron scrolls — a little under one viewport of posters. */
const SCROLL_STEP = 600

/**
 * A titled, horizontally-scrollable poster strip. Chevrons scroll the track and
 * self-disable at each end; on touch/trackpad the native scroll still works.
 */
export function Row({ title, items, onSelect, loading = false }: RowProps) {
  const trackRef = useRef<HTMLDivElement>(null)
  const [atStart, setAtStart] = useState(true)
  const [atEnd, setAtEnd] = useState(false)

  const updateEdges = useCallback(() => {
    const el = trackRef.current
    if (!el) return
    const maxScroll = el.scrollWidth - el.clientWidth
    setAtStart(el.scrollLeft <= 1)
    // 1px slack so sub-pixel rounding doesn't leave the arrow stuck enabled.
    setAtEnd(el.scrollLeft >= maxScroll - 1)
  }, [])

  useEffect(() => {
    const el = trackRef.current
    if (!el) return
    updateEdges()
    el.addEventListener('scroll', updateEdges, { passive: true })
    window.addEventListener('resize', updateEdges)
    return () => {
      el.removeEventListener('scroll', updateEdges)
      window.removeEventListener('resize', updateEdges)
    }
  }, [updateEdges, items.length])

  const scrollBy = (delta: number) => {
    trackRef.current?.scrollBy({ left: delta, behavior: 'smooth' })
  }

  if (items.length === 0 && !loading) return null

  return (
    <section className="mb-8">
      <div className="mb-3 flex items-center justify-between gap-4">
        <h2 className="font-display text-lg font-bold text-ink">{title}</h2>
        <div className="flex gap-1.5">
          <ChevronButton
            direction="left"
            disabled={atStart}
            onClick={() => scrollBy(-SCROLL_STEP)}
          />
          <ChevronButton
            direction="right"
            disabled={atEnd}
            onClick={() => scrollBy(SCROLL_STEP)}
          />
        </div>
      </div>

      <div className="relative">
        <div
          ref={trackRef}
          className="flex snap-x snap-mandatory gap-4 overflow-x-auto scroll-smooth pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {items.length === 0 && loading
            ? Array.from({ length: 8 }, (_, i) => (
                <div
                  key={`skeleton-${i}`}
                  className="aspect-[2/3] w-[150px] shrink-0 snap-start animate-pulse rounded-[7px] bg-poster ring-1 ring-white/5"
                />
              ))
            : items.map((item) => (
                <div
                  key={`${item.media_type}-${item.tmdb_id}`}
                  className="w-[150px] shrink-0 snap-start"
                >
                  <PosterCard
                    title={item.title}
                    year={item.year ?? null}
                    posterUrl={item.poster_url ?? null}
                    seed={item.tmdb_id}
                    onClick={() => onSelect(item)}
                  />
                </div>
              ))}
        </div>

        {/* Right-edge fade hints there's more to scroll. */}
        {!atEnd ? (
          <div className="pointer-events-none absolute inset-y-0 right-0 w-12 bg-gradient-to-l from-bg to-transparent" />
        ) : null}
      </div>
    </section>
  )
}

interface ChevronButtonProps {
  direction: 'left' | 'right'
  disabled: boolean
  onClick: () => void
}

function ChevronButton({ direction, disabled, onClick }: ChevronButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={direction === 'left' ? 'Scroll left' : 'Scroll right'}
      className="flex size-8 items-center justify-center rounded-full bg-white/8 text-muted ring-1 ring-inset ring-white/10 transition-colors hover:text-ink hover:bg-white/12 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-white/8 disabled:hover:text-muted"
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
