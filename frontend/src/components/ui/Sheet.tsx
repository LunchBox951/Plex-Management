import * as RadixDialog from '@radix-ui/react-dialog'
import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'
import { CloseIcon } from './icons'

interface SheetProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: ReactNode
  /** Accessible description; visually hidden. */
  description?: string
  /** Shown under the title (e.g. a poster + meta line). */
  header?: ReactNode
  /** Fixed below the scrolling body: the summary and the main action. */
  footer?: ReactNode
  children: ReactNode
  /** Desktop width. */
  size?: 'sm' | 'md' | 'lg'
  className?: string
}

const SIZES = { sm: 'sm:max-w-[420px]', md: 'sm:max-w-[560px]', lg: 'sm:max-w-[800px]' } as const

/**
 * The one modal surface for the overhaul: a bottom sheet on phones (slides up,
 * grab handle, full-width), a centered panel on desktop. Header and footer stay
 * put while the body scrolls, so the main button is always reachable. Closes on
 * the ✕, Esc, a tap outside, or Back (Radix handles focus and scroll lock).
 */
export function Sheet({
  open,
  onOpenChange,
  title,
  description,
  header,
  footer,
  children,
  size = 'md',
  className,
}: SheetProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/60 backdrop-blur-[2px]" />
        <RadixDialog.Content
          className={cn(
            'fixed z-50 flex flex-col overflow-hidden bg-surface outline-none',
            'shadow-[0_-10px_40px_rgb(0_0_0/50%)] ring-1 ring-white/10',
            // phone: bottom sheet
            'inset-x-0 bottom-0 max-h-[92dvh] rounded-t-[20px]',
            // desktop: centered panel
            'sm:inset-x-auto sm:top-1/2 sm:bottom-auto sm:left-1/2 sm:max-h-[86vh] sm:w-[calc(100vw-2rem)]',
            'sm:-translate-x-1/2 sm:-translate-y-1/2 sm:rounded-2xl',
            SIZES[size],
            className,
          )}
        >
          <div className="mx-auto mt-2 h-[5px] w-10 shrink-0 rounded-full bg-white/20 sm:hidden" />
          <div className="flex shrink-0 items-start gap-3 px-5 pt-3 pb-3 sm:px-6 sm:pt-5">
            <div className="min-w-0 flex-1">
              <RadixDialog.Title className="font-display text-xl leading-tight font-extrabold text-ink">
                {title}
              </RadixDialog.Title>
              {description ? (
                <RadixDialog.Description className="sr-only">{description}</RadixDialog.Description>
              ) : null}
            </div>
            <RadixDialog.Close
              aria-label="Close"
              className="flex size-9 shrink-0 items-center justify-center rounded-full bg-white/8 text-muted outline-none hover:bg-white/14 hover:text-ink focus-visible:ring-2 focus-visible:ring-white/40"
            >
              <CloseIcon />
            </RadixDialog.Close>
          </div>
          {header ? <div className="shrink-0 px-5 sm:px-6">{header}</div> : null}
          <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-5 sm:px-6">{children}</div>
          {footer ? (
            <div className="shrink-0 border-t border-hairline bg-surface-deep px-5 py-3.5 pb-[max(0.875rem,env(safe-area-inset-bottom))] sm:px-6">
              {footer}
            </div>
          ) : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}
