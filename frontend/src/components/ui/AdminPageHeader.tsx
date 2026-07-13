import type { JSX, ReactNode } from 'react'
import { cn } from '../../lib/cn'

interface AdminPageHeaderProps {
  title: string
  count?: ReactNode
  description?: string
  actions?: ReactNode
  status?: ReactNode
  /** Additive classes only; the component owns its layout and typography. */
  className?: string
}

export function AdminPageHeader({
  title,
  count,
  description,
  actions,
  status,
  className,
}: AdminPageHeaderProps): JSX.Element {
  const hasStatus = status != null
  const hasActions = actions != null

  return (
    <header className={cn('flex flex-col gap-2', className)}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="flex shrink-0 items-baseline gap-2">
          <h1 className="font-display text-[22px] leading-none font-extrabold text-ink">
            {title}
          </h1>
          {count != null ? (
            <span className="font-mono text-xs leading-none font-medium text-faint">{count}</span>
          ) : null}
        </div>

        {hasStatus || hasActions ? (
          <div className="ml-auto flex max-w-full shrink-0 flex-wrap items-center justify-end gap-3">
            {hasStatus ? (
              <div
                role="status"
                aria-live="polite"
                aria-atomic="true"
                className="font-mono text-[11px] leading-none text-faint"
              >
                {status}
              </div>
            ) : null}
            {hasActions ? (
              <div className="flex flex-wrap items-center justify-end gap-2">{actions}</div>
            ) : null}
          </div>
        ) : null}
      </div>

      {description ? (
        <p className="truncate text-[12.5px] leading-relaxed text-faint">{description}</p>
      ) : null}
    </header>
  )
}
