import * as RadixDialog from '@radix-ui/react-dialog'
import type { ComponentPropsWithoutRef, ReactNode } from 'react'
import { cn } from '../../lib/cn'

interface DialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  title: string
  /** Optional accessible description (visually hidden if `srOnlyDescription`). */
  description?: string
  children: ReactNode
  /** Hero/backdrop slot rendered above the body (e.g. a title backdrop). */
  hero?: ReactNode
  /**
   * Opt in to caller-composed chrome. The content keeps Radix's focus/scroll
   * behavior, but the caller places {@link DialogTitle} and
   * {@link DialogClose} and owns every inner gutter.
   */
  customChrome?: boolean
  className?: string
  /** Optional explicit focus target for caller-owned (triggerless) dialogs. */
  returnFocusTo?: HTMLElement | null | (() => HTMLElement | null) | undefined
}

/** A caller-placeable, semantic Radix dialog title for custom chrome. */
export function DialogTitle({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof RadixDialog.Title>) {
  return <RadixDialog.Title className={className} {...props} />
}

/** A caller-placeable Radix close control that preserves focus restoration. */
export function DialogClose({
  className,
  ...props
}: ComponentPropsWithoutRef<typeof RadixDialog.Close>) {
  return <RadixDialog.Close className={className} {...props} />
}

/** Accessible modal (Radix): focus-trapped, Esc/backdrop close, scroll-locked. */
export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  hero,
  customChrome = false,
  className,
  returnFocusTo,
}: DialogProps) {
  return (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/72 backdrop-blur-sm" />
        <RadixDialog.Content
          onCloseAutoFocus={
            returnFocusTo
              ? (event) => {
                  const target =
                    typeof returnFocusTo === 'function' ? returnFocusTo() : returnFocusTo
                  if (target?.isConnected) {
                    event.preventDefault()
                    target.focus()
                  }
                }
              : undefined
          }
          className={cn(
            'fixed top-1/2 left-1/2 z-50 w-[calc(100vw-2rem)]',
            customChrome ? 'max-w-[820px]' : 'max-w-3xl',
            '-translate-x-1/2 -translate-y-1/2 overflow-hidden rounded-2xl',
            'border border-white/10 bg-surface shadow-2xl outline-none',
            'max-h-[90vh] overflow-y-auto',
            className,
          )}
        >
          {customChrome ? (
            <>
              {description ? (
                <RadixDialog.Description className="sr-only">{description}</RadixDialog.Description>
              ) : null}
              {children}
            </>
          ) : (
            <>
              {hero}
              <div className="p-6">
                <div className="mb-4 flex items-start justify-between gap-4">
                  <RadixDialog.Title className="font-display text-2xl font-extrabold text-ink">
                    {title}
                  </RadixDialog.Title>
                  <RadixDialog.Close
                    aria-label="Close"
                    className="flex size-8 shrink-0 items-center justify-center rounded-full bg-white/8 text-muted hover:bg-white/14 hover:text-ink"
                  >
                    ✕
                  </RadixDialog.Close>
                </div>
                {description ? (
                  <RadixDialog.Description className="sr-only">{description}</RadixDialog.Description>
                ) : null}
                {children}
              </div>
            </>
          )}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}
