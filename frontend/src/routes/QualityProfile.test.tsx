import { fireEvent, render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { QualityProfileResponse } from '../api/types'
import { QualityProfile } from './QualityProfile'

const h = vi.hoisted(() => ({
  data: null as QualityProfileResponse | null,
  isPending: false,
  error: null as Error | null,
  refetch: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useQualityProfile: () => ({
    data: h.data,
    isPending: h.isPending,
    isError: h.error !== null,
    error: h.error,
    refetch: h.refetch,
  }),
}))

const PROFILE: QualityProfileResponse = {
  id: 1,
  name: 'Default profile',
  cutoff_quality_id: 2,
  cutoff_name: 'WEBDL-1080p',
  upgrade_allowed: true,
  items: [
    {
      quality_id: 1,
      name: 'CAM',
      source: 'cam',
      resolution: 'unknown',
      allowed: false,
    },
    {
      quality_id: 2,
      name: 'WEBDL-1080p',
      source: 'webdl',
      resolution: '1080p',
      allowed: true,
    },
    {
      quality_id: 3,
      name: 'Bluray-2160p',
      source: 'bluray',
      resolution: '2160p',
      allowed: true,
    },
  ],
}

describe('QualityProfile', () => {
  beforeEach(() => {
    h.data = PROFILE
    h.isPending = false
    h.error = null
    h.refetch.mockReset()
  })

  it('uses the admin header with complete profile metadata and v1 copy', () => {
    render(<QualityProfile />)

    expect(screen.getByRole('heading', { level: 1, name: 'Quality profile' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(
      'Cutoff: WEBDL-1080p · Upgrades: allowed',
    )
    expect(screen.getByText(/Default profile\. This is the ordered quality profile/)).toHaveTextContent(
      'Read-only in v1.',
    )
    expect(screen.queryByText(/read-only in the alpha/i)).not.toBeInTheDocument()
  })

  it('preserves response order in a semantic list', () => {
    render(<QualityProfile />)

    const list = screen.getByRole('list')
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(3)
    expect(within(rows[0]!).getByText('CAM')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('WEBDL-1080p')).toBeInTheDocument()
    expect(within(rows[2]!).getByText('Bluray-2160p')).toBeInTheDocument()
  })

  it('pairs textual verdicts with decorative eight-pixel state dots', () => {
    render(<QualityProfile />)

    const rows = screen.getAllByRole('listitem')
    expect(within(rows[0]!).getByText('blocked')).toHaveClass('text-error')
    expect(within(rows[1]!).getByText('allowed')).toHaveClass('text-available')

    const blockedDot = rows[0]!.querySelector('[aria-hidden="true"]')
    const allowedDot = rows[1]!.querySelector('[aria-hidden="true"]')
    expect(blockedDot).toHaveClass('size-2', 'bg-error')
    expect(allowedDot).toHaveClass('size-2', 'bg-available')
  })

  it('marks the exact cutoff row and keeps responsive non-table layout classes', () => {
    render(<QualityProfile />)

    const chip = screen.getByText('CUTOFF')
    const row = chip.closest('li')
    expect(row).not.toBeNull()
    expect(row).toHaveClass(
      'px-[14px]',
      'py-[11px]',
      'grid',
      'sm:grid-cols-[auto_minmax(0,1fr)_auto_auto_4rem]',
      'border-l-2',
      'border-l-gold',
      'bg-gold/5',
    )
    expect(row).not.toHaveClass('border-l-transparent')
    expect(chip).toHaveClass('border-gold/40', 'uppercase', 'text-gold')
    expect(within(row!).getByText('webdl · 1080p')).toHaveClass('break-words')
  })

  it('shows a stable loading header and first-load spinner', () => {
    h.data = null
    h.isPending = true

    render(<QualityProfile />)

    expect(screen.getByRole('heading', { level: 1, name: 'Quality profile' })).toBeInTheDocument()
    expect(screen.getByText('Loading quality profile…')).toBeInTheDocument()
    expect(screen.getByRole('status', { name: 'Loading' })).toBeInTheDocument()
  })

  it('surfaces an actionable error and retries the same query', () => {
    h.data = null
    h.error = new Error('Profile service unavailable')

    render(<QualityProfile />)

    expect(screen.getByText("Couldn't load the quality profile")).toBeInTheDocument()
    expect(screen.getByText('Profile service unavailable')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(h.refetch).toHaveBeenCalledTimes(1)
  })
})
