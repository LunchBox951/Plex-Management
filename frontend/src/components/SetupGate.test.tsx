import { act, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AUTH_EXPIRED_EVENT, AUTH_INVALID_EVENT } from '../api/client'
import { queryKeys } from '../lib/queryClient'
import { SetupGate } from './SetupGate'

const h = vi.hoisted(() => ({
  setup: { data: { initialized: true, app_api_key: null }, isLoading: false, isError: false },
  auth: {
    data: { authenticated: false, auth_method: null as string | null, is_admin: false, user: null },
    isLoading: false,
  },
  invalidateQueries: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useSetupStatus: vi.fn(() => h.setup),
  useAuthMe: vi.fn(() => h.auth),
}))

vi.mock('./PlexLogin', () => ({
  PlexLogin: ({ onUseAccessKey }: { onUseAccessKey: () => void }) => (
    <div>
      <div>Mock Plex Login</div>
      <button onClick={onUseAccessKey}>Use access key</button>
    </div>
  ),
}))

vi.mock('./KeyEntry', () => ({
  KeyEntry: () => <div>Mock Key Entry</div>,
}))

vi.mock('./RealtimeProvider', () => ({
  RealtimeProvider: ({ children }: { children: ReactNode }) => (
    <div data-testid="realtime-provider">{children}</div>
  ),
}))

vi.mock('./ui/feedback', () => ({
  CenteredSpinner: ({ label }: { label: string }) => <div>{label}</div>,
  StateMessage: ({ title, action }: { title: string; action?: ReactNode }) => (
    <div>
      <div>{title}</div>
      {action}
    </div>
  ),
}))

vi.mock('./ui/Button', () => ({
  Button: ({ children }: { children: ReactNode }) => <button>{children}</button>,
}))

vi.mock('@tanstack/react-query', async () => {
  const actual =
    await vi.importActual<typeof import('@tanstack/react-query')>('@tanstack/react-query')
  return {
    ...actual,
    useQueryClient: () => ({ invalidateQueries: h.invalidateQueries }),
  }
})

describe('SetupGate auth routing', () => {
  beforeEach(() => {
    h.setup = { data: { initialized: true, app_api_key: null }, isLoading: false, isError: false }
    h.auth = {
      data: { authenticated: false, auth_method: null, is_admin: false, user: null },
      isLoading: false,
    }
    h.invalidateQueries.mockClear()
  })

  it('shows Plex login for an initialized install without an authenticated session', () => {
    render(<SetupGate />, { wrapper: MemoryRouter })

    expect(screen.getByText('Mock Plex Login')).toBeInTheDocument()
    expect(screen.queryByTestId('realtime-provider')).not.toBeInTheDocument()
  })

  it('does not mount realtime for an authenticated shared user', () => {
    h.auth = {
      data: { authenticated: true, auth_method: 'plex_session', is_admin: false, user: null },
      isLoading: false,
    }

    render(<SetupGate />, { wrapper: MemoryRouter })

    expect(screen.queryByText('Mock Plex Login')).not.toBeInTheDocument()
    expect(screen.queryByTestId('realtime-provider')).not.toBeInTheDocument()
  })

  it('mounts realtime only after auth/me reports an authenticated admin', () => {
    h.auth = {
      data: { authenticated: true, auth_method: 'plex_session', is_admin: true, user: null },
      isLoading: false,
    }

    render(<SetupGate />, { wrapper: MemoryRouter })

    expect(screen.queryByText('Mock Plex Login')).not.toBeInTheDocument()
    expect(screen.getByTestId('realtime-provider')).toBeInTheDocument()
  })

  it('refetches auth state when a session-expired event fires', () => {
    h.auth = {
      data: { authenticated: true, auth_method: 'plex_session', is_admin: true, user: null },
      isLoading: false,
    }

    render(<SetupGate />, { wrapper: MemoryRouter })
    act(() => {
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT))
    })

    expect(h.invalidateQueries).toHaveBeenCalledWith({ queryKey: queryKeys.authMe })
  })

  it('shows KeyEntry after an access-key 401, even with cached authenticated state', () => {
    // Authenticated via a stored access key: /auth/me is cached authenticated:true,
    // so protected screens (an empty <Outlet/>) render — KeyEntry is NOT shown yet.
    h.auth = {
      data: { authenticated: true, auth_method: 'api_key', is_admin: true, user: null },
      isLoading: false,
    }

    render(<SetupGate />, { wrapper: MemoryRouter })
    expect(screen.queryByText('Mock Key Entry')).not.toBeInTheDocument()

    // The stored key is rejected (client clears it and fires AUTH_INVALID). The gate
    // must drop the now-stale cached auth AND show KeyEntry — not strand the user on
    // <Outlet/> (which would keep 401ing) or bounce them to the Plex login.
    act(() => {
      window.dispatchEvent(new Event(AUTH_INVALID_EVENT))
    })

    expect(h.invalidateQueries).toHaveBeenCalledWith({ queryKey: queryKeys.authMe })
    expect(screen.getByText('Mock Key Entry')).toBeInTheDocument()
    expect(screen.queryByText('Mock Plex Login')).not.toBeInTheDocument()
    expect(screen.queryByTestId('realtime-provider')).not.toBeInTheDocument()
  })
})
