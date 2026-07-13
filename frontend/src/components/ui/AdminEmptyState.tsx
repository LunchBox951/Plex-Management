import type { JSX, ReactNode } from 'react'
import { cn } from '../../lib/cn'

interface AdminEmptyStateProps {
  title: string
  message: ReactNode
  /** Additive classes only; the component owns its spacing and typography. */
  className?: string
}

export function AdminEmptyState({
  title,
  message,
  className,
}: AdminEmptyStateProps): JSX.Element {
  return (
    <div
      role="status"
      aria-live="polite"
      className={cn(
        'flex flex-col gap-1.5 rounded-[10px] border border-dashed border-white/12 px-6 py-11 text-center',
        className,
      )}
    >
      <div className="text-[14px] leading-snug font-bold text-muted">{title}</div>
      <div className="text-[13.5px] leading-relaxed text-faint">{message}</div>
    </div>
  )
}
