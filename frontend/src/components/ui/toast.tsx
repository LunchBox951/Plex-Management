import * as RadixToast from '@radix-ui/react-toast'
import { createContext, use, useCallback, useMemo, useState, type ReactNode } from 'react'
import { cn } from '../../lib/cn'

export type ToastIntent = 'info' | 'success' | 'warning' | 'error'

/** One follow-up a toast can offer: Undo, Open it. Clicking it closes the toast. */
interface ToastAction {
  label: string
  onClick: () => void
}

interface ToastItem {
  id: number
  title: string
  description?: string
  intent: ToastIntent
  action?: ToastAction
}

interface ToastInput {
  title: string
  description?: string
  intent?: ToastIntent
  action?: ToastAction
}

interface ToastContextValue {
  toast: (input: ToastInput) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

const ACCENT: Record<ToastIntent, string> = {
  info: 'border-l-gold',
  success: 'border-l-available',
  warning: 'border-l-downloading',
  error: 'border-l-error',
}

let nextId = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])

  const toast = useCallback((input: ToastInput) => {
    const id = nextId++
    setItems((prev) => [...prev, { id, intent: 'info', ...input }])
  }, [])

  const remove = useCallback((id: number) => {
    setItems((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const value = useMemo(() => ({ toast }), [toast])

  return (
    <ToastContext value={value}>
      <RadixToast.Provider swipeDirection="right" duration={4500}>
        {children}
        {items.map((item) => (
          <RadixToast.Root
            key={item.id}
            // A toast that offers Undo stays a little longer so it can be used.
            {...(item.action ? { duration: 6500 } : {})}
            onOpenChange={(open) => {
              if (!open) remove(item.id)
            }}
            className={cn(
              'flex items-start gap-3 rounded-xl border border-white/10 border-l-[3px] bg-surface px-4 py-3',
              'shadow-2xl',
              ACCENT[item.intent],
            )}
          >
            <div className="min-w-0">
              <RadixToast.Title className="text-sm font-semibold text-ink">
                {item.title}
              </RadixToast.Title>
              {item.description ? (
                <RadixToast.Description className="mt-0.5 text-xs text-muted">
                  {item.description}
                </RadixToast.Description>
              ) : null}
            </div>
            {item.action ? (
              <RadixToast.Action
                altText={item.action.label}
                onClick={item.action.onClick}
                className="ml-auto shrink-0 self-center rounded-md px-2 py-1 text-[13px] font-semibold text-gold outline-none hover:bg-gold/10 focus-visible:ring-2 focus-visible:ring-gold/60"
              >
                {item.action.label}
              </RadixToast.Action>
            ) : null}
          </RadixToast.Root>
        ))}
        <RadixToast.Viewport className="fixed right-0 bottom-0 z-[60] m-4 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2 outline-none" />
      </RadixToast.Provider>
    </ToastContext>
  )
}

// eslint-disable-next-line react-refresh/only-export-components -- hook co-located with its provider
export function useToast(): ToastContextValue {
  const ctx = use(ToastContext)
  if (!ctx) throw new Error('useToast must be used within <ToastProvider>')
  return ctx
}
