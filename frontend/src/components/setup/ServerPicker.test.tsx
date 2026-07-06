import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { toApiError } from '../../lib/errors'
import type { PlexServersResponse, ServiceValidateResponse } from '../../api/types'
import { ServerPicker } from './ServerPicker'

const h = vi.hoisted(() => ({
  servers: vi.fn(),
  validate: vi.fn(),
}))

vi.mock('../../api/hooks', () => ({
  useSetupPlexServers: () => h.servers(),
  useValidatePlex: () => ({ mutateAsync: h.validate, isPending: false }),
}))

const APOLLO: PlexServersResponse = {
  servers: [
    {
      name: 'Apollo',
      machine_identifier: 'MID-APOLLO',
      connections: [
        // Deliberately listed unreachable-first to prove the picker RANKS
        // reachable + local connections to the top (the default selection).
        {
          uri: 'http://1.2.3.4:32400',
          local: false,
          relay: false,
          status: 'unreachable',
          error_code: 'server_unreachable_from_backend',
        },
        { uri: 'http://127.0.0.1:32400', local: true, relay: false, status: 'ok' },
      ],
    },
  ],
}

function serversLoaded(data: PlexServersResponse = APOLLO) {
  h.servers.mockReturnValue({ data, isLoading: false, isError: false, error: null })
}

function plexOk(overrides: Partial<ServiceValidateResponse> = {}): ServiceValidateResponse {
  return {
    ok: true,
    message: 'Plex ok',
    machine_identifier: 'MID-APOLLO',
    libraries: [
      { path: '/media/movies', section_key: '1', section_type: 'movie', title: 'Movies', writable: true },
    ],
    ...overrides,
  }
}

describe('ServerPicker', () => {
  beforeEach(() => {
    h.servers.mockReset()
    h.validate.mockReset()
    serversLoaded()
  })

  it('lists each owned server connection, ranked reachable-first, with local/reachability annotations', () => {
    render(<ServerPicker onVerified={vi.fn()} />)

    const select = screen.getByLabelText('Plex server')
    expect(within(select).getByText('Apollo — http://127.0.0.1:32400 (local, reachable)')).toBeInTheDocument()
    expect(
      within(select).getByText('Apollo — http://1.2.3.4:32400 (remote, unreachable)'),
    ).toBeInTheDocument()

    // Ranked reachable-first: the local, reachable connection is the default value.
    expect((select as HTMLSelectElement).value).toBe('http://127.0.0.1:32400')
  })

  it('validates the picked server with no token and reports the verified selection upward', async () => {
    h.validate.mockResolvedValue(plexOk())
    const onVerified = vi.fn()
    render(<ServerPicker onVerified={onVerified} />)

    fireEvent.click(screen.getByRole('button', { name: /verify/i }))

    await waitFor(() => expect(h.validate).toHaveBeenCalledWith({ url: 'http://127.0.0.1:32400' }))
    await waitFor(() =>
      expect(onVerified).toHaveBeenCalledWith({
        url: 'http://127.0.0.1:32400',
        machine_identifier: 'MID-APOLLO',
        libraries: plexOk().libraries,
      }),
    )
  })

  it('validates a typed custom URL with an optional token override', async () => {
    h.validate.mockResolvedValue(plexOk({ machine_identifier: 'MID-CUSTOM' }))
    const onVerified = vi.fn()
    render(<ServerPicker onVerified={onVerified} />)

    fireEvent.click(screen.getByLabelText(/enter a custom server/i))
    fireEvent.change(screen.getByLabelText('Server URL'), {
      target: { value: 'http://custom:32400' },
    })
    fireEvent.change(screen.getByLabelText(/plex token/i), { target: { value: 'custom-token' } })
    fireEvent.click(screen.getByRole('button', { name: /verify/i }))

    await waitFor(() =>
      expect(h.validate).toHaveBeenCalledWith({ url: 'http://custom:32400', token: 'custom-token' }),
    )
    await waitFor(() =>
      expect(onVerified).toHaveBeenCalledWith({
        url: 'http://custom:32400',
        token: 'custom-token',
        machine_identifier: 'MID-CUSTOM',
        libraries: plexOk().libraries,
      }),
    )
  })

  it('renders AuthErrorCard and does not advance when the server is not owned', async () => {
    h.validate.mockRejectedValue(toApiError({ detail: 'server_not_owned' }, 403))
    const onVerified = vi.fn()
    render(<ServerPicker onVerified={onVerified} />)

    fireEvent.click(screen.getByRole('button', { name: /verify/i }))

    expect(
      await screen.findByText(
        'Your Plex account does not own this server. Pick a server you own, or sign in as the owner.',
      ),
    ).toBeInTheDocument()
    expect(onVerified).not.toHaveBeenCalled()
  })

  it('falls back to custom entry when the account owns no discoverable servers', () => {
    serversLoaded({ servers: [] })
    render(<ServerPicker onVerified={vi.fn()} />)

    // No select to pick from; the custom URL field is offered instead.
    expect(screen.queryByLabelText('Plex server')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Server URL')).toBeInTheDocument()
  })

  it('surfaces the real discovery error (not a misleading empty state), keeping custom entry available', () => {
    // A 409 plex_account_required (or a 5xx) is an ERROR, not "you own no
    // servers": show the honest AuthErrorCard, never the empty-state hint.
    h.servers.mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: toApiError({ detail: 'plex_account_required' }, 409),
    })
    render(<ServerPicker onVerified={vi.fn()} />)

    expect(
      screen.getByText('Server discovery needs a Plex-signed-in admin. Sign in with Plex first.'),
    ).toBeInTheDocument()
    // The dishonest "owns no auto-discoverable server" hint must NOT appear.
    expect(screen.queryByText(/owns no auto-discoverable server/i)).not.toBeInTheDocument()
    // Custom entry stays available so the operator is never stuck.
    expect(screen.getByLabelText('Server URL')).toBeInTheDocument()
  })
})
