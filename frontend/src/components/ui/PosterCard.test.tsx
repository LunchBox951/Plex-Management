import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PosterCard } from './PosterCard'

/**
 * The action slot (issue #42) sits INSIDE the card's own clickable div, which
 * opens the detail modal on click and on a keyboard Enter/Space (see
 * `PosterCard.tsx`'s `onKeyDown`). An action element that plays by the rules —
 * stopping propagation on both its `onClick` and its `onKeyDown` — must be
 * able to act without also triggering the card underneath it. These tests
 * pin that contract at the `PosterCard` level, independent of any one action
 * component (`QuickRequestButton` has its own tests for its own behaviour).
 */
function actionButton(onAction: () => void) {
  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation()
        onAction()
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.stopPropagation()
        }
      }}
    >
      Act
    </button>
  )
}

describe('PosterCard action slot', () => {
  it('renders the action slot only when provided', () => {
    const { rerender } = render(<PosterCard title="Movie" />)
    expect(screen.queryByRole('button', { name: 'Act' })).not.toBeInTheDocument()

    rerender(<PosterCard title="Movie" action={actionButton(() => {})} />)
    expect(screen.getByRole('button', { name: 'Act' })).toBeInTheDocument()
  })

  it('clicking the action button does not fire the card onClick', () => {
    const onClick = vi.fn()
    const onAction = vi.fn()
    render(<PosterCard title="Movie" onClick={onClick} action={actionButton(onAction)} />)

    fireEvent.click(screen.getByRole('button', { name: 'Act' }))

    expect(onAction).toHaveBeenCalledTimes(1)
    expect(onClick).not.toHaveBeenCalled()
  })

  it('Enter/Space on the focused action button does not open the card', () => {
    const onClick = vi.fn()
    render(<PosterCard title="Movie" onClick={onClick} action={actionButton(() => {})} />)

    const action = screen.getByRole('button', { name: 'Act' })
    fireEvent.keyDown(action, { key: 'Enter' })
    fireEvent.keyDown(action, { key: ' ' })

    expect(onClick).not.toHaveBeenCalled()
  })
})
