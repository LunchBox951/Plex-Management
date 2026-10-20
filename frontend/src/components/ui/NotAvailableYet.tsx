import { cn } from '../../lib/cn'
import { InfoIcon } from './icons'

/**
 * The honest state for a planned feature whose backend answered 501
 * `not_implemented`: says so plainly instead of a generic error.
 */
export function NotAvailableYet({
  title,
  message,
  className,
}: {
  title: string
  message?: string | undefined
  className?: string | undefined
}) {
  return (
    <div
      role="status"
      className={cn(
        'flex items-start gap-3 rounded-[10px] bg-searching/10 px-4 py-3 ring-1 ring-inset ring-searching/30',
        className,
      )}
    >
      <span className="mt-0.5 text-searching">
        <InfoIcon />
      </span>
      <div className="min-w-0 text-[13px] leading-[19px] text-muted">
        <b className="font-semibold text-ink">{title}</b>
        {message ? <> {message}</> : null}
      </div>
    </div>
  )
}
