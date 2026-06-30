import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { type ButtonSize, type ButtonVariant, buttonClasses } from './button-variants'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  children: ReactNode
}

export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled,
  className,
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      // Always non-interactive while loading, even when an explicit falsy
      // `disabled` is passed (`??` would let a spinning button stay clickable).
      disabled={(disabled ?? false) || loading}
      className={buttonClasses({ variant, size, className })}
      {...rest}
    >
      {loading && (
        <span
          aria-hidden
          className="size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      )}
      {children}
    </button>
  )
}
