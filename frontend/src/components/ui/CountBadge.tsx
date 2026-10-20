import { cn } from '../../lib/cn'

interface CountBadgeProps {
  count: number
  /** `gold` for "yours / unread", `error` for "needs attention". */
  tone?: 'gold' | 'error'
  /** Screen-reader text read after the number, e.g. "unread notifications". */
  label: string
  className?: string
}

/**
 * A round count badge. Always a pill or circle in sans type, never a key-cap
 * shape, so it can't be mistaken for a keyboard shortcut. Hidden at zero.
 */
export function CountBadge({ count, tone = 'gold', label, className }: CountBadgeProps) {
  if (count <= 0) return null
  return (
    <span
      className={cn(
        'inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full px-[5px]',
        'font-sans text-[10.5px] leading-none font-bold tabular-nums',
        tone === 'gold' ? 'bg-gold text-gold-ink' : 'bg-error text-gold-ink',
        className,
      )}
    >
      <span aria-hidden>{count > 99 ? '99+' : count}</span>
      <span className="sr-only">{`, ${count} ${label}`}</span>
    </span>
  )
}
