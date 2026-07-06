import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  PlexLibraryOption,
  PlexServersResponse,
  ServiceValidateResponse,
} from '../api/types'
import { SetupWizard } from './SetupWizard'

const h = vi.hoisted(() => ({
  validate: vi.fn(), // useValidateService — prowlarr/qbittorrent/tmdb cards
  validatePlex: vi.fn(), // useValidatePlex — the ServerPicker probe
  complete: vi.fn(),
  navigate: vi.fn(),
  authMeRefetch: vi.fn(),
  setSetupToken: vi.fn(),
  clearSetupToken: vi.fn(),
  authenticated: false,
  servers: { servers: [] } as PlexServersResponse,
  initialized: false,
  setupTokenRequired: false,
}))

vi.mock('../api/hooks', () => ({
  useSetupStatus: () => ({
    data: { initialized: h.initialized, setup_token_required: h.setupTokenRequired },
    isLoading: false,
  }),
  useAuthMe: () => ({
    data: { authenticated: h.authenticated, auth_method: 'plex_session', user: null },
    isLoading: false,
    refetch: h.authMeRefetch,
  }),
  useSetupPlexServers: () => ({
    data: h.servers,
    isLoading: false,
    isError: false,
    error: null,
  }),
  useValidatePlex: () => ({ mutateAsync: h.validatePlex, isPending: false }),
  useValidateService: () => ({ mutateAsync: h.validate, isPending: false }),
  useCompleteSetup: () => ({ mutateAsync: h.complete, isPending: false }),
  // PlexLogin (rendered by the sign-in step) calls this.
  usePlexSignIn: () => ({ mutateAsync: vi.fn(), isPending: false }),
}))

vi.mock('../lib/apiKey', () => ({
  setSetupToken: h.setSetupToken,
  clearSetupToken: h.clearSetupToken,
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}))

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom')
  return { ...actual, useNavigate: () => h.navigate }
})

const Wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

const movieLibrary: PlexLibraryOption = {
  path: '/media/movies',
  section_key: '1',
  section_type: 'movie',
  title: 'Movies',
  writable: true,
}

const tvLibrary: PlexLibraryOption = {
  path: '/media/tv',
  section_key: '2',
  section_type: 'tv',
  title: 'TV Shows',
  writable: true,
}

const SERVERS: PlexServersResponse = {
  servers: [
    {
      name: 'Apollo',
      machine_identifier: 'MID-APOLLO',
      connections: [{ uri: 'http://127.0.0.1:32400', local: true, relay: false, status: 'ok' }],
    },
  ],
}

function plexVerifyOk(libraries: PlexLibraryOption[]): ServiceValidateResponse {
  return { ok: true, message: 'Plex ok', machine_identifier: 'MID-APOLLO', libraries }
}

function resetMocks() {
  h.validate.mockReset()
  h.validatePlex.mockReset()
  h.complete.mockReset()
  h.navigate.mockReset()
  h.authMeRefetch.mockReset()
  h.setSetupToken.mockReset()
  h.clearSetupToken.mockReset()
  h.authenticated = false
  h.servers = { servers: [] }
  h.initialized = false
  h.setupTokenRequired = false
}

/** Sign in + pick + verify a server so a test lands on the services step. */
async function reachServices() {
  render(<SetupWizard />, { wrapper: Wrapper })
  fireEvent.click(await screen.findByRole('button', { name: /verify server/i }))
  await screen.findByText('Plex: http://127.0.0.1:32400 — verified ✓')
}

