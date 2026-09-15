import type { ButtonHTMLAttributes } from 'react'

import Icon, { type IconName } from './Icon'
import { buttonClass, cx, type ButtonSize, type ButtonVariant } from './classes'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  /** Drawn before the label. */
  icon?: IconName
  /** Swaps the icon for a spinner and disables the button. */
  loading?: boolean
}

/**
 * The app's button.
 *
 * `type="button"` by default: almost every button here sits inside a form-ish
 * layout and must not submit it.
 */
export default function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  loading = false,
  disabled,
  className,
  children,
  type = 'button',
  ...rest
}: ButtonProps) {
  const glyph: IconName | undefined = loading ? 'spinner' : icon

  return (
    <button
      {...rest}
      type={type}
      disabled={disabled === true || loading}
      className={cx(buttonClass(variant, size), className)}
    >
      {glyph ? <Icon name={glyph} size={size === 'sm' ? 13 : 14} /> : null}
      {children}
    </button>
  )
}
