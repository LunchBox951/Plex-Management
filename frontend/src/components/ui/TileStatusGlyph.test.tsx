import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { requestStatus } from '../../lib/status'
import { TileStatusGlyph } from './TileStatusGlyph'

/**
 * TileStatusGlyph (issue #135) is the compact icon that replaces the text
 * StatusBadge pill on a Discover/Row tile. These tests pin: (1) the
 * accessible name survives the swap from visible text to an icon, (2)
 * available vs. partially_available render visibly distinct icons even
 * though they share one `StatusIntent`, and (3) the downloading state gets
 * the indeterminate bar and never a numeric percentage.
 */
describe('TileStatusGlyph', () => {
  it('carries the status label as its accessible name (role=img), not as visible text', () => {
    render(<TileStatusGlyph status={requestStatus('available')} />)
    expect(screen.getByRole('img', { name: 'In library' })).toBeInTheDocument()
  })

  it('renders a different icon path for "available" than for "partially_available"', () => {
    const { container: full } = render(<TileStatusGlyph status={requestStatus('available')} />)
    const { container: partial } = render(
      <TileStatusGlyph status={requestStatus('partially_available')} />,
    )

    const fullPaths = Array.from(full.querySelectorAll('svg path')).map((p) => p.getAttribute('d'))
    const partialPaths = Array.from(partial.querySelectorAll('svg path')).map((p) =>
      p.getAttribute('d'),
    )

    expect(fullPaths).not.toEqual(partialPaths)
    // Both are still the "available" intent color (same green family).
    expect(full.querySelector('svg')).toHaveClass('text-available')
    expect(partial.querySelector('svg')).toHaveClass('text-available')
  })

  it('renders the pending clock icon for "Requested" and a different icon for "Searching"', () => {
    const { container: pending } = render(<TileStatusGlyph status={requestStatus('pending')} />)
    const { container: searching } = render(
      <TileStatusGlyph status={requestStatus('searching')} />,
    )

    const pendingSvg = pending.querySelector('svg')
    const searchingSvg = searching.querySelector('svg')
    expect(pendingSvg?.innerHTML).not.toBe(searchingSvg?.innerHTML)
  })

  it('gives the Discover-only "processing" fallback the same pending glyph as a plain Requested status', () => {
    // libraryStateToPresentation's processing case (tileState.ts) hands this
    // component `{ label: 'Requested', intent: 'searching' }` — same label as
    // pending but a different intent. It must still render as "waiting", not
    // the active-search pulse.
    const { container: processing } = render(
      <TileStatusGlyph status={{ label: 'Requested', intent: 'searching' }} />,
    )
    const { container: pending } = render(<TileStatusGlyph status={requestStatus('pending')} />)

    expect(processing.querySelector('svg')?.innerHTML).toBe(pending.querySelector('svg')?.innerHTML)
  })

  it('renders an indeterminate bar for downloading and never a numeric percentage anywhere', () => {
    const { container } = render(<TileStatusGlyph status={requestStatus('downloading')} />)
    // The bar is animated/indeterminate — Discover has no queue-progress data
    // to show a real number, and a fabricated one would violate "honesty over
    // silence".
    expect(container.querySelector('.animate-pulse.bg-downloading')).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/%|\d/)
  })

  it('does not render the indeterminate bar for non-downloading states', () => {
    const { container } = render(<TileStatusGlyph status={requestStatus('available')} />)
    expect(container.querySelector('.bg-downloading')).not.toBeInTheDocument()
  })

  it('applies the error color for a blocked-import status', () => {
    const { container } = render(<TileStatusGlyph status={requestStatus('import_blocked')} />)
    expect(container.querySelector('svg')).toHaveClass('text-error')
  })
})
