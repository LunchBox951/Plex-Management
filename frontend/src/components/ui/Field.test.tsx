import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Field } from './Field'

describe('Field', () => {
  it('keeps the default form appearance and forwards native input attributes', () => {
    render(
      <Field
        id="server-url"
        label="Server URL"
        type="url"
        required
        autoComplete="off"
        placeholder="http://localhost"
        onChange={vi.fn()}
      />,
    )

    const input = screen.getByLabelText('Server URL')
    expect(input).toHaveAttribute('id', 'server-url')
    expect(input).toHaveAttribute('type', 'url')
    expect(input).toBeRequired()
    expect(input).toHaveAttribute('autocomplete', 'off')
    expect(input).toHaveAttribute('placeholder', 'http://localhost')
    expect(input).toHaveClass('bg-bg', 'text-sm')
    expect(input).not.toHaveClass('bg-surface-deep', 'font-mono')
    expect(screen.getByText('Server URL')).toHaveClass('text-sm', 'font-medium', 'text-muted')
  })

  it('uses the compact admin label and recessed mono input appearance', () => {
    render(<Field label="API key" appearance="admin" />)

    const input = screen.getByLabelText('API key')
    expect(input).toHaveClass('bg-surface-deep', 'font-mono', 'text-xs')
    expect(input).not.toHaveClass('bg-bg', 'text-sm')
    expect(screen.getByText('API key')).toHaveClass(
      'font-mono',
      'text-[10.5px]',
      'uppercase',
      'tracking-[0.12em]',
      'text-faint',
    )
  })

  it('associates generated IDs with the label and visible hint', () => {
    render(<Field label="Token" hint="Leave blank to keep the saved token." />)

    const input = screen.getByLabelText('Token')
    const hint = screen.getByText('Leave blank to keep the saved token.')
    expect(input).toHaveAttribute('id')
    expect(hint).toHaveAttribute('id')
    expect(input).toHaveAttribute('aria-describedby', hint.id)
    expect(input).not.toHaveAttribute('aria-invalid')
  })

  it('links errors in preference to hints and exposes invalid state', () => {
    render(<Field label="Password" hint="Optional" error="Password is required" />)

    const input = screen.getByLabelText('Password')
    const error = screen.getByText('Password is required')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAttribute('aria-describedby', error.id)
    expect(screen.queryByText('Optional')).not.toBeInTheDocument()
  })
})
