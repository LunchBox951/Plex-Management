import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PlexLogin } from './PlexLogin'

const h = vi.hoisted(() => ({
  start: vi.fn(),
  assign: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useStartPlexLogin: () => ({ mutateAsync: h.start, isPending: false }),
}))

describe('PlexLogin', () => {
  beforeEach(() => {
    h.start.mockReset()
    h.assign.mockReset()
    h.start.mockResolvedValue({
      auth_url: 'https://app.plex.tv/auth#?code=ABCD',
      expires_at: '2026-07-04T12:00:00Z',
      state: 'state-123',
    })
    vi.stubGlobal('location', { assign: h.assign })
    sessionStorage.clear()
  })

  it('starts Plex login, stores the returned state, and redirects to Plex', async () => {
    render(<PlexLogin onUseAccessKey={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /sign in with plex/i }))

    await waitFor(() => expect(h.start).toHaveBeenCalledTimes(1))
    expect(sessionStorage.getItem('plexmgr.plexLoginState')).toBe('state-123')
    expect(h.assign).toHaveBeenCalledWith('https://app.plex.tv/auth#?code=ABCD')
  })

  it('offers access-key recovery as a secondary path', () => {
    const onUseAccessKey = vi.fn()

    render(<PlexLogin onUseAccessKey={onUseAccessKey} />)
    fireEvent.click(screen.getByRole('button', { name: /use access key/i }))

    expect(onUseAccessKey).toHaveBeenCalledTimes(1)
  })
})
