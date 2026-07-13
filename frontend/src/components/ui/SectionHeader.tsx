import type { ComponentPropsWithoutRef, JSX } from 'react'
import { cn } from '../../lib/cn'

type SectionHeaderProps = ComponentPropsWithoutRef<'h2'>

export function SectionHeader({ className, ...props }: SectionHeaderProps): JSX.Element {
  return (
    <h2
      {...props}
      className={cn(
        'font-mono text-[10.5px] leading-none font-semibold uppercase tracking-[0.14em] text-faint',
        className,
      )}
    />
  )
}
