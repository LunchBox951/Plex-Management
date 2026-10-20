import { useId, useState, type ReactNode } from 'react'
import { cn } from '../../lib/cn'
import { Button } from './Button'
import { Sheet } from './Sheet'

export interface ConfirmEffect {
  /** `risk`: lost or risky. `undo`: how to reverse it. `info`: a neutral fact. */
  tone: 'risk' | 'undo' | 'info'
  text: ReactNode
}

interface ConfirmDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** A question: "Cancel Survivor?" */
  title: string
  /** One sentence of context. */
  intro?: ReactNode
  /** Every side effect, listed before anything happens. */
  effects: ConfirmEffect[]
  /** When set, the action stays disabled until this box is ticked. */
  acknowledge?: string
  /** Named by what it does: "Cancel request", "Mark as bad & search". */
  confirmLabel: string
  cancelLabel?: string
  onConfirm: () => void
  pending?: boolean
  /** An error from the last attempt, shown above the buttons. */
  error?: string | null
}

const MARK: Record<ConfirmEffect['tone'], { glyph: string; className: string }> = {
  risk: { glyph: '!', className: 'text-error' },
  undo: { glyph: '↺', className: 'text-available' },
  info: { glyph: '•', className: 'text-faint' },
}

/**
 * The confirmation for anything that deletes files, blocklists a release or
 * affects other people (Interactions: "Undo or confirm, never both"). Lists the
 * side effects first; the action is a solid destructive button named by what it
 * does, next to Go back.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  intro,
  effects,
  acknowledge,
  confirmLabel,
  cancelLabel = 'Go back',
  onConfirm,
  pending = false,
  error,
}: ConfirmDialogProps) {
  const [acknowledged, setAcknowledged] = useState(false)
  const ackId = useId()
  const handleOpenChange = (next: boolean) => {
    if (!next) setAcknowledged(false)
    onOpenChange(next)
  }
  return (
    <Sheet
      open={open}
      onOpenChange={handleOpenChange}
      title={title}
      size="sm"
      footer={
        <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
          <Button variant="ghost" onClick={() => handleOpenChange(false)} className="max-sm:h-12">
            {cancelLabel}
          </Button>
          <Button
            variant="destructive"
            loading={pending}
            disabled={acknowledge != null && !acknowledged}
            onClick={onConfirm}
            className="max-sm:h-12"
          >
            {confirmLabel}
          </Button>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        {intro ? <p className="text-[13.5px] leading-5 text-muted">{intro}</p> : null}
        <ul className="flex flex-col gap-2 rounded-lg bg-surface-deep p-3">
          {effects.map((effect, index) => (
            <li key={index} className="flex gap-2.5 text-[13px] leading-[19px] text-ink">
              <span aria-hidden className={cn('w-4 shrink-0 text-center', MARK[effect.tone].className)}>
                {MARK[effect.tone].glyph}
              </span>
              <span>{effect.text}</span>
            </li>
          ))}
        </ul>
        {acknowledge ? (
          <label htmlFor={ackId} className="flex cursor-pointer items-start gap-2.5 text-[13.5px] font-medium">
            <input
              id={ackId}
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
              className="mt-0.5 size-[18px] shrink-0 accent-gold"
            />
            {acknowledge}
          </label>
        ) : null}
        {error ? (
          <p role="alert" className="text-xs text-error">
            {error}
          </p>
        ) : null}
      </div>
    </Sheet>
  )
}
