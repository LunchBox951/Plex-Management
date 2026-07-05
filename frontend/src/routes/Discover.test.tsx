import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useDiscoverHome,
  useDiscoverSearch,
  useRequests,
  useRequestsInvalidated,
} from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { Discover } from './Discover'

// Discover mounts the shared TitleDetailModal unconditionally (title=null), which
// calls its full hook surface before its own guard, and each unbadged tile mounts a
// QuickRequestButton (useCreateRequest + useToast). Stub the whole hooks module —
// same pattern as Requests.test.tsx — so nothing touches the network; the three
// hooks this suite actually drives (useDiscoverHome / useDiscoverSearch /
// useRequests) are overridden per test.
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

// An unrequested movie tile: library_state 'none' with no matching request row means
// deriveTileState(...) resolves to null, so ONLY the requests-freshness gate decides
// whether its quick-request action renders.
const UNREQUESTED: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 1,
  title: 'Fresh Movie',
  year: 2020,
  library_state: 'none',
}

// Exact-string match: the enclosing PosterCard role="button" folds this aria-label
// into its own name-from-content, so a loose /request/ regex would be ambiguous.
const REQUEST_ACTION = 'Request Fresh Movie'

function mockHome() {
  ;(useDiscoverHome as unknown as Mock).mockReturnValue({
    data: { spotlight: null, rows: [{ row_type: 'trending', title: 'Trending', items: [UNREQUESTED] }] },
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

describe('Discover — quick-request freshness gate (Codex P2)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockHome()
  })

  it('hides the quick-request action while the requests query is invalidated, even though derived state is null', () => {
    // The bug window: useCreateRequest has invalidated /requests after a
    // season-scoped tv request, but the refetch has not landed. The invalidated
    // flag is set, so the still-null tile must NOT expose a Request button (a click
    // would POST a seasons-less, whole-series body).
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [] },
      isFetched: true,
      dataUpdatedAt: Date.now(),
    })
    ;(useRequestsInvalidated as unknown as Mock).mockReturnValue(true)
    render(<Discover />)
    expect(screen.queryByRole('button', { name: REQUEST_ACTION })).not.toBeInTheDocument()
  })

  it('hides the quick-request action before the first requests fetch completes', () => {
    // No /requests fetch has settled yet: state derives null only for lack of data,
    // which is not proof the title is unrequested. Suppress until we actually know.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: undefined,
      isFetched: false,
      dataUpdatedAt: 0,
    })
    ;(useRequestsInvalidated as unknown as Mock).mockReturnValue(false)
    render(<Discover />)
    expect(screen.queryByRole('button', { name: REQUEST_ACTION })).not.toBeInTheDocument()
  })

  it('shows the quick-request action once the requests query has settled and the title is still unrequested', () => {
    // Fetched at least once and not invalidated: the null state is now trustworthy,
    // so the one-click Request is safe to offer.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [] },
      isFetched: true,
      dataUpdatedAt: Date.now(),
    })
    ;(useRequestsInvalidated as unknown as Mock).mockReturnValue(false)
    render(<Discover />)
    expect(screen.getByRole('button', { name: REQUEST_ACTION })).toBeInTheDocument()
  })
})
