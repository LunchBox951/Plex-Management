import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SectionHeader } from './SectionHeader'

describe('SectionHeader', () => {
  it('renders the canonical level-two heading and forwards heading attributes', () => {
    render(
      <SectionHeader
        id="download-health"
        aria-describedby="download-health-description"
        className="section-context"
      >
        Download health
      </SectionHeader>,
    )

    const heading = screen.getByRole('heading', { level: 2, name: 'Download health' })
    expect(heading).toHaveAttribute('id', 'download-health')
    expect(heading).toHaveAttribute('aria-describedby', 'download-health-description')
    expect(heading).toHaveClass(
      'font-mono',
      'text-[10.5px]',
      'leading-none',
      'font-semibold',
      'uppercase',
      'tracking-[0.14em]',
      'text-faint',
      'section-context',
    )
  })
})
