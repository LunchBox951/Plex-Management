import { act, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Outlet, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useDiscoverHome,
  useRequests,
  useRequestsInvalidated,
} from '../api/hooks'
import type { DiscoverResult, RequestResponse } from '../api/types'
import { resetSettleObservations } from '../lib/tileState'
import { Discover } from './Discover'

// Discover mounts the shared TitleDetailModal unconditionally (title=null), which
// calls its full hook surface before its own guard, and each unbadged tile mounts a
// QuickRequestButton (useCreateRequest + useToast). Stub the whole hooks module —
// same pattern as Requests.test.tsx — so nothing touches the network; the hooks
// this suite actually drives (useDiscoverHome / useRequests /
// useRequestsInvalidated) are overridden per test.
vi.mock('../api/hooks', () => ({
  useDiscoverHome: vi.fn(),
  useRequests: vi.fn(),
  useRequestsInvalidated: vi.fn(() => false),
  // Admin context for the shared modal's RBAC gating (same default as
  // Requests.test.tsx): this suite tests quick-request gating, not roles.
  useAuthMe: vi.fn(() => ({
    data: { authenticated: true, auth_method: 'api_key', is_admin: true, user: null },
    isLoading: false,
  })),
  useCreateRequest: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useQueue: vi.fn(() => ({ data: { queue: [] } })),
  useSearchPreview: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useGrab: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useMarkFailed: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useImportDownload: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useSetKeepForever: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useReportIssue: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useCancelRequest: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
}))

vi.mock('../components/ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }))

// Passthrough Dialog so the shared modal renders as plain DOM (no Radix portal
// focus-trap noise in jsdom) — same shim Requests.test.tsx uses.
vi.mock('../components/ui/Dialog', () => ({
  Dialog: ({ title, children }: { title: string; children: ReactNode }) => (
    <div>
      <h2>{title}</h2>
      {children}
    </div>
  ),
}))

// Unrequested tiles: library_state 'none' with no matching request row means
// deriveTileState(...) resolves to null, so ONLY the quickRequestable gate
// (freshness + tv first-time rule) decides whether the quick-request action renders.
const MOVIE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 1,
  title: 'Fresh Movie',
  year: 2020,
  library_state: 'none',
}

const SHOW: DiscoverResult = {
  media_type: 'tv',
  tmdb_id: 2,
  title: 'Fresh Show',
  year: 2021,
  library_state: 'none',
}

const HERO: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 3,
  title: 'Hero Movie',
  year: 2024,
  library_state: 'none',
}

// Exact-string matches select the quick-request action, not the card details
// button that includes the same title in its accessible name.
const REQUEST_MOVIE = 'Request Fresh Movie'
const REQUEST_SHOW = 'Request Fresh Show'

function requestRow(overrides: Partial<RequestResponse> = {}): RequestResponse {
  return {
    id: 1,
    tmdb_id: 2,
    media_type: 'tv',
    title: 'Fresh Show',
    status: 'failed',
    is_anime: false,
    keep_forever: false,
    ...overrides,
  }
}

function mockHome(items: DiscoverResult[] = [MOVIE], spotlights: DiscoverResult[] = []) {
  ;(useDiscoverHome as unknown as Mock).mockReturnValue({
    data: { spotlights, rows: [{ row_type: 'trending', title: 'Trending', items }] },
    isLoading: false,
    isError: false,
    dataUpdatedAt: Date.now(),
  })
}

