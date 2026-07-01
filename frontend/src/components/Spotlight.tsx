import type { DiscoverResult } from '../api/types'
import { Button } from './ui/Button'

interface SpotlightProps {
  item: DiscoverResult | null
  onOpen: (item: DiscoverResult) => void
}

/**
 * A full-bleed cinematic hero. The backdrop carries the mood; a left-to-bottom
 * dark scrim keeps the title, overview, and CTA legible over any artwork.
 */
export function Spotlight({ item, onOpen }: SpotlightProps) {
  if (!item) return null

  const meta = [item.media_type === 'tv' ? 'TV' : 'Movie', item.year]
    .filter((v): v is string | number => v !== null && v !== undefined)
    .join(' · ')

  return (
    <section className="relative mb-10 overflow-hidden rounded-poster ring-1 ring-white/5">
      {item.backdrop_url ? (
        <div
          className="absolute inset-0 bg-cover bg-center"
          style={{ backgroundImage: `url(${item.backdrop_url})` }}
          aria-hidden
        />
      ) : (
        <div className="absolute inset-0 bg-gradient-to-br from-surface to-bg" aria-hidden />
      )}

      {/* Legibility scrim: darkest at the lower-left where the copy sits. */}
      <div className="absolute inset-0 bg-gradient-to-r from-black/85 via-black/40 to-transparent" />
      <div className="absolute inset-0 bg-gradient-to-t from-black/85 via-transparent to-transparent" />

      <div className="relative flex min-h-[20rem] flex-col justify-end gap-3 p-6 sm:min-h-[24rem] sm:p-10">
        <div className="max-w-2xl">
          <div className="font-mono text-xs tracking-wide text-gold">{meta}</div>
          <h2 className="mt-2 font-display text-3xl font-extrabold text-ink sm:text-4xl">
            {item.title}
          </h2>
          {item.overview ? (
            <p className="mt-3 max-w-xl text-sm leading-relaxed text-muted line-clamp-3">
              {item.overview}
            </p>
          ) : null}
          <div className="mt-5">
            <Button onClick={() => onOpen(item)}>View details</Button>
          </div>
        </div>
      </div>
    </section>
  )
}
