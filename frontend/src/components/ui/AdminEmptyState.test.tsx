import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AdminEmptyState } from './AdminEmptyState'
import { adminRowPadding } from './adminStyles'

describe('AdminEmptyState', () => {
  it('renders the title and message as distinct blocks in a polite status region', () => {
    render(<AdminEmptyState title="Nothing queued" message="New downloads will appear here." />)

    const region = screen.getByRole('status')
    const title = within(region).getByText('Nothing queued')
    const message = within(region).getByText('New downloads will appear here.')

    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region.children).toHaveLength(2)
    expect(region).toHaveClass(
      'rounded-[10px]',
      'border',
      'border-dashed',
      'border-white/12',
      'px-6',
      'py-11',
      'text-center',
    )
    expect(title).not.toBe(message)
    expect(title.parentElement).toBe(region)
    expect(title).toHaveClass('text-[14px]', 'font-bold', 'text-muted')
    expect(message.parentElement).toBe(region)
    expect(message).toHaveClass('text-[13.5px]', 'text-faint')
  })

  it('preserves a semantic link in the message and appends an additive caller class', () => {
    render(
      <AdminEmptyState
        title="No blocked releases"
        message={<a href="/queue">Review the queue</a>}
        className="blocklist-context"
      />,
    )

    expect(screen.getByRole('link', { name: 'Review the queue' })).toHaveAttribute('href', '/queue')
    expect(screen.getByRole('status')).toHaveClass(
      'rounded-[10px]',
      'border-dashed',
      'blocklist-context',
    )
  })

  it('keeps the dense admin row padding contract exact', () => {
    expect(adminRowPadding).toBe('px-[14px] py-[11px]')
  })
})