function mockRequests(rows: RequestResponse[]) {
  ;(useRequests as unknown as Mock).mockReturnValue({
    data: { requests: rows },
    isSuccess: true,
    dataUpdatedAt: Date.now(),
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  // Discover runs the REAL deriveTileState, whose settle observations are
  // module-level state — reset between tests like tileState.test.ts does.
  resetSettleObservations()
  mockHome()
  mockRequests([])
  ;(useRequestsInvalidated as unknown as Mock).mockReturnValue(false)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Discover — hero-first home (issue #188)', () => {
  it('removes the page heading, subtitle, and inline search input', () => {
    render(<Discover />)

    expect(screen.queryByRole('heading', { name: 'Discover' })).not.toBeInTheDocument()
    expect(screen.queryByText('Search TMDB to request a movie or show.')).not.toBeInTheDocument()
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
  })

  it('renders Spotlight before the first home row on a successful response', () => {
    mockHome([MOVIE], [HERO])
    render(<Discover />)

    const hero = screen.getByRole('heading', { name: HERO.title }).closest('section')
    const firstRow = screen.getByRole('heading', { name: 'Trending' }).closest('section')
    expect(hero).not.toBeNull()
    expect(firstRow).not.toBeNull()
    expect(hero!.compareDocumentPosition(firstRow!) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0)
  })

  it('lets the first non-empty row lead when the server returns no spotlight', () => {
    const view = render(<Discover />)

    expect(view.container.firstElementChild?.firstElementChild?.firstElementChild).toBe(
      screen.getByRole('heading', { name: 'Trending' }).closest('section'),
    )
  })

  it('passes the header search-overlay state through to pause spotlight rotation', () => {
    vi.useFakeTimers()
    mockHome([MOVIE], [HERO, SHOW])
    render(
      <MemoryRouter>
        <Routes>
          <Route element={<Outlet context={{ searchOpen: true }} />}>
            <Route index element={<Discover />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    )

    act(() => vi.advanceTimersByTime(20_000))
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(HERO.title)
  })

  it('pauses rotation while the title modal is open', () => {
    vi.useFakeTimers()
    mockHome([MOVIE], [HERO, SHOW])
    render(<Discover />)

    fireEvent.click(screen.getByRole('button', { name: 'Details' }))
    act(() => vi.advanceTimersByTime(20_000))

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent(HERO.title)
  })
})

describe('Discover — quick-request freshness gate (Codex P2)', () => {
  it('hides the quick-request action while the requests query is invalidated, even though derived state is null', () => {
    // The bug window: useCreateRequest has invalidated /requests after a
    // season-scoped tv request, but the refetch has not landed. The invalidated
    // flag is set, so the still-null tile must NOT expose a Request button (a click
    // would POST a seasons-less, whole-series body).
    mockRequests([])
    ;(useRequestsInvalidated as unknown as Mock).mockReturnValue(true)
    render(<Discover />)
    expect(screen.queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
  })

  it('hides the quick-request action before the first requests fetch completes', () => {
    // No /requests fetch has succeeded yet: state derives null only for lack of
    // data, which is not proof the title is unrequested. Suppress until we know.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: undefined,
      isSuccess: false,
      dataUpdatedAt: 0,
    })
    render(<Discover />)
    expect(screen.queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
  })

  it('hides the quick-request action when the requests query is in ERROR state', () => {
    // isFetched would be true here (the fetch COMPLETED — with an error) while
    // data is still undefined, so every tile derives null with zero request
    // knowledge. The gate must demand a SUCCESSFUL fetch, not just a finished one.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: undefined,
      isSuccess: false,
      isError: true,
      isFetched: true,
      dataUpdatedAt: 0,
    })
    render(<Discover />)
    expect(screen.queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
  })

  it('shows the quick-request action once the requests query has settled and the title is still unrequested', () => {
    // Fetched successfully and not invalidated: the null state is now trustworthy,
    // so the one-click Request is safe to offer.
    mockRequests([])
    render(<Discover />)
    // getByRole (singular) doubles as a honesty check: the looping row exposes the
    // quick-request action exactly once, not one-per-clone.
    expect(screen.getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
  })
})

describe('Discover — tv quick-request is first-time only (Codex P2)', () => {
  it.each(['failed', 'cancelled', 'evicted'] as const)(
    'hides the quick-request action for a tv title whose request settled as %s (modal path still works)',
    (status) => {
      // The settled-bad row intentionally re-derives state === null (unbadged tile),
      // but a seasons-less POST from the tile would EXPAND the tracked set to the
      // whole aired series — where the modal's "Request again" deliberately narrows
      // to the selected season. The tile must offer nothing; retry goes via modal.
      mockHome([SHOW])
      mockRequests([
        requestRow({ status, seasons: [{ season_number: 5, status }] }),
      ])
      render(<Discover />)
      expect(screen.queryByRole('button', { name: REQUEST_SHOW })).not.toBeInTheDocument()
      // The tile itself still opens the detail modal — the correction path. The
      // real tile is the single reachable details trigger (clones are inert).
      fireEvent.click(screen.getByRole('button', { name: 'View details for Fresh Show (2021)' }))
      expect(screen.getByRole('heading', { name: 'Fresh Show' })).toBeInTheDocument()
    },
  )

  it('shows the quick-request action for a tv title with no request rows at all', () => {
    // True first-time request: whole-series tracking is exactly what the user asks
    // for. A same-tmdb_id MOVIE row must not suppress it (exact media_type
    // correlation, mirroring deriveTileState).
    mockHome([SHOW])
    mockRequests([
      requestRow({ tmdb_id: SHOW.tmdb_id, media_type: 'movie', title: 'Same-id Movie', status: 'failed' }),
    ])
    render(<Discover />)
    expect(screen.getByRole('button', { name: REQUEST_SHOW })).toBeInTheDocument()
  })

  it('keeps the quick-request action for a MOVIE with settled request history', () => {
    // Movies keep the scope-free behavior: a re-request after cancelled/evicted has
    // no season scope to corrupt, and the backend dedups an actually-active one.
    mockHome([MOVIE])
    mockRequests([
      requestRow({ tmdb_id: MOVIE.tmdb_id, media_type: 'movie', title: 'Fresh Movie', status: 'cancelled' }),
    ])
    render(<Discover />)
    expect(screen.getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
  })
})
