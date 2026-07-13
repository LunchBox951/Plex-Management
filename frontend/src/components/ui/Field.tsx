import type { InputHTMLAttributes, ReactNode } from 'react'
import { useId } from 'react'
import { cn } from '../../lib/cn'

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  label: string
  hint?: ReactNode
  error?: string | undefined
  appearance?: 'default' | 'admin'
}

/** Labelled text input with inline error — the building block of wizard + settings forms. */
export function Field({
  label,
  hint,
  error,
  appearance = 'default',
  className,
  id,
  ...rest
}: FieldProps) {
  const autoId = useId()
  const inputId = id ?? autoId
  const errorId = `${inputId}-error`
  const hintId = `${inputId}-hint`
  const describedBy = error ? errorId : hint ? hintId : undefined
  return (
    <div className="flex flex-col gap-1.5">
      <label
        htmlFor={inputId}
        className={
          appearance === 'admin'
            ? 'font-mono text-[10.5px] leading-none font-semibold uppercase tracking-[0.12em] text-faint'
            : 'text-sm font-medium text-muted'
        }
      >
        {label}
      </label>
      <input
        id={inputId}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className={cn(
          appearance === 'admin'
            ? 'h-10 rounded-lg bg-surface-deep px-3 font-mono text-xs text-ink'
            : 'h-10 rounded-lg bg-bg px-3 text-sm text-ink',
          'ring-1 ring-inset ring-white/10 outline-none',
          'placeholder:text-faint focus-visible:ring-2 focus-visible:ring-gold/50',
          error && 'ring-error/50 focus-visible:ring-error/60',
          className,
        )}
        {...rest}
      />
      {error ? (
        <p id={errorId} className="text-xs text-error">
          {error}
        </p>
      ) : hint ? (
        <p id={hintId} className="text-xs text-faint">
          {hint}
        </p>
      ) : null}
    </div>
  )
}
