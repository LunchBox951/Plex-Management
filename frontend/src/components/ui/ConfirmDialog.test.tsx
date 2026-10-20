import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ConfirmDialog } from './ConfirmDialog'

describe('ConfirmDialog', () => {
  it('lists every side effect before anything happens', () => {
    render(
      <ConfirmDialog
        open
        onOpenChange={vi.fn()}
        title="Cancel Survivor?"
        effects={[
          { tone: 'risk', text: 'Files are deleted.' },
          { tone: 'undo', text: 'Ask again later.' },
        ]}
        confirmLabel="Cancel request"
        onConfirm={vi.fn()}
      />,
    )
    expect(screen.getByRole('dialog', { name: 'Cancel Survivor?' })).toBeInTheDocument()
    expect(screen.getByText('Files are deleted.')).toBeInTheDocument()
    expect(screen.getByText('Ask again later.')).toBeInTheDocument()
  })

  it('keeps the action disabled until the acknowledgement is ticked', () => {
    const onConfirm = vi.fn()
    render(
      <ConfirmDialog
        open
        onOpenChange={vi.fn()}
        title="Import anyway?"
        effects={[{ tone: 'risk', text: 'Wrong files may land in Plex.' }]}
        acknowledge="I've checked the files"
        confirmLabel="Import anyway"
        onConfirm={onConfirm}
      />,
    )
    const action = screen.getByRole('button', { name: 'Import anyway' })
    expect(action).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: "I've checked the files" }))
    expect(action).toBeEnabled()
    fireEvent.click(action)
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('Go back closes without confirming', () => {
    const onOpenChange = vi.fn()
    const onConfirm = vi.fn()
    render(
      <ConfirmDialog
        open
        onOpenChange={onOpenChange}
        title="Mark as bad?"
        effects={[]}
        confirmLabel="Mark as bad"
        onConfirm={onConfirm}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Go back' }))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(onConfirm).not.toHaveBeenCalled()
  })
})
