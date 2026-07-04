import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SetupGate } from './SetupGate'

const h = vi.hoisted(() => ({
  setup: { data: { initialized: true, app_api_key: null }, isLoading: false, isError: false },
  auth: {
    data: { authenticated: false, auth_method: null as string | null, user: null },
    isLoading: false,
  },
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
    useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  }
})

describe('SetupGate auth routing', () => {
  beforeEach(() => {
    h.setup = { data: { initialized: true, app_api_key: null }, isLoading: false, isError: false }
    h.auth = { data: { authenticated: false, auth_method: null, user: null }, isLoading: false }
  })

  it('shows Plex login for an initialized install without an authenticated session', () => {
    render(<SetupGate />, { wrapper: MemoryRouter })

    expect(screen.getByText('Mock Plex Login')).toBeInTheDocument()
  })

  it('skips Plex login once auth/me reports authenticated', () => {
    h.auth = { data: { authenticated: true, auth_method: 'plex_session', user: null }, isLoading: false }

    render(<SetupGate />, { wrapper: MemoryRouter })

    expect(screen.queryByText('Mock Plex Login')).not.toBeInTheDocument()
  })
})
