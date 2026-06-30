import { Link, type LinkProps } from 'react-router-dom'
import { type ButtonSize, type ButtonVariant, buttonClasses } from './button-variants'

interface LinkButtonProps extends LinkProps {
  variant?: ButtonVariant
  size?: ButtonSize
}

/** A react-router <Link> styled as a button — correct anchor semantics, no
 *  invalid <a>-inside-<button> nesting. */
export function LinkButton({ variant, size, className, ...rest }: LinkButtonProps) {
  return <Link className={buttonClasses({ variant, size, className })} {...rest} />
}
