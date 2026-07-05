import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PosterCard } from './PosterCard'

/**
 * The action slot (issue #42) must coexist with the card's own details trigger
 * without creating nested interactive controls. These tests pin that contract at
 * the `PosterCard` level, independent of any one action component
 * (`QuickRequestButton` has its own tests for its own behaviour).
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

  it('keeps action controls outside the card details trigger', () => {
    const onClick = vi.fn()
    render(<PosterCard title="Movie" onClick={onClick} action={actionButton(() => {})} />)

    const details = screen.getByRole('button', { name: 'View details for Movie' })
    const action = screen.getByRole('button', { name: 'Act' })

    expect(details.tagName).toBe('BUTTON')
    expect(details).not.toContainElement(action)
    expect(action.closest('[role="button"]')).toBeNull()

    fireEvent.click(details)
    expect(onClick).toHaveBeenCalledTimes(1)
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
