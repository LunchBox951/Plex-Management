import { cn } from '../../lib/cn'

interface ProgressBarProps {
  /** 0..1, as the backend reports it. Clamped defensively. */
  value: number
  label?: string
  className?: string
}

export function ProgressBar({ value, label = 'Progress', className }: ProgressBarProps) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100)
  return (
    <div
      role="progressbar"
      aria-label={label}
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
      className={cn('h-1.5 w-full overflow-hidden rounded-full bg-white/10', className)}
    >
      <div
        className="h-full rounded-full bg-downloading transition-[width] duration-500 ease-out"
        style={{ width: `${pct}%` }}
      />
    </div>
  )
}
