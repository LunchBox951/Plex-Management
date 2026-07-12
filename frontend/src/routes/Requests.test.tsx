import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi, type Mock } from 'vitest'
import { useRequests } from '../api/hooks'
import type { RequestResponse } from '../api/types'
import { Requests } from './Requests'

// The row-click path mounts the shared `TitleDetailModal`, which calls every
// hook below (plus useToast) unconditionally before its own `if (!title)`
// guard — so the mock must cover the modal's full hook surface, not just
// `useRequests`, or an unmocked hook returns undefined and the modal throws
// before the read-only cases below even get a chance to NOT mount it.
vi.mock('../api/hooks', () => ({
  useRequests: vi.fn(),
  // Admin context: these route tests exercise the full (admin) modal surface.
  useAuthMe: vi.fn(() => ({
    data: { authenticated: true, auth_method: 'api_key', is_admin: true, user: null },
    isLoading: false,
  })),
  useQueue: vi.fn(() => ({ data: { queue: [] } })),
  useCreateRequest: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useSearchPreview: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useGrab: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useMarkFailed: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useImportDownload: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useSetKeepForever: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useReportIssue: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useCancelRequest: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
}))

vi.mock('../components/ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }))

// Passthrough Dialog so the modal's content renders as plain DOM — no Radix
// portal focus-trap noise in a jsdom test environment.
vi.mock('../components/ui/Dialog', () => ({
  Dialog: ({ title, children }: { title: string; children: ReactNode }) => (
    <div>
      <h2>{title}</h2>
      {children}
    </div>
  ),
}))

function movieRequest(overrides: Partial<RequestResponse> = {}): RequestResponse {
  return {
    id: 1,
    tmdb_id: 42,
    media_type: 'movie',
    title: 'Test Movie',
    status: 'downloading',
    is_anime: false,
    keep_forever: false,
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
    keep_forever: false,
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

describe('Requests — episode-fallback "N/M" badge (ADR-0018, issue #178)', () => {
  it('renders "S1 2/3" when a season is partially imported', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          tvRequest({
            seasons: [
              {
                season_number: 1,
                status: 'downloading',
                imported_episode_count: 2,
                target_episode_count: 3,
              },
            ],
          }),
        ],
      },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    expect(screen.getByText(/S1 2\/3/)).toBeInTheDocument()
  })

  it('renders plain "S1" when the counts are absent (unseeded season)', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          tvRequest({
            seasons: [{ season_number: 1, status: 'downloading' }],
          }),
        ],
      },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    expect(screen.getByText(/S1$/)).toBeInTheDocument()
    expect(screen.queryByText(/S1 \d+\/\d+/)).not.toBeInTheDocument()
  })

  it('renders plain "S1" once imported reaches target (nothing left to distinguish)', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          tvRequest({
            seasons: [
              {
                season_number: 1,
                status: 'completed',
                imported_episode_count: 3,
                target_episode_count: 3,
              },
            ],
          }),
        ],
      },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    expect(screen.getByText(/S1$/)).toBeInTheDocument()
  })
})

describe('Requests — poster rendering (issue #26)', () => {
  // Decorative posters carry alt="" (implicit role "presentation", not "img"),
  // so query the DOM node directly rather than by accessible role.
  it('renders an <img> with the poster_url when present', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [movieRequest({ poster_url: 'https://image.tmdb.org/t/p/w200/poster.jpg' })],
      },
      isLoading: false,
      isError: false,
    })
    const { container } = render(<Requests />, { wrapper: MemoryRouter })
    const img = container.querySelector('img')
    expect(img).toHaveAttribute('src', 'https://image.tmdb.org/t/p/w200/poster.jpg')
  })

  it('renders the gradient placeholder when poster_url is null', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ poster_url: null })] },
      isLoading: false,
      isError: false,
    })
    const { container } = render(<Requests />, { wrapper: MemoryRouter })
    expect(container.querySelector('img')).not.toBeInTheDocument()
  })

  it('falls back to the gradient placeholder when the poster image fails to load', async () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [movieRequest({ poster_url: 'https://image.tmdb.org/t/p/w200/broken.jpg' })],
      },
      isLoading: false,
      isError: false,
    })
    const { container } = render(<Requests />, { wrapper: MemoryRouter })
    const img = container.querySelector('img')
    expect(img).not.toBeNull()
    fireEvent.error(img as HTMLImageElement)
    await waitFor(() => expect(container.querySelector('img')).not.toBeInTheDocument())
  })
})

describe('Requests — opening the shared TitleDetailModal from a row', () => {
  it('clicking a movie row opens the modal on that title', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'no_acceptable_release' })] },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    fireEvent.click(screen.getByRole('button', { name: /test movie/i }))
    // The reused modal renders its Dialog title as the title's own name — the
    // co-located correction surface (re-search, retry-import, ...) is the same
    // component Discover uses, just opened from a request row instead.
    expect(screen.getByRole('heading', { name: 'Test Movie' })).toBeInTheDocument()
  })

  it('clicking a tv row opens the modal (media_type narrows "tv" correctly)', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [tvRequest()] },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    fireEvent.click(screen.getByRole('button', { name: /test show/i }))
    expect(screen.getByRole('heading', { name: 'Test Show' })).toBeInTheDocument()
  })
})
