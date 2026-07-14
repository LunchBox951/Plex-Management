import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuthMe, useLogout, useRequests } from '../api/hooks'
import type { AuthMeResponse, RequestListResponse, RequestResponse } from '../api/types'
import { Layout } from './Layout'

type MockAuth = { data: AuthMeResponse }
type MockRequests = { data: RequestListResponse | undefined; isError?: boolean }

const h = vi.hoisted(
  (): {
    logout: ReturnType<typeof vi.fn>
    auth: MockAuth
    requests: MockRequests
  } => ({
    logout: vi.fn(),
    auth: {
      data: { authenticated: true, auth_method: 'plex_session', is_admin: true, user: null },
    },
    requests: { data: { requests: [] } },
  }),
)

vi.mock('../api/hooks', () => ({
  useLogout: vi.fn(() => ({ mutateAsync: h.logout, isPending: false })),
  useAuthMe: vi.fn(() => h.auth),
  useRequests: vi.fn(() => h.requests),
}))

vi.mock('./HealthDot', () => ({
  HealthDot: () => <div>Health</div>,
}))

// SearchOverlay owns its detailed Radix/keyboard/query behavior in its focused
// suite; shell tests keep only the authenticated-header integration seam.
vi.mock('./SearchOverlay', () => ({
  SearchOverlay: () => (
    <button type="button" aria-label="Search TMDB to request">
      Search
    </button>
  ),
}))

function requestRow(id: number, status: RequestResponse['status']): RequestResponse {
  return {
    id,
    tmdb_id: id,
    media_type: 'movie',
    title: `Request ${id}`,
    status,
    is_anime: false,
    keep_forever: false,
    can_mutate: false,
    is_owner: false,
    can_withdraw: false,
    has_other_participants: false,
  }
}

function setRequests(data: RequestListResponse | undefined, isError = false) {
  h.requests = { data, isError }
  vi.mocked(useRequests).mockReturnValue(h.requests as never)
}

function renderAt(path = '/') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Layout />
    </MemoryRouter>,
  )
}

describe('Layout', () => {
  beforeEach(() => {
    h.logout.mockReset()
    h.logout.mockResolvedValue(undefined)
    vi.mocked(useLogout).mockReturnValue({ mutateAsync: h.logout, isPending: false } as never)
    h.auth = {
      data: { authenticated: true, auth_method: 'plex_session', is_admin: true, user: null },
    }
    vi.mocked(useAuthMe).mockReturnValue(h.auth as never)
    setRequests({ requests: [] })
    vi.mocked(useRequests).mockClear()
  })

  it('renders labelled user and administration navigation zones for admins', () => {
    renderAt()

    const userNav = screen.getByRole('navigation', { name: 'User' })
    expect(within(userNav).getByRole('link', { name: 'Discover' })).toBeInTheDocument()
    expect(within(userNav).getByRole('link', { name: 'Requests' })).toBeInTheDocument()

    const adminNav = screen.getByRole('navigation', { name: 'Administration' })
    for (const label of ['Queue', 'Status', 'Logs', 'Settings', 'Blocklist']) {
      expect(within(adminNav).getByRole('link', { name: label })).toBeInTheDocument()
    }
    expect(screen.getAllByRole('separator')).toHaveLength(1)
  })

  it('uses separate active treatments for user and administration links', () => {
    const view = renderAt('/requests')

    const requestsLink = screen.getByRole('link', { name: 'Requests' })
    expect(requestsLink).toHaveClass('bg-white/8', 'text-ink')
    expect(requestsLink).toHaveAttribute('aria-current', 'page')

    view.unmount()
    renderAt('/status')

    const statusLink = screen.getByRole('link', { name: 'Status' })
    expect(statusLink).toHaveClass('bg-gold/12', 'text-gold')
    expect(statusLink).toHaveAttribute('aria-current', 'page')
  })

  it('shows only the user zone and a non-interactive health indicator to shared Plex users', () => {
    h.auth = {
      data: {
        authenticated: true,
        auth_method: 'plex_session',
        is_admin: false,
        user: { id: 1, plex_id: 99, username: 'shared', is_admin: false },
      },
    }
    vi.mocked(useAuthMe).mockReturnValue(h.auth as never)

    renderAt()

    const userNav = screen.getByRole('navigation', { name: 'User' })
    expect(within(userNav).getByRole('link', { name: 'Discover' })).toBeInTheDocument()
    expect(within(userNav).getByRole('link', { name: 'Requests' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Administration' })).not.toBeInTheDocument()
    expect(screen.queryByRole('separator')).not.toBeInTheDocument()
    for (const label of ['Queue', 'Status', 'Logs', 'Settings', 'Blocklist']) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument()
    }
    expect(screen.getByText('Health').closest('a')).toBeNull()
    expect(screen.getByRole('button', { name: 'Search TMDB to request' })).toBeInTheDocument()
  })

  it('counts only in-flight request statuses and keeps stale data visible on error', () => {
    // In flight: searching / downloading / no_acceptable_release, plus the two
    // non-terminal states Codex flagged on #249 — completed ("Finalizing":
    // imported, before Plex confirms availability) and import_blocked (awaiting
    // the operator's retry/reject). Not started (pending, waiting_for_air_date)
    // and settled (available, partially_available, failed, cancelled, evicted)
    // stay out of the count.
    setRequests(
      {
        requests: [
          requestRow(1, 'searching'),
          requestRow(2, 'downloading'),
          requestRow(3, 'no_acceptable_release'),
          requestRow(4, 'completed'),
          requestRow(5, 'import_blocked'),
          requestRow(6, 'pending'),
          requestRow(7, 'waiting_for_air_date'),
          requestRow(8, 'available'),
          requestRow(9, 'partially_available'),
          requestRow(10, 'failed'),
          requestRow(11, 'cancelled'),
          requestRow(12, 'evicted'),
        ],
      },
      true,
    )

    renderAt('/requests')

    const requestsLink = screen.getByRole('link', {
      name: 'Requests, 5 active requests',
    })
    expect(within(requestsLink).getByText('5').parentElement).toHaveClass(
      'bg-gold',
      'text-gold-ink',
    )
    expect(useRequests).toHaveBeenCalledWith({ poll: true })
  })

  it('hides the request badge while unresolved and when the resolved count is zero', () => {
    setRequests(undefined)
    const view = renderAt('/requests')

    expect(screen.queryByText(/active requests?/)).not.toBeInTheDocument()

    view.unmount()
    setRequests({ requests: [requestRow(1, 'pending')] })
    renderAt('/requests')

    expect(screen.queryByText(/active requests?/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Requests' })).toBeInTheDocument()
  })

  it('links the health indicator to Status for admins', () => {
    renderAt()

    const healthLink = screen.getByRole('link', { name: 'Open system status: Health' })
    expect(healthLink).toHaveAttribute('href', '/status')
    expect(healthLink).toHaveAttribute('title', 'Open system status')
    expect(healthLink).toHaveClass('min-h-6', 'focus-visible:ring-2')
  })

  it('revokes the current session from the header', () => {
    renderAt()

    fireEvent.click(screen.getByRole('button', { name: /sign out/i }))

    expect(h.logout).toHaveBeenCalledTimes(1)
  })
})
