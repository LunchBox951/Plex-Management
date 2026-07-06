import { fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useDiscoverHome,
  useDiscoverSearch,
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
// this suite actually drives (useDiscoverHome / useDiscoverSearch / useRequests /
// useRequestsInvalidated) are overridden per test.
vi.mock('../api/hooks', () => ({
  useDiscoverHome: vi.fn(),
  useDiscoverSearch: vi.fn(),
  useRequests: vi.fn(),
  useRequestsInvalidated: vi.fn(() => false),
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

function mockHome(items: DiscoverResult[] = [MOVIE]) {
  ;(useDiscoverHome as unknown as Mock).mockReturnValue({
    data: { spotlight: null, rows: [{ row_type: 'trending', title: 'Trending', items }] },
    isLoading: false,
    isError: false,
    dataUpdatedAt: Date.now(),
  })
  ;(useDiscoverSearch as unknown as Mock).mockReturnValue({
    data: undefined,
    isError: false,
    isFetching: false,
    dataUpdatedAt: 0,
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
  ;(useRequestsInvalidated as unknown as Mock).mockReturnValue(false)
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
      // The tile itself still opens the detail modal — the correction path.
      fireEvent.click(screen.getByRole('button', { name: /Fresh Show/ }))
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
