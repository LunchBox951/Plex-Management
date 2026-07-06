import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toApiError } from '../lib/errors'
import { PlexPinError } from '../lib/plexOAuth'
import { PlexLogin } from './PlexLogin'

const h = vi.hoisted(() => ({
  openPlexPopup: vi.fn(),
  runPlexPinFlow: vi.fn(),
  signIn: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  usePlexSignIn: () => ({ mutateAsync: h.signIn, isPending: false }),
}))

// Keep the real PlexPinError class (so `instanceof` in the component matches the
// error the test throws) and only stub the browser-side popup + poll functions.
vi.mock('../lib/plexOAuth', async () => {
  const actual = await vi.importActual<typeof import('../lib/plexOAuth')>('../lib/plexOAuth')
  return { ...actual, openPlexPopup: h.openPlexPopup, runPlexPinFlow: h.runPlexPinFlow }
})

/** The popup handle the click handler pre-opens; opaque to the mocked flow. */
const POPUP = { name: 'plex-auth' } as unknown as Window

describe('PlexLogin', () => {
  beforeEach(() => {
    h.openPlexPopup.mockReset()
    h.runPlexPinFlow.mockReset()
    h.signIn.mockReset()
    h.openPlexPopup.mockReturnValue(POPUP)
    h.runPlexPinFlow.mockResolvedValue('plex-token-xyz')
    h.signIn.mockResolvedValue({ authenticated: true, auth_method: 'plex_session', user: null })
  })

  it('opens the popup, runs the PIN flow, verifies the token, and fires onSignedIn', async () => {
    const onSignedIn = vi.fn()
    render(<PlexLogin onSignedIn={onSignedIn} onUseAccessKey={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /sign in with plex/i }))

    await waitFor(() => expect(h.signIn).toHaveBeenCalledWith({ auth_token: 'plex-token-xyz' }))
    expect(h.openPlexPopup).toHaveBeenCalledTimes(1)
    expect(h.runPlexPinFlow).toHaveBeenCalledWith(POPUP)
    await waitFor(() => expect(onSignedIn).toHaveBeenCalledTimes(1))
  })

  it('surfaces a blocked-popup failure with its specific message and never mints a session', async () => {
    h.openPlexPopup.mockReturnValue(null)
    h.runPlexPinFlow.mockRejectedValue(new PlexPinError('plex_popup_blocked'))
    const onSignedIn = vi.fn()
    render(<PlexLogin onSignedIn={onSignedIn} onUseAccessKey={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /sign in with plex/i }))

    expect(
      await screen.findByText(
        'Your browser blocked the Plex sign-in popup. Allow popups for this site and try again.',
      ),
    ).toBeInTheDocument()
    expect(h.signIn).not.toHaveBeenCalled()
    expect(onSignedIn).not.toHaveBeenCalled()
  })

  it('surfaces a backend rejection (no owned servers) from the sign-in call', async () => {
    h.signIn.mockRejectedValue(toApiError({ detail: 'no_owned_servers' }, 403))
    const onSignedIn = vi.fn()
    render(<PlexLogin onSignedIn={onSignedIn} onUseAccessKey={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: /sign in with plex/i }))

    expect(
      await screen.findByText(
        'Your Plex account does not own any Plex Media Server. Sign in with the account that owns the server this app should manage.',
      ),
    ).toBeInTheDocument()
    expect(onSignedIn).not.toHaveBeenCalled()
  })

  it('offers access-key recovery as a secondary path', () => {
    const onUseAccessKey = vi.fn()

    render(<PlexLogin onSignedIn={vi.fn()} onUseAccessKey={onUseAccessKey} />)
    fireEvent.click(screen.getByRole('button', { name: /use access key/i }))

    expect(onUseAccessKey).toHaveBeenCalledTimes(1)
  })
})
