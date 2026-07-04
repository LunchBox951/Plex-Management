import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PlexCallback } from './PlexCallback'

const h = vi.hoisted(() => ({
  complete: vi.fn(),
  navigate: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useCompletePlexLogin: () => ({ mutateAsync: h.complete }),
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => h.navigate }
})

describe('PlexCallback', () => {
  beforeEach(() => {
    h.complete.mockReset()
    h.navigate.mockReset()
    h.complete.mockResolvedValue({ authenticated: true, auth_method: 'plex_session', user: null })
    sessionStorage.clear()
  })

  it('completes login from the state query and returns to the app', async () => {
    render(
      <MemoryRouter initialEntries={['/auth/plex/callback?state=state-123']}>
        <PlexCallback />
      </MemoryRouter>,
    )

    await waitFor(() => expect(h.complete).toHaveBeenCalledWith({ state: 'state-123' }))
    expect(h.navigate).toHaveBeenCalledWith('/', { replace: true })
  })

  it('falls back to the stored state when Plex returns without query params', async () => {
    sessionStorage.setItem('plexmgr.plexLoginState', 'stored-state')

    render(
      <MemoryRouter initialEntries={['/auth/plex/callback']}>
        <PlexCallback />
      </MemoryRouter>,
    )

    await waitFor(() => expect(h.complete).toHaveBeenCalledWith({ state: 'stored-state' }))
  })

  it('shows a retryable error when no state is available', async () => {
    render(
      <MemoryRouter initialEntries={['/auth/plex/callback']}>
        <PlexCallback />
      </MemoryRouter>,
    )

    expect(await screen.findByText("Couldn't complete Plex sign-in")).toBeInTheDocument()
    expect(h.complete).not.toHaveBeenCalled()
  })
})
