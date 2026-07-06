import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AuthMeResponse } from '../api/types'
import { useAuthMe, useLogout } from '../api/hooks'
import { Layout } from './Layout'

type MockAuth = { data: AuthMeResponse }

const h = vi.hoisted(
  (): { logout: ReturnType<typeof vi.fn>; auth: MockAuth } => ({
    logout: vi.fn(),
    auth: {
      data: { authenticated: true, auth_method: 'plex_session', is_admin: true, user: null },
    },
  }),
)

vi.mock('../api/hooks', () => ({
  useLogout: vi.fn(() => ({ mutateAsync: h.logout, isPending: false })),
  useAuthMe: vi.fn(() => h.auth),
}))

vi.mock('./HealthDot', () => ({
  HealthDot: () => <div>Health</div>,
}))

describe('Layout', () => {
  beforeEach(() => {
    h.logout.mockReset()
    h.logout.mockResolvedValue(undefined)
    vi.mocked(useLogout).mockReturnValue({ mutateAsync: h.logout, isPending: false } as never)
    h.auth = {
      data: { authenticated: true, auth_method: 'plex_session', is_admin: true, user: null },
    }
    vi.mocked(useAuthMe).mockReturnValue(h.auth as never)
  })

  it('revokes the current session from the header', () => {
    render(<Layout />, { wrapper: MemoryRouter })

    fireEvent.click(screen.getByRole('button', { name: /sign out/i }))

    expect(h.logout).toHaveBeenCalledTimes(1)
  })

  it('hides admin navigation for shared Plex users', () => {
    h.auth = {
      data: {
        authenticated: true,
        auth_method: 'plex_session',
        is_admin: false,
        user: { id: 1, plex_id: 99, username: 'shared', is_admin: false },
      },
    }
    vi.mocked(useAuthMe).mockReturnValue(h.auth as never)

    render(<Layout />, { wrapper: MemoryRouter })

    expect(screen.getByRole('link', { name: 'Discover' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Requests' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Queue' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Status' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Logs' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Blocklist' })).not.toBeInTheDocument()
  })
})