describe('SetupWizard — step machine', () => {
  beforeEach(resetMocks)

  it('shows the Plex sign-in first on a fresh, unauthenticated install', () => {
    render(<SetupWizard />, { wrapper: Wrapper })

    expect(screen.getByRole('button', { name: /sign in with plex/i })).toBeInTheDocument()
    // No access-key link pre-init (the wizard passes only onSignedIn).
    expect(screen.queryByRole('button', { name: /use access key/i })).not.toBeInTheDocument()
    // Neither the server picker nor the service cards are reachable yet.
    expect(screen.queryByLabelText('Plex server')).not.toBeInTheDocument()
    expect(screen.queryByText('Prowlarr')).not.toBeInTheDocument()
  })

  it('advances to the server picker once authenticated, listing owned servers', () => {
    h.authenticated = true
    h.servers = SERVERS
    render(<SetupWizard />, { wrapper: Wrapper })

    expect(screen.queryByRole('button', { name: /sign in with plex/i })).not.toBeInTheDocument()
    const select = screen.getByLabelText('Plex server')
    expect(
      within(select).getByText('Apollo — http://127.0.0.1:32400 (local, reachable)'),
    ).toBeInTheDocument()
  })

  it('verifying a picked server advances to the services step and shows the summary + change link', async () => {
    h.authenticated = true
    h.servers = SERVERS
    h.validatePlex.mockResolvedValue(plexVerifyOk([movieLibrary, tvLibrary]))
    render(<SetupWizard />, { wrapper: Wrapper })

    fireEvent.click(screen.getByRole('button', { name: /verify server/i }))

    await waitFor(() =>
      expect(h.validatePlex).toHaveBeenCalledWith({ url: 'http://127.0.0.1:32400' }),
    )
    expect(await screen.findByText('Plex: http://127.0.0.1:32400 — verified ✓')).toBeInTheDocument()
    expect(screen.getByText('Prowlarr')).toBeInTheDocument()
    // The Plex card itself is gone from the services step.
    expect(screen.queryByLabelText('Plex token')).not.toBeInTheDocument()

    // "Change" returns to the server step.
    fireEvent.click(screen.getByRole('button', { name: /change/i }))
    expect(screen.getByLabelText('Plex server')).toBeInTheDocument()
  })
})

describe('SetupWizard — services (library roots + completion)', () => {
  beforeEach(() => {
    resetMocks()
    h.authenticated = true
    h.servers = SERVERS
    h.validatePlex.mockResolvedValue(plexVerifyOk([movieLibrary, tvLibrary]))
    h.validate.mockImplementation(async ({ service }: { service: string }) => ({
      ok: true,
      message: `${service} ok`,
    }))
  })

  it('filters the movie picker to section_type "movie" and the tv picker to "tv"', async () => {
    await reachServices()

    const movieSelect = screen.getByLabelText('Movies library folder')
    const tvSelect = screen.getByLabelText('TV library folder')

    expect(within(movieSelect).getByText(/Movies —/)).toBeInTheDocument()
    expect(within(movieSelect).queryByText(/TV Shows/)).not.toBeInTheDocument()
    expect(within(tvSelect).getByText(/TV Shows —/)).toBeInTheDocument()
    expect(within(tvSelect).queryByText(/^Movies —/)).not.toBeInTheDocument()
  })

  it('never requires a tv library folder to be chosen (tv_root is optional)', async () => {
    await reachServices()
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(3))

    fireEvent.change(screen.getByLabelText('Movies library folder'), {
      target: { value: '/media/movies' },
    })

    expect(screen.getByLabelText('TV library folder')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('completes a tv-only install: tv folder chosen, movies left unset', async () => {
    await reachServices()
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(3))

    fireEvent.change(screen.getByLabelText('TV library folder'), { target: { value: '/media/tv' } })

    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('disables completion until at least one library root is chosen', async () => {
    await reachServices()
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(3))

    expect(screen.getByRole('button', { name: /complete setup/i })).toBeDisabled()
  })

  it('shows the tv section as optional when no folder is chosen', async () => {
    await reachServices()
    const tvSelect = screen.getByLabelText('TV library folder')
    const tvSection = tvSelect.closest('section')
    expect(tvSection).not.toBeNull()
    expect(within(tvSection!).getByText(/^optional$/i)).toBeInTheDocument()
  })

  it('reuses the Movies/TV Plex library lists for the anime pickers', async () => {
    await reachServices()

    const animeMovieSelect = screen.getByLabelText('Anime movies library folder')
    const animeTvSelect = screen.getByLabelText('Anime TV library folder')

    expect(within(animeMovieSelect).getByText(/Movies —/)).toBeInTheDocument()
    expect(within(animeMovieSelect).queryByText(/TV Shows/)).not.toBeInTheDocument()
    expect(within(animeTvSelect).getByText(/TV Shows —/)).toBeInTheDocument()
    expect(within(animeTvSelect).queryByText(/^Movies —/)).not.toBeInTheDocument()
  })

  it('completes an anime-only install: an anime root chosen, movies/tv left unset', async () => {
    await reachServices()
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(3))

    fireEvent.change(screen.getByLabelText('Anime movies library folder'), {
      target: { value: '/media/movies' },
    })

    expect((screen.getByLabelText('Movies library folder') as HTMLSelectElement).value).toBe('')
    expect((screen.getByLabelText('TV library folder') as HTMLSelectElement).value).toBe('')
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('submits chosen roots + the verified Plex machine identifier, then navigates home with no key screen', async () => {
    h.complete.mockResolvedValue({ initialized: true, setup_token_required: false })
    await reachServices()
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(3))

    fireEvent.change(screen.getByLabelText('Movies library folder'), {
      target: { value: '/media/movies' },
    })
    fireEvent.change(screen.getByLabelText('Anime movies library folder'), {
      target: { value: '/media/movies' },
    })

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: /complete setup/i }))

    await waitFor(() => expect(h.complete).toHaveBeenCalledTimes(1))
    const body = h.complete.mock.calls[0]![0] as Record<string, string>
    expect(body.plex_machine_identifier).toBe('MID-APOLLO')
    expect(body.plex_url).toBe('http://127.0.0.1:32400')
    expect(body.movies_root).toBe('/media/movies')
    expect(body.anime_movie_root).toBe('/media/movies')

    // No one-time key screen exists anymore — sign-in is the only credential.
    expect(screen.queryByText(/save your access key/i)).toBeNull()
    await waitFor(() => expect(h.navigate).toHaveBeenCalledWith('/', { replace: true }))
  })
})

