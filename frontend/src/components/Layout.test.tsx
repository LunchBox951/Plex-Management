import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
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
    unread: number
  } => ({
    logout: vi.fn(),
    auth: {
      data: { authenticated: true, auth_method: 'plex_session', is_admin: true, user: null },
    },
    requests: { data: { requests: [] } },
    unread: 0,
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

// SearchOverlay owns its detailed Radix/query behavior in its focused suite;
// shell tests keep only the integration seam: who opens it, and when.
vi.mock('./SearchOverlay', () => ({
  SearchOverlay: ({ open }: { open?: boolean }) =>
    open ? <div role="dialog" aria-label="Search dialog" /> : null,
}))

vi.mock('./notifications/NotificationBell', () => ({
  NotificationBell: () => (
    <button type="button" aria-label="Notifications">
      Bell
    </button>
  ),
}))

vi.mock('./notifications/useUnreadNotificationCount', () => ({
  useUnreadNotificationCount: () => h.unread,
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
    is_owner: true,
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
    h.unread = 0
    setRequests({ requests: [] })
    vi.mocked(useRequests).mockClear()
  })

  function setSharedUser() {
    h.auth = {
      data: {
        authenticated: true,
        auth_method: 'plex_session',
        is_admin: false,
        user: { id: 1, plex_id: 99, username: 'shared', is_admin: false },
      },
    }
    vi.mocked(useAuthMe).mockReturnValue(h.auth as never)
  }

  it('renders the watching nav and the Manage button for admins', () => {
    renderAt()

    const main = screen.getByRole('navigation', { name: 'Main' })
    for (const label of ['Home', 'Movies', 'TV', 'My requests']) {
      expect(within(main).getByRole('link', { name: label })).toBeInTheDocument()
    }
    expect(screen.getAllByRole('link', { name: /^Manage/ })[0]).toHaveAttribute('href', '/manage')
    // Manage's own tabs only appear in Manage mode.
    expect(screen.queryByRole('navigation', { name: 'Manage' })).not.toBeInTheDocument()
  })

  it('shows Manage mode tabs and System health on /manage paths', () => {
    renderAt('/manage/requests')

    const manage = screen.getByRole('navigation', { name: 'Manage' })
    for (const label of ['Needs attention', 'Requests', 'Downloads', 'Blocklist', 'Settings']) {
      expect(within(manage).getByRole('link', { name: label })).toBeInTheDocument()
    }
    expect(within(manage).getByRole('link', { name: 'Requests' })).toHaveAttribute('aria-current', 'page')
    expect(within(manage).getByRole('link', { name: /System health/ })).toHaveAttribute(
      'href',
      '/manage/health',
    )
    // The watching nav reads inactive while in Manage mode.
    expect(
      within(screen.getByRole('navigation', { name: 'Main' })).getByRole('link', { name: 'Home' }),
    ).not.toHaveClass('bg-white/8')
  })

  it('never shows Manage to shared Plex users', () => {
    setSharedUser()
    renderAt()

    expect(screen.queryByRole('link', { name: /^Manage/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Manage' })).not.toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Tabs' })).toBeInTheDocument()
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

    const main = screen.getByRole('navigation', { name: 'Main' })
    const requestsLink = within(main).getByRole('link', { name: 'My requests, 5 active requests' })
    expect(within(requestsLink).getByText('5').parentElement).toHaveClass('bg-gold', 'text-gold-ink')
    expect(useRequests).toHaveBeenCalledWith({ poll: true })
  })

  it('counts requests that need attention on the Manage button (admins only)', () => {
    setRequests({
      requests: [
        requestRow(1, 'import_blocked'),
        requestRow(2, 'no_acceptable_release'),
        requestRow(3, 'failed'),
        requestRow(4, 'downloading'),
      ],
    })
    renderAt()

    const manage = screen.getAllByRole('link', { name: /^Manage/ })[0]!
    expect(manage).toHaveAccessibleName('Manage, 2 need attention')
    // A round badge, never a key-cap shape.
    expect(within(manage).getByText('2').parentElement).toHaveClass('rounded-full', 'bg-error')
  })

  it('hides the request badge while unresolved and when the resolved count is zero', () => {
    setRequests(undefined)
    const view = renderAt('/requests')

    expect(screen.queryByText(/active requests?/)).not.toBeInTheDocument()

    view.unmount()
    setRequests({ requests: [requestRow(1, 'pending')] })
    renderAt('/requests')

    expect(screen.queryByText(/active requests?/)).not.toBeInTheDocument()
  })

  it('opens search from the phone Search tab', () => {
    renderAt()

    expect(screen.queryByRole('dialog', { name: 'Search dialog' })).not.toBeInTheDocument()
    fireEvent.click(
      within(screen.getByRole('navigation', { name: 'Tabs' })).getByRole('button', { name: 'Search' }),
    )
    expect(screen.getByRole('dialog', { name: 'Search dialog' })).toBeInTheDocument()
  })

  it('shows the unread notification count on the phone Notifications tab', () => {
    h.unread = 3
    renderAt()

    const tabs = screen.getByRole('navigation', { name: 'Tabs' })
    expect(within(tabs).getByRole('link', { name: /Notifications/ })).toHaveAccessibleName(
      /3 unread notifications/,
    )
  })

  it('signs out from the profile menu', async () => {
    renderAt()

    const profile = screen.getByRole('button', { name: 'Profile' })
    fireEvent.pointerDown(profile, { button: 0, ctrlKey: false, pointerType: 'mouse' })
    fireEvent.click(await screen.findByRole('menuitem', { name: /sign out/i }))

    expect(h.logout).toHaveBeenCalledTimes(1)
  })
})
