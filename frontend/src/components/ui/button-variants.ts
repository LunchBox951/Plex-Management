import { cn } from '../../lib/cn'

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
export type ButtonSize = 'sm' | 'md'

const VARIANTS: Record<ButtonVariant, string> = {
  primary: 'bg-gold text-gold-ink hover:bg-gold-hover focus-visible:ring-gold/60',
  secondary:
    'bg-white/8 text-ink hover:bg-white/12 ring-1 ring-inset ring-white/10 focus-visible:ring-white/30',
  ghost: 'bg-transparent text-muted hover:text-ink hover:bg-white/6 focus-visible:ring-white/20',
  danger:
    'bg-error/15 text-error hover:bg-error/25 ring-1 ring-inset ring-error/30 focus-visible:ring-error/50',
}

const SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-[13px]',
  md: 'h-10 px-4 text-sm',
}

/** Shared class string so a styled <Link> (LinkButton) matches <Button> exactly. */
export function buttonClasses(
  opts: {
    variant?: ButtonVariant | undefined
    size?: ButtonSize | undefined
    className?: string | undefined
  } = {},
): string {
  const { variant = 'primary', size = 'md', className } = opts
  return cn(
    'inline-flex items-center justify-center gap-2 rounded-lg font-semibold',
    'transition-colors outline-none focus-visible:ring-2',
    'disabled:cursor-not-allowed disabled:opacity-50',
    VARIANTS[variant],
    SIZES[size],
    className,
  )
}
