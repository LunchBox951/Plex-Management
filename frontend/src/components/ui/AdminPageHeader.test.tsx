import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AdminPageHeader } from './AdminPageHeader'

describe('AdminPageHeader', () => {
  it('exposes exactly one level-one heading with the supplied title', () => {
    render(<AdminPageHeader title="Download queue" />)

    const headings = screen.getAllByRole('heading', { level: 1 })
    expect(headings).toHaveLength(1)
    expect(headings[0]).toHaveTextContent('Download queue')
    expect(headings[0]).toHaveClass(
      'font-display',
      'text-[22px]',
      'leading-none',
      'font-extrabold',
      'text-ink',
    )
  })

  it('renders count, description, status, and native action controls when supplied', () => {
    render(
      <AdminPageHeader
        title="Download queue"
        count="2 active"
        description="Downloads currently managed by Plex Manager"
        status="Connected"
        actions={
          <>
            <button type="button">Refresh</button>
            <a href="/settings">Settings</a>
          </>
        }
      />,
    )

    const heading = screen.getByRole('heading', { level: 1 })
    const count = screen.getByText('2 active')
    const description = screen.getByText('Downloads currently managed by Plex Manager')
    const status = screen.getByRole('status')
    const action = screen.getByRole('button', { name: 'Refresh' })

    expect(heading.parentElement?.parentElement).toHaveClass('flex', 'flex-wrap')
    expect(count.tagName).toBe('SPAN')
    expect(count).toHaveClass('font-mono', 'text-xs', 'leading-none', 'font-medium', 'text-faint')
    expect(description.tagName).toBe('P')
    expect(description).toHaveClass('truncate', 'text-[12.5px]', 'leading-relaxed', 'text-faint')
    expect(status).toHaveTextContent('Connected')
    expect(status).toHaveClass('font-mono', 'text-[11px]', 'leading-none', 'text-faint')
    expect(status.parentElement).toHaveClass(
      'ml-auto',
      'max-w-full',
      'shrink-0',
      'flex-wrap',
      'justify-end',
    )
    expect(action).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/settings')
  })

  it('omits every optional wrapper when no optional content is supplied', () => {
    const { container } = render(<AdminPageHeader title="Download queue" />)

    const header = container.querySelector('header')
    expect(header).not.toBeNull()
    expect(header!.children).toHaveLength(1)
    expect(header!.firstElementChild?.children).toHaveLength(1)
    expect(screen.getByRole('heading', { level: 1 }).parentElement?.children).toHaveLength(1)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('gives the status slot polite, atomic live-region semantics', () => {
    render(<AdminPageHeader title="System status" status="All systems operational" />)

    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'polite')
    expect(screen.getByRole('status')).toHaveAttribute('aria-atomic', 'true')
  })

  it('appends an additive caller class to the semantic header', () => {
    const { container } = render(
      <AdminPageHeader title="System status" className="admin-page-context" />,
    )

    expect(container.querySelector('header')).toHaveClass(
      'flex',
      'flex-col',
      'admin-page-context',
    )
  })
})
