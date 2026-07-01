import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi, type Mock } from 'vitest'
import { useRequests } from '../api/hooks'
import type { RequestResponse } from '../api/types'
import { Requests } from './Requests'

vi.mock('../api/hooks', () => ({ useRequests: vi.fn() }))

function movieRequest(overrides: Partial<RequestResponse> = {}): RequestResponse {
  return {
    id: 1,
    tmdb_id: 42,
    media_type: 'movie',
    title: 'Test Movie',
    status: 'downloading',
    is_anime: false,
    ...overrides,
  }
}

function tvRequest(overrides: Partial<RequestResponse> = {}): RequestResponse {
  return {
    id: 2,
    tmdb_id: 100,
    media_type: 'tv',
    title: 'Test Show',
    status: 'partially_available',
    is_anime: false,
    seasons: [
      { season_number: 1, status: 'available' },
      { season_number: 2, status: 'downloading' },
    ],
    ...overrides,
  }
}

describe('Requests — per-season status list', () => {
  it('shows only the show-level status for a movie row (no per-season list)', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest()] },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    // The overall status renders...
    expect(screen.getByText(/downloading/i)).toBeInTheDocument()
    // ...but there is no per-season badge (movies carry no seasons at all).
    expect(screen.queryByText(/S1/)).not.toBeInTheDocument()
    expect(screen.queryByText(/S2/)).not.toBeInTheDocument()
  })

  it('lists every tracked season, each with its OWN status, for a tv row', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [tvRequest()] },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    // The show-level rollup...
    expect(screen.getByText(/partially available/i)).toBeInTheDocument()
    // ...alongside each season's own status.
    expect(screen.getByText(/S1/)).toBeInTheDocument()
    expect(screen.getByText(/S2/)).toBeInTheDocument()
  })

  it('renders no per-season list for a tv row with no tracked seasons yet', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [tvRequest({ seasons: [] })] },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    expect(screen.queryByText(/S1/)).not.toBeInTheDocument()
  })
})
