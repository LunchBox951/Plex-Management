import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { Dialog, DialogClose, DialogTitle } from './Dialog'

describe('Dialog chrome', () => {
  it('keeps the default header, body gutter, width, hero, title, and close semantics', () => {
    const onOpenChange = vi.fn()
    render(
      <Dialog
        open
        onOpenChange={onOpenChange}
        title="Default title"
        description="Default description"
        hero={<div data-testid="hero">Hero</div>}
      >
        <p>Default body</p>
      </Dialog>,
    )

    const dialog = screen.getByRole('dialog', { name: 'Default title' })
    expect(dialog).toHaveClass('max-w-3xl', 'max-h-[90vh]', 'overflow-y-auto')
    expect(dialog).not.toHaveClass('max-w-[820px]')
    expect(dialog.querySelector('.p-6')).toBeInTheDocument()
    expect(screen.getByTestId('hero')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Default title' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('lets a caller place the real title and close without stock padding conflicts', () => {
    const onOpenChange = vi.fn()
    render(
      <Dialog
        open
        onOpenChange={onOpenChange}
        title="Custom title"
        description="Custom description"
        customChrome
      >
        <div data-testid="custom-body" className="px-4">
          <DialogClose aria-label="Close custom dialog">✕</DialogClose>
          <DialogTitle>Custom title</DialogTitle>
          <p>Custom body</p>
        </div>
      </Dialog>,
    )

    const dialog = screen.getByRole('dialog', { name: 'Custom title' })
    expect(dialog).toHaveClass('max-w-[820px]', 'max-h-[90vh]', 'overflow-y-auto')
    expect(dialog).not.toHaveClass('max-w-3xl')
    expect(dialog.querySelector('.p-6')).not.toBeInTheDocument()
    expect(screen.getByTestId('custom-body')).toHaveClass('px-4')
    expect(screen.getAllByRole('heading', { level: 2 })).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Close custom dialog' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})
