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

/**
 * The details trigger's accessible name must disambiguate remakes/duplicate
 * titles (e.g. "Dune" 1984 vs. 2021) the same way the rendered title/year
 * caption already does visually.
 *
 * Note: the trigger's focus ring is deliberately not pinned here via a
 * `className` assertion. jsdom doesn't lay out or clip box-shadows, so a
 * string match on Tailwind classes wouldn't verify the actual fix (the ring
 * no longer being clipped by the card's `overflow-hidden` wrapper) — it
 * would only assert today's class spelling, which is brittle theater that
 * breaks on any unrelated Tailwind refactor without catching a regression.
 */
describe('PosterCard details trigger label', () => {
  it('includes the year in the aria-label when provided', () => {
    render(<PosterCard title="Dune" year={2021} onClick={() => {}} />)

    expect(screen.getByRole('button', { name: 'View details for Dune (2021)' })).toBeInTheDocument()
  })

  it('omits the year from the aria-label when absent', () => {
    render(<PosterCard title="Dune" onClick={() => {}} />)

    expect(screen.getByRole('button', { name: 'View details for Dune' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /\(/ })).not.toBeInTheDocument()
  })
})
