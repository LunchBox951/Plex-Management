import { cn } from '../../lib/cn'

export type DotTone = 'ok' | 'warn' | 'error' | 'neutral'

const TONE_CLASSES: Record<DotTone, string> = {
  ok: 'bg-available',
  warn: 'bg-searching',
  error: 'bg-error',
  neutral: 'bg-faint',
}

/**
 * The small colored-dot + mono label pattern the header's {@link HealthDot}
 * liveness indicator introduced — pulled out here so the Status page's
 * per-subsystem cards can reuse the exact same visual language instead of
 * re-inventing a second "is this thing okay" indicator.
 */
export function Dot({ tone, label }: { tone: DotTone; label: string }) {
  return (
    <span className="flex items-center gap-2 font-mono text-[11px] text-faint">
      <span aria-hidden className={cn('size-2 rounded-full', TONE_CLASSES[tone])} />
      {label}
    </span>
  )
}
