import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'

interface PosterCardProps {
  title: string
  year?: number | null
  posterUrl?: string | null
  /** Seed for the deterministic placeholder gradient (e.g. tmdb_id). */
  seed?: number
  onClick?: () => void
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
      className={cn(
        'group relative aspect-[2/3] overflow-hidden rounded-[7px] bg-poster',
        'ring-1 ring-white/5 transition-transform duration-200',
        interactive && 'cursor-pointer hover:-translate-y-1 hover:ring-white/15',
        className,
      )}
      {...(interactive
        ? {
            role: 'button',
            tabIndex: 0,
            onClick,
            onKeyDown: (e: React.KeyboardEvent) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                onClick?.()
              }
            },
          }
        : {})}
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

      {badge ? <div className="absolute top-2 left-2 z-10">{badge}</div> : null}
      {action ? <div className="absolute top-2 right-2 z-10">{action}</div> : null}

      <div className="absolute right-2.5 bottom-2 left-2.5">
        <div className="font-display text-[12.5px] leading-tight font-bold text-ink line-clamp-2">
          {title}
        </div>
        {year ? <div className="font-mono text-[10.5px] text-muted/80">{year}</div> : null}
      </div>
    </div>
  )
}
