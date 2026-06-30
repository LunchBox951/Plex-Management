import { cn } from '../../lib/cn'
import { INTENT_CLASSES, type StatusPresentation } from '../../lib/status'

interface StatusBadgeProps {
  status: StatusPresentation
  /** Optional trailing detail, e.g. a download percentage. */
  detail?: string
  className?: string
}

/** The mono status pill used on cards, rows and the detail view (handoff §3). */
export function StatusBadge({ status, detail, className }: StatusBadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded px-2 py-1',
        'font-mono text-[10px] font-semibold tracking-wide whitespace-nowrap',
        'ring-1 ring-inset',
        INTENT_CLASSES[status.intent],
        className,
      )}
    >
      {status.label}
      {detail ? <span className="opacity-80">· {detail}</span> : null}
    </span>
  )
}