describe('SetupWizard — service validation flow', () => {
  beforeEach(() => {
    resetMocks()
    h.authenticated = true
    h.servers = SERVERS
    h.validatePlex.mockResolvedValue(plexVerifyOk([movieLibrary]))
  })

  it('ignores a stale service validation success after its fields are edited', async () => {
    const pending = deferred<ServiceValidateResponse>()
    h.validate.mockReturnValueOnce(pending.promise)
    await reachServices()

    fireEvent.change(screen.getAllByLabelText('URL')[0]!, {
      target: { value: 'http://old-prowlarr:9696' },
    })
    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)
    fireEvent.change(screen.getAllByLabelText('URL')[0]!, {
      target: { value: 'http://new-prowlarr:9696' },
    })

    await act(async () => {
      pending.resolve({ ok: true, message: 'prowlarr ok' })
      await pending.promise
    })

    expect(screen.queryByText('prowlarr ok')).not.toBeInTheDocument()
    expect(screen.getByText('0/3 verified')).toBeInTheDocument()
  })

  it('keeps each service test disabled while its own validation is pending', async () => {
    const prowlarrPending = deferred<ServiceValidateResponse>()
    const qbPending = deferred<ServiceValidateResponse>()
    h.validate.mockImplementation(({ service }: { service: string }) =>
      service === 'prowlarr' ? prowlarrPending.promise : qbPending.promise,
    )
    await reachServices()

    const testButtons = screen.getAllByRole('button', { name: /test connection/i })
    fireEvent.click(testButtons[0]!)
    await waitFor(() => expect(testButtons[0]).toBeDisabled())

    fireEvent.click(testButtons[1]!)
    await waitFor(() => {
      expect(testButtons[0]).toBeDisabled()
      expect(testButtons[1]).toBeDisabled()
    })

    await act(async () => {
      prowlarrPending.resolve({ ok: true, message: 'prowlarr ok' })
      qbPending.resolve({ ok: true, message: 'qbittorrent ok' })
      await Promise.all([prowlarrPending.promise, qbPending.promise])
    })
  })
})

describe('SetupWizard — setup token (pre-init hardening)', () => {
  beforeEach(resetMocks)

  it('offers the setup-token field above the sign-in step and plumbs it to sessionStorage', () => {
    h.setupTokenRequired = true
    render(<SetupWizard />, { wrapper: Wrapper })

    // The field sits ABOVE the sign-in step, before any server/service work.
    expect(screen.getByLabelText('Setup token')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /sign in with plex/i })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Setup token'), { target: { value: 'boot-token' } })
    expect(h.setSetupToken).toHaveBeenCalledWith('boot-token')

    fireEvent.change(screen.getByLabelText('Setup token'), { target: { value: '' } })
    expect(h.clearSetupToken).toHaveBeenCalled()
  })

  it('does not show the setup-token field when the backend does not require it', () => {
    render(<SetupWizard />, { wrapper: Wrapper })
    expect(screen.queryByLabelText('Setup token')).not.toBeInTheDocument()
  })
})
