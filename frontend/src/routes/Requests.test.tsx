import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useAuthMe, useRequests, useSearchPreview } from '../api/hooks'
import type { RequestResponse, SearchPreviewResponse } from '../api/types'
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

const ADMIN_AUTH = {
  data: { authenticated: true, auth_method: 'api_key', is_admin: true, user: null },
  isLoading: false,
}

const EMPTY_PREVIEW: SearchPreviewResponse = {
  accepted: [],
  rejected: [],
  no_acceptable_release: true,
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(useAuthMe as unknown as Mock).mockReturnValue(ADMIN_AUTH)
  ;(useSearchPreview as unknown as Mock).mockReturnValue({
    mutateAsync: vi.fn().mockResolvedValue(EMPTY_PREVIEW),
    isPending: false,
  })
})

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

describe('Requests — episode-fallback "N/M" badge (ADR-0020, issue #178)', () => {
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

describe('Requests — truthful inline download progress', () => {
  it('renders known 42% and known 0% with labelled progressbars', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ id: 1, title: 'Forty Two', download_progress: 0.42 }),
          movieRequest({ id: 2, title: 'Known Zero', download_progress: 0 }),
        ],
      },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })

    expect(screen.getByRole('progressbar', { name: 'Download progress for Forty Two' })).toHaveAttribute(
      'aria-valuenow',
      '42',
    )
    expect(screen.getByRole('progressbar', { name: 'Download progress for Known Zero' })).toHaveAttribute(
      'aria-valuenow',
      '0',
    )
    expect(screen.getByText('42%')).toBeInTheDocument()
    expect(screen.getByText('0%')).toBeInTheDocument()
  })

  it('omits progress when absent/ambiguous or when the request is not downloading', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ id: 1, title: 'Unknown', download_progress: null }),
          movieRequest({
            id: 2,
            title: 'Not Downloading',
            status: 'searching',
            download_progress: 0.42,
          }),
        ],
      },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.queryByText('42%')).not.toBeInTheDocument()
  })

  it('uses the shared progress convention to clamp out-of-range values', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ download_progress: 1.4 })] },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '100')
    expect(screen.getByText('100%')).toBeInTheDocument()
  })
})

describe('Requests — admin no-release shortcut', () => {
  it('opens the modal and runs its one shared preview path exactly once', async () => {
    const previewMutation = vi.fn().mockResolvedValue(EMPTY_PREVIEW)
    ;(useSearchPreview as unknown as Mock).mockReturnValue({
      mutateAsync: previewMutation,
      isPending: false,
    })
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [movieRequest({ id: 17, status: 'no_acceptable_release' })],
      },
      isLoading: false,
      isError: false,
    })
    const view = render(<Requests />, { wrapper: MemoryRouter })

    const details = screen.getByRole('button', { name: 'Open details for Test Movie' })
    const shortcut = screen.getByRole('button', { name: 'Re-search Test Movie' })
    expect(details.contains(shortcut)).toBe(false)
    expect(shortcut.contains(details)).toBe(false)

    fireEvent.click(shortcut)
    expect(screen.getByRole('heading', { name: 'Test Movie' })).toBeInTheDocument()
    await waitFor(() => expect(previewMutation).toHaveBeenCalledWith({ request_id: 17 }))
    expect(previewMutation).toHaveBeenCalledTimes(1)

    // The same action token survives ordinary route/modal rerenders but is already
    // consumed, so the effect cannot fire a second API request.
    view.rerender(<Requests />)
    await waitFor(() => expect(previewMutation).toHaveBeenCalledTimes(1))
  })

  it('targets the row-resolved no-release TV season for an inline re-search', async () => {
    const previewMutation = vi.fn().mockResolvedValue(EMPTY_PREVIEW)
    ;(useSearchPreview as unknown as Mock).mockReturnValue({
      mutateAsync: previewMutation,
      isPending: false,
    })
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          tvRequest({
            id: 23,
            status: 'no_acceptable_release',
            seasons: [
              { season_number: 1, status: 'available' },
              { season_number: 2, status: 'no_acceptable_release' },
            ],
          }),
        ],
      },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })

    fireEvent.click(screen.getByRole('button', { name: 'Re-search Test Show' }))
    await waitFor(() =>
      expect(previewMutation).toHaveBeenCalledWith({ request_id: 23, season: 2 }),
    )
    expect(previewMutation).toHaveBeenCalledTimes(1)
  })

  it('is hidden for shared/unknown users and for statuses other than no-release', () => {
    ;(useAuthMe as unknown as Mock).mockReturnValue({ data: undefined, isLoading: true })
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ id: 1, status: 'no_acceptable_release' }),
          movieRequest({ id: 2, title: 'Searching Movie', status: 'searching' }),
        ],
      },
      isLoading: false,
      isError: false,
    })
    render(<Requests />, { wrapper: MemoryRouter })
    expect(screen.queryByRole('button', { name: /re-search/i })).not.toBeInTheDocument()
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
    fireEvent.click(screen.getByRole('button', { name: 'Open details for Test Movie' }))
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
    fireEvent.click(screen.getByRole('button', { name: 'Open details for Test Show' }))
    expect(screen.getByRole('heading', { name: 'Test Show' })).toBeInTheDocument()
  })

  it('supports native Enter and Space activation on the details control', async () => {
    const user = userEvent.setup()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest()] },
      isLoading: false,
      isError: false,
    })
    const enterView = render(<Requests />, { wrapper: MemoryRouter })
    let details = screen.getByRole('button', { name: 'Open details for Test Movie' })

    details.focus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('heading', { name: 'Test Movie' })).toBeInTheDocument()
    enterView.unmount()

    render(<Requests />, { wrapper: MemoryRouter })
    details = screen.getByRole('button', { name: 'Open details for Test Movie' })
    details.focus()
    await user.keyboard(' ')
    expect(screen.getByRole('heading', { name: 'Test Movie' })).toBeInTheDocument()
  })
})
