import { useId } from 'react'
import { cn } from '../../lib/cn'

/**
 * The two-ticket mark: a gold ticket in front, a grey one behind it tilted −24°
 * and nudged 4px left. A transparent cutout around the front ticket keeps the two
 * apart on any dark ground. Mask ids are per-instance so several marks can share
 * a page.
 */
export function LogoMark({ className, title }: { className?: string; title?: string }) {
  const id = useId().replace(/:/g, '')
  const ticketMask = `pm-tk-${id}`
  const knockoutMask = `pm-ko-${id}`
  return (
    <svg
      viewBox="0 0 64 64"
      className={cn('size-7 shrink-0', className)}
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
    >
      <defs>
        <mask id={ticketMask} maskUnits="userSpaceOnUse" x="-20" y="-20" width="104" height="104">
          <rect x="-20" y="-20" width="104" height="104" fill="#fff" />
          <circle cx="12" cy="36" r="5.5" fill="#000" />
          <circle cx="56" cy="36" r="5.5" fill="#000" />
          <circle cx="25" cy="26" r="1.7" fill="#000" />
          <circle cx="25" cy="32" r="1.7" fill="#000" />
          <circle cx="25" cy="40" r="1.7" fill="#000" />
          <circle cx="25" cy="46" r="1.7" fill="#000" />
        </mask>
        <mask
          id={knockoutMask}
          maskUnits="userSpaceOnUse"
          x="-20"
          y="-20"
          width="104"
          height="104"
        >
          <rect x="-20" y="-20" width="104" height="104" fill="#fff" />
          <rect x="9.5" y="18.5" width="49" height="35" rx="8.5" fill="#000" />
        </mask>
      </defs>
      <g mask={`url(#${knockoutMask})`}>
        <g transform="translate(-9 -8) rotate(-24 34 36)">
          <rect
            className="fill-faint"
            x="12"
            y="21"
            width="44"
            height="30"
            rx="6"
            mask={`url(#${ticketMask})`}
          />
        </g>
      </g>
      <rect
        className="fill-gold"
        x="12"
        y="21"
        width="44"
        height="30"
        rx="6"
        mask={`url(#${ticketMask})`}
      />
    </svg>
  )
}

/** Mark + PLEXMGR wordmark, as used in the header. */
export function Logo({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-2 font-display text-[17px] font-extrabold tracking-wide',
        className,
      )}
    >
      <LogoMark />
      <span>
        PLEX<span className="text-gold">MGR</span>
      </span>
    </span>
  )
}
