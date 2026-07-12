import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'

interface PosterCardProps {
  title: string
  year?: number | null
  posterUrl?: string | null
  /** Seed for the deterministic placeholder gradient (e.g. tmdb_id). */
  seed?: number
  onClick?: (trigger: HTMLButtonElement) => void
  /** Overlay slot, top-left (e.g. a StatusBadge). */
  badge?: ReactNode
  /** Overlay slot, top-right (e.g. a request button or library check). */
  action?: ReactNode
  className?: string
}

/** Deterministic cinematic gradient so missing-art posters still look intentional. */
function gradient(seed: number): string {
  const hue = (seed * 47) % 360
  return `linear-gradient(157deg, hsl(${hue} 42% 30%), hsl(${(hue + 280) % 360} 38% 12%) 70%, #0a0c10)`
}

export function PosterCard({
  title,
  year,
  posterUrl,
  seed = 0,
  onClick,
  badge,
  action,
  className,
}: PosterCardProps) {
  const interactive = typeof onClick === 'function'
  return (
    <div
      data-poster-card
      className={cn(
        'group relative aspect-[2/3] overflow-hidden rounded-[7px] bg-poster',
        'ring-1 ring-white/5 transition-transform duration-200',
        interactive && 'hover:-translate-y-1 hover:ring-white/15 focus-within:ring-white/15',
        className,
      )}
    >
      {posterUrl ? (
        <img
          src={posterUrl}
          alt=""
          loading="lazy"
          className="absolute inset-0 size-full object-cover"
        />
      ) : (
        <div className="absolute inset-0" style={{ background: gradient(seed) }} />
      )}

      <div className="absolute inset-0 bg-gradient-to-t from-black/85 via-black/10 to-transparent" />

      {interactive ? (
        <button
          type="button"
          data-poster-card-trigger
          aria-label={year ? `View details for ${title} (${year})` : `View details for ${title}`}
          onClick={(event) => onClick(event.currentTarget)}
          className="absolute inset-0 z-10 cursor-pointer rounded-[7px] border-0 bg-transparent p-0 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-gold/60"
        />
      ) : null}

      {badge ? <div className="pointer-events-none absolute top-2 left-2 z-20">{badge}</div> : null}
      {action ? (
        // pointer-events-none so the wrapper never swallows taps meant for the
        // full-card details trigger underneath (z-10) while its child action is
        // hidden — the revealed child opts back in with its own
        // pointer-events-auto (see QuickRequestButton's reveal classes).
        <div className="pointer-events-none absolute top-2 right-2 z-30">{action}</div>
      ) : null}

      <div className="pointer-events-none absolute right-2.5 bottom-2 left-2.5 z-20">
        <div className="font-display text-[12.5px] leading-tight font-bold text-ink line-clamp-2">
          {title}
        </div>
        {year ? <div className="font-mono text-[10.5px] text-muted/80">{year}</div> : null}
      </div>
    </div>
  )
}
