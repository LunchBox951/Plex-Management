import { act, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AUTH_EXPIRED_EVENT } from '../api/client'
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

  it('opens the break-glass KeyEntry from the Plex login access-key affordance', async () => {
    // Unauthenticated: the gate shows the Plex login, whose "Use access key" button
    // switches to the break-glass KeyEntry (the recovery-key exchange path). This is
    // now the ONLY route to KeyEntry — the browser no longer stores a key to have
    // rejected, so there is no AUTH_INVALID signal (CodeQL #263).
    render(<SetupGate />, { wrapper: MemoryRouter })
    expect(screen.getByText('Mock Plex Login')).toBeInTheDocument()
    expect(screen.queryByText('Mock Key Entry')).not.toBeInTheDocument()

    await act(async () => {
      screen.getByText('Use access key').click()
    })

    expect(screen.getByText('Mock Key Entry')).toBeInTheDocument()
    expect(screen.queryByText('Mock Plex Login')).not.toBeInTheDocument()
    expect(screen.queryByTestId('realtime-provider')).not.toBeInTheDocument()
  })
})
