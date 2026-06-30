import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cn(
        'inline-block size-5 animate-spin rounded-full border-2 border-white/20 border-t-gold',
        className,
      )}
    />
  )
}

export function CenteredSpinner({ label }: { label?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-faint">
      <Spinner />
      {label ? <span className="font-mono text-xs">{label}</span> : null}
    </div>
  )
}

interface StateProps {
  title: string
  message?: string
  icon?: ReactNode
  action?: ReactNode
  tone?: 'neutral' | 'error'
}

/** Empty / error states share one honest layout — surface the state, offer a way out. */
export function StateMessage({ title, message, icon, action, tone = 'neutral' }: StateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-xl border border-dashed border-white/10 px-6 py-14 text-center">
      {icon ? <div className="text-3xl">{icon}</div> : null}
      <div
        className={cn('font-display text-lg font-semibold', tone === 'error' ? 'text-error' : 'text-ink')}
      >
        {title}
      </div>
      {message ? <p className="max-w-sm text-sm text-muted">{message}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  )
}
