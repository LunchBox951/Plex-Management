import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  PlexLibraryOption,
  PlexServersResponse,
  ServiceValidateResponse,
} from '../api/types'
import { toApiError } from '../lib/errors'
import { SetupWizard, WIZARD_STEPS } from './SetupWizard'

const h = vi.hoisted(() => ({
  validate: vi.fn(), // useValidateService — prowlarr/qbittorrent/tmdb cards
  validatePlex: vi.fn(), // useValidatePlex — the ServerPicker probe
  complete: vi.fn(),
  navigate: vi.fn(),
  authMeRefetch: vi.fn(),
  setSetupToken: vi.fn(),
  clearSetupToken: vi.fn(),
  toast: vi.fn(),
  authenticated: false,
  servers: { servers: [] } as PlexServersResponse,
  initialized: false,
  setupTokenRequired: false,
  // The token as it survives in sessionStorage across a same-tab reload (per-tab,
  // so a fresh tab sees null). Drives the getSetupToken() mock the wizard seeds
  // its input from.
  storedSetupToken: null as string | null,
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
  getSetupToken: () => h.storedSetupToken,
  setSetupToken: h.setSetupToken,
  clearSetupToken: h.clearSetupToken,
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: h.toast }),
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
  h.toast.mockReset()
  h.authenticated = false
  h.servers = { servers: [] }
  h.initialized = false
  h.setupTokenRequired = false
  h.storedSetupToken = null
}

/** Sign in + pick + verify a server so a test lands on the services step. */
async function reachServices() {
  render(<SetupWizard />, { wrapper: Wrapper })
  fireEvent.click(await screen.findByRole('button', { name: /verify server/i }))
  await screen.findByText('Plex server verified ✓')
}

async function validateAllServices() {
  for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
    fireEvent.click(button)
  }
  await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(3))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled())
}

/** Follow the real service-validation gate so a test lands on Libraries. */
async function reachLibraries() {
  await reachServices()
  await validateAllServices()
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
  await screen.findByRole('heading', { level: 1, name: 'Confirm library roots' })
}

describe('SetupWizard — step machine', () => {
  beforeEach(resetMocks)

  it('renders the ordered five-step progress as non-interactive status', () => {
    render(<SetupWizard />, { wrapper: Wrapper })

    const progress = screen.getByRole('list', { name: 'Setup progress' })
    const items = within(progress).getAllByRole('listitem')
    expect(items).toHaveLength(WIZARD_STEPS.length)
    WIZARD_STEPS.forEach((step, index) => {
      expect(within(items[index]!).getByText(step.label)).toBeInTheDocument()
      expect(within(items[index]!).getByText(String(index + 1))).toBeInTheDocument()
    })
    expect(items[0]).toHaveAttribute('aria-current', 'step')
    expect(within(progress).queryByRole('button')).not.toBeInTheDocument()
    expect(within(progress).queryByRole('link')).not.toBeInTheDocument()
  })

  it('keeps every approved heading and intro to one sentence with no exclamation mark', () => {
    for (const metadata of WIZARD_STEPS) {
      expect(metadata.heading).not.toContain('!')
      expect(metadata.description).not.toContain('!')
      expect(metadata.description.match(/[.!?]/g)).toHaveLength(1)
    }
  })

  it('redirects an already initialized direct visit instead of reopening setup', () => {
    h.initialized = true
    render(<SetupWizard />, { wrapper: Wrapper })

    expect(screen.queryByText('First-run setup')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()
  })

  it('shows the Plex sign-in first on a fresh, unauthenticated install', () => {
    render(<SetupWizard />, { wrapper: Wrapper })

    expect(screen.getByRole('heading', { level: 1, name: 'Sign in with Plex' })).toBeInTheDocument()
    expect(
      screen.getByText(
        'Plex is the identity provider, so the server owner administers Plex Manager and shared users get request access automatically.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /sign in with plex/i })).toBeInTheDocument()
    // No access-key link pre-init (the wizard passes only onSignedIn).
    expect(screen.queryByRole('button', { name: /use access key/i })).not.toBeInTheDocument()
    // Neither the server picker nor the service cards are reachable yet.
    expect(screen.queryByLabelText('Plex server')).not.toBeInTheDocument()
    expect(screen.queryByText('Prowlarr')).not.toBeInTheDocument()
    expect(
      screen.queryByRole('heading', { level: 1, name: 'Pick your server' }),
    ).not.toBeInTheDocument()
  })

  it('advances to the server picker once authenticated, listing owned servers', () => {
    h.authenticated = true
    h.servers = SERVERS
    render(<SetupWizard />, { wrapper: Wrapper })

    expect(screen.queryByRole('button', { name: /sign in with plex/i })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Pick your server' })).toBeInTheDocument()
    expect(
      screen.getByText(
        'Choose one of the servers your Plex account can reach, with local connections preferred.',
      ),
    ).toBeInTheDocument()
    const select = screen.getByLabelText('Plex server')
    expect(
      within(select).getByText('Apollo — http://127.0.0.1:32400 (local, reachable)'),
    ).toBeInTheDocument()
  })

  it('focuses the Server heading when owner authentication changes the active step', () => {
    const { rerender } = render(<SetupWizard />, { wrapper: Wrapper })
    expect(screen.getByRole('heading', { level: 1, name: 'Sign in with Plex' })).not.toHaveFocus()

    h.authenticated = true
    h.servers = SERVERS
    rerender(<SetupWizard />)

    expect(screen.getByRole('heading', { level: 1, name: 'Pick your server' })).toHaveFocus()
    expect(
      screen.queryByRole('heading', { level: 1, name: 'Sign in with Plex' }),
    ).not.toBeInTheDocument()
  })

  it('verifying a picked server advances to Services with checked prior steps and numbered future steps', async () => {
    h.authenticated = true
    h.servers = SERVERS
    h.validatePlex.mockResolvedValue(plexVerifyOk([movieLibrary, tvLibrary]))
    render(<SetupWizard />, { wrapper: Wrapper })

    fireEvent.click(screen.getByRole('button', { name: /verify server/i }))

    await waitFor(() =>
      expect(h.validatePlex).toHaveBeenCalledWith({ url: 'http://127.0.0.1:32400' }),
    )
    expect(await screen.findByText('Plex server verified ✓')).toBeInTheDocument()
    expect(screen.queryByText(/127\.0\.0\.1:32400/)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Connect services' })).toHaveFocus()
    expect(
      screen.getByText(
        'Prowlarr finds releases and qBittorrent downloads them, so both must be validated before you continue.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('Prowlarr')).toBeInTheDocument()
    // The Plex card itself is gone from the services step.
    expect(screen.queryByLabelText('Plex token')).not.toBeInTheDocument()

    const progress = screen.getByRole('list', { name: 'Setup progress' })
    const items = within(progress).getAllByRole('listitem')
    expect(items[0]).toHaveAccessibleName('Sign in, completed')
    expect(items[1]).toHaveAccessibleName('Server, completed')
    expect(within(items[0]!).getByText('✓')).toBeInTheDocument()
    expect(within(items[1]!).getByText('✓')).toBeInTheDocument()
    expect(items[2]).toHaveAttribute('aria-current', 'step')
    expect(within(items[3]!).getByText('4')).toBeInTheDocument()
    expect(within(items[4]!).getByText('5')).toBeInTheDocument()

    // The footer Back action returns through the existing changeServer path.
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    expect(screen.getByLabelText('Plex server')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'Pick your server' })).toHaveFocus()
  })
})

describe('SetupWizard — libraries + completion', () => {
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
    await reachLibraries()

    expect(screen.getByRole('heading', { level: 1, name: 'Confirm library roots' })).toHaveFocus()
    expect(
      screen.getByText(
        'Choose where finished files land, using roots that are writable from inside the container.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Prowlarr' })).not.toBeInTheDocument()
    const movieSelect = screen.getByLabelText('Movies library folder')
    const tvSelect = screen.getByLabelText('TV library folder')

    expect(within(movieSelect).getByText(/Movies —/)).toBeInTheDocument()
    expect(within(movieSelect).queryByText(/TV Shows/)).not.toBeInTheDocument()
    expect(within(tvSelect).getByText(/TV Shows —/)).toBeInTheDocument()
    expect(within(tvSelect).queryByText(/^Movies —/)).not.toBeInTheDocument()
  })

  it('stores a container suggested_path as the option value (issue #132)', async () => {
    // A Plex section reporting a HOST-namespace location gets a container-visible
    // suggested_path (setup_validation.library_options); the picker must select
    // THAT path, so completing setup sends the in-container path, not the raw one.
    const hostLibrary: PlexLibraryOption = {
      path: '/host/Media/Movies',
      suggested_path: '/media/Movies',
      section_key: '3',
      section_type: 'movie',
      title: 'Movies',
      writable: null,
    }
    h.validatePlex.mockResolvedValue(plexVerifyOk([hostLibrary, tvLibrary]))
    await reachLibraries()

    const movieSelect = screen.getByLabelText('Movies library folder')
    const option = within(movieSelect).getByText(
      /Movies — \/host\/Media\/Movies · in-container: \/media\/Movies/,
    ) as HTMLOptionElement
    expect(option.value).toBe('/media/Movies')
  })

  it('never requires a tv library folder to be chosen (tv_root is optional)', async () => {
    await reachLibraries()

    fireEvent.change(screen.getByLabelText('Movies library folder'), {
      target: { value: '/media/movies' },
    })

    expect(screen.getByLabelText('TV library folder')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('completes a tv-only install: tv folder chosen, movies left unset', async () => {
    await reachLibraries()

    fireEvent.change(screen.getByLabelText('TV library folder'), { target: { value: '/media/tv' } })

    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('disables completion until at least one library root is chosen', async () => {
    await reachLibraries()

    expect(screen.getByRole('button', { name: /complete setup/i })).toBeDisabled()
  })

  it('shows the tv section as optional when no folder is chosen', async () => {
    await reachLibraries()
    const tvSelect = screen.getByLabelText('TV library folder')
    const tvSection = tvSelect.closest('section')
    expect(tvSection).not.toBeNull()
    expect(within(tvSection!).getByText(/^optional$/i)).toBeInTheDocument()
  })

  it('reuses the Movies/TV Plex library lists for the anime pickers', async () => {
    await reachLibraries()

    const animeMovieSelect = screen.getByLabelText('Anime movies library folder')
    const animeTvSelect = screen.getByLabelText('Anime TV library folder')

    expect(within(animeMovieSelect).getByText(/Movies —/)).toBeInTheDocument()
    expect(within(animeMovieSelect).queryByText(/TV Shows/)).not.toBeInTheDocument()
    expect(within(animeTvSelect).getByText(/TV Shows —/)).toBeInTheDocument()
    expect(within(animeTvSelect).queryByText(/^Movies —/)).not.toBeInTheDocument()
  })

  it('completes an anime-only install: an anime root chosen, movies/tv left unset', async () => {
    await reachLibraries()

    fireEvent.change(screen.getByLabelText('Anime movies library folder'), {
      target: { value: '/media/movies' },
    })

    expect((screen.getByLabelText('Movies library folder') as HTMLSelectElement).value).toBe('')
    expect((screen.getByLabelText('TV library folder') as HTMLSelectElement).value).toBe('')
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('submits the unchanged completion body once, shows Done, then navigates only from Open Discover', async () => {
    h.complete.mockImplementation(async () => {
      // Model the setup-status invalidation racing the local success render.
      h.initialized = true
      return { initialized: true, setup_token_required: false }
    })
    await reachLibraries()

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
    expect(h.complete).toHaveBeenCalledWith({
      plex_url: 'http://127.0.0.1:32400',
      plex_machine_identifier: 'MID-APOLLO',
      plex_token: null,
      prowlarr_url: '',
      prowlarr_api_key: '',
      qbittorrent_url: '',
      qbittorrent_username: '',
      qbittorrent_password: '',
      tmdb_api_key: '',
      movies_root: '/media/movies',
      tv_root: '',
      anime_movie_root: '/media/movies',
      anime_tv_root: '',
    })

    const doneHeading = await screen.findByRole('heading', { level: 1, name: "You're set" })
    expect(doneHeading).toHaveFocus()
    expect(screen.getByText('Setup is complete.')).toBeInTheDocument()
    expect(h.navigate).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('Setup token')).not.toBeInTheDocument()
    expect(screen.queryByText(/save your access key/i)).toBeNull()
    expect(screen.getAllByRole('button')).toHaveLength(1)

    fireEvent.click(screen.getByRole('button', { name: 'Open Discover' }))
    expect(h.navigate).toHaveBeenCalledWith('/', { replace: true })
  })

  it('surfaces a completion rejection and remains on Libraries', async () => {
    h.complete.mockRejectedValue(
      toApiError({ detail: 'setup_complete_failed', message: 'Setup could not be saved.' }, 500),
    )
    await reachLibraries()
    fireEvent.change(screen.getByLabelText('Movies library folder'), {
      target: { value: '/media/movies' },
    })

    fireEvent.click(screen.getByRole('button', { name: /complete setup/i }))

    await waitFor(() =>
      expect(h.toast).toHaveBeenCalledWith({
        title: 'Setup failed',
        description: 'Setup could not be saved.',
        intent: 'error',
      }),
    )
    expect(h.complete).toHaveBeenCalledTimes(1)
    expect(
      screen.getByRole('heading', { level: 1, name: 'Confirm library roots' }),
    ).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1, name: "You're set" })).not.toBeInTheDocument()
    expect(h.navigate).not.toHaveBeenCalled()
  })

  it('returns to Services without clearing roots or current validation results', async () => {
    await reachLibraries()
    fireEvent.change(screen.getByLabelText('Movies library folder'), {
      target: { value: '/media/movies' },
    })

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))

    const servicesHeading = screen.getByRole('heading', { level: 1, name: 'Connect services' })
    expect(servicesHeading).toHaveFocus()
    expect(screen.getByText('3/3 verified')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByRole('heading', { level: 1, name: 'Confirm library roots' })
    expect(screen.getByLabelText('Movies library folder')).toHaveValue('/media/movies')
  })

  it('clears picked library roots when the server is changed, forcing re-selection from the new server', async () => {
    // Server B exposes a DIFFERENT movie library path than server A. A root picked
    // against A must not survive a server change — it could be a path B doesn't own.
    const serverBMovies: PlexLibraryOption = {
      path: '/mnt/serverB/movies',
      section_key: '9',
      section_type: 'movie',
      title: 'Movies B',
      writable: true,
    }
    h.validatePlex.mockReset()
    h.validatePlex
      .mockResolvedValueOnce(plexVerifyOk([movieLibrary, tvLibrary])) // server A
      .mockResolvedValueOnce(plexVerifyOk([serverBMovies])) // server B (after Change)

    render(<SetupWizard />, { wrapper: Wrapper })
    fireEvent.click(await screen.findByRole('button', { name: /verify server/i }))
    await screen.findByText('Plex server verified ✓')

    // Verify every service, then cross the real Services → Libraries gate.
    await validateAllServices()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByRole('heading', { level: 1, name: 'Confirm library roots' })

    // Pick a root from server A's libraries — completion becomes enabled.
    fireEvent.change(screen.getByLabelText('Movies library folder'), {
      target: { value: '/media/movies' },
    })
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled(),
    )

    // Libraries Back preserves the root; Services Back invokes changeServer and
    // clears it before the next owned server is verified.
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    fireEvent.click(await screen.findByRole('button', { name: /verify server/i }))
    await screen.findByText('Plex server verified ✓')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    await screen.findByRole('heading', { level: 1, name: 'Confirm library roots' })

    // The previously-picked root is gone; it must be re-picked from server B's
    // libraries, and completion is disabled until then.
    expect((screen.getByLabelText('Movies library folder') as HTMLSelectElement).value).toBe('')
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeDisabled()

    // The new root can only come from server B's library list.
    expect(
      within(screen.getByLabelText('Movies library folder')).getByText(/Movies B —/),
    ).toBeInTheDocument()
  })
})

describe('SetupWizard — service validation flow', () => {
  beforeEach(() => {
    resetMocks()
    h.authenticated = true
    h.servers = SERVERS
    h.validatePlex.mockResolvedValue(plexVerifyOk([movieLibrary]))
  })

  it('keeps Continue gated on all three current validations and advances without completing', async () => {
    h.validate.mockImplementation(async ({ service }: { service: string }) => ({
      ok: true,
      message: `${service} ok`,
    }))
    await reachServices()

    const continueButton = screen.getByRole('button', { name: 'Continue' })
    const testButtons = screen.getAllByRole('button', { name: /test connection/i })
    expect(continueButton).toBeDisabled()

    fireEvent.click(testButtons[0]!)
    await screen.findByText('1/3 verified')
    expect(continueButton).toBeDisabled()
    fireEvent.click(testButtons[1]!)
    await screen.findByText('2/3 verified')
    expect(continueButton).toBeDisabled()
    fireEvent.click(testButtons[2]!)
    await waitFor(() => expect(continueButton).toBeEnabled())

    fireEvent.click(continueButton)
    expect(h.complete).not.toHaveBeenCalled()
    expect(
      await screen.findByRole('heading', { level: 1, name: 'Confirm library roots' }),
    ).toHaveFocus()
    expect(screen.queryByRole('heading', { name: 'Prowlarr' })).not.toBeInTheDocument()
  })

  it('never marks the Services step complete on partial verification — the chip tracks position, not optimism', async () => {
    // Honesty north star: a "completed" chip must reflect a real, crossed gate,
    // never optimistic verification progress. Validating two of three services
    // must leave Services the current (numbered) step with no checkmark, and the
    // future Libraries/Done steps numbered — until the real Continue gate is met.
    h.validate.mockImplementation(async ({ service }: { service: string }) => ({
      ok: true,
      message: `${service} ok`,
    }))
    await reachServices()

    const testButtons = screen.getAllByRole('button', { name: /test connection/i })
    fireEvent.click(testButtons[0]!)
    fireEvent.click(testButtons[1]!)
    await screen.findByText('2/3 verified')

    const progress = screen.getByRole('list', { name: 'Setup progress' })
    const items = within(progress).getAllByRole('listitem')
    // Services (index 2) is still the current step — numbered, not checkmarked.
    expect(items[2]).toHaveAttribute('aria-current', 'step')
    expect(items[2]).not.toHaveAccessibleName('Services, completed')
    expect(within(items[2]!).getByText('3')).toBeInTheDocument()
    expect(within(items[2]!).queryByText('✓')).not.toBeInTheDocument()
    // Future steps stay numbered.
    expect(within(items[3]!).getByText('4')).toBeInTheDocument()
    expect(within(items[4]!).getByText('5')).toBeInTheDocument()

    // Crossing the real gate (all three verified) is what promotes Services to
    // completed — position advances, so the chip earns its checkmark honestly.
    fireEvent.click(testButtons[2]!)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue' }))
    await screen.findByRole('heading', { level: 1, name: 'Confirm library roots' })
    const advanced = within(
      screen.getByRole('list', { name: 'Setup progress' }),
    ).getAllByRole('listitem')
    expect(advanced[2]).toHaveAccessibleName('Services, completed')
    expect(within(advanced[2]!).getByText('✓')).toBeInTheDocument()
  })

  it('keeps Continue gated on the required setup token after services validate', async () => {
    h.setupTokenRequired = true
    h.storedSetupToken = 'boot-token'
    h.validate.mockImplementation(async ({ service }: { service: string }) => ({
      ok: true,
      message: `${service} ok`,
    }))
    await reachServices()
    await validateAllServices()

    fireEvent.change(screen.getByLabelText('Setup token'), { target: { value: '' } })
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Setup token'), { target: { value: 'new-token' } })
    expect(screen.getByRole('button', { name: 'Continue' })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    expect(await screen.findByLabelText('Setup token')).toHaveValue('new-token')
    expect(h.complete).not.toHaveBeenCalled()
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

  it('re-enables Test connection immediately when a field is edited during an in-flight validation', async () => {
    // Issue #140: editing a field mid-validation must free the button right
    // away — it must not stay disabled until the now-obsolete request settles.
    const pending = deferred<ServiceValidateResponse>()
    h.validate.mockReturnValueOnce(pending.promise)
    await reachServices()

    const testButton = screen.getAllByRole('button', { name: /test connection/i })[0]!
    fireEvent.click(testButton)
    await waitFor(() => expect(testButton).toBeDisabled())

    fireEvent.change(screen.getAllByLabelText('URL')[0]!, {
      target: { value: 'http://new-prowlarr:9696' },
    })

    // Re-enabled synchronously on edit — no waiting for the stale request.
    expect(testButton).toBeEnabled()

    // The stale request settling afterward must not corrupt state: no result
    // surfaces, the verified count stays put, and the button is not re-disabled.
    await act(async () => {
      pending.resolve({ ok: true, message: 'prowlarr ok' })
      await pending.promise
    })
    expect(testButton).toBeEnabled()
    expect(screen.queryByText('prowlarr ok')).not.toBeInTheDocument()
    expect(screen.getByText('0/3 verified')).toBeInTheDocument()
  })

  it('does not let a stale validation finally-clause clobber a newer in-flight one', async () => {
    const first = deferred<ServiceValidateResponse>()
    const second = deferred<ServiceValidateResponse>()
    h.validate.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    await reachServices()

    const testButton = screen.getAllByRole('button', { name: /test connection/i })[0]!
    fireEvent.click(testButton) // gen 1 (first)
    await waitFor(() => expect(testButton).toBeDisabled())

    fireEvent.change(screen.getAllByLabelText('URL')[0]!, {
      target: { value: 'http://new-prowlarr:9696' },
    })
    fireEvent.click(testButton) // gen 2 (second)
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(2))

    // The stale gen-1 request resolves — it must not clear the pending flag
    // for the fresh gen-2 request, nor surface its (stale) result.
    await act(async () => {
      first.resolve({ ok: true, message: 'stale prowlarr ok' })
      await first.promise
    })
    expect(testButton).toBeDisabled()
    expect(screen.queryByText('stale prowlarr ok')).not.toBeInTheDocument()

    // The fresh gen-2 request settling completes normally.
    await act(async () => {
      second.resolve({ ok: true, message: 'prowlarr ok' })
      await second.promise
    })
    expect(testButton).toBeEnabled()
    expect(await screen.findByText('prowlarr ok')).toBeInTheDocument()
  })

  it('normalizes a non-envelope service-test throw so the card shows a real message, never blank', async () => {
    // A bare, non-envelope throw (a bug/network failure with no `.message`) must
    // be routed through toApiError, or the bare `error as ApiError` cast would
    // leave the card message `undefined` — a silent, dishonest blank.
    h.validate.mockRejectedValueOnce('kaboom')
    await reachServices()

    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)

    expect(await screen.findByText(/unexpected error/i)).toBeInTheDocument()
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

  it('renders qBittorrent\'s download_path_note as a non-blocking caution (issues #133/#157)', async () => {
    h.validate.mockResolvedValue({
      ok: true,
      message: 'qbittorrent ok',
      download_path_note: "the client's default save path isn't visible inside this container",
    } satisfies ServiceValidateResponse)
    await reachServices()

    const testButtons = screen.getAllByRole('button', { name: /test connection/i })
    fireEvent.click(testButtons[1]!) // qbittorrent card

    expect(await screen.findByText('qbittorrent ok')).toBeInTheDocument()
    expect(
      await screen.findByText(/default save path isn't visible inside this container/),
    ).toBeInTheDocument()
    // Non-blocking: the card still counts as verified.
    expect(screen.getByText('1/3 verified')).toBeInTheDocument()
  })

  it('renders no note line for a service whose validation carries none', async () => {
    h.validate.mockResolvedValue({ ok: true, message: 'prowlarr ok' } satisfies ServiceValidateResponse)
    await reachServices()

    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)

    expect(await screen.findByText('prowlarr ok')).toBeInTheDocument()
    expect(screen.queryByText(/⚠/)).not.toBeInTheDocument()
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

  it('restores the persisted token after an authed reload so the services step is not stranded', async () => {
    // A mid-wizard reload keeps the 30-day session cookie (authed → past the
    // sign-in step) but resets React state. The token still lives in
    // sessionStorage; pre-fix the input re-initialized empty, leaving
    // setupTokenReady=false with the token card unreachable — Test/Complete
    // permanently disabled with no field to recover (north-star-#1 dead end).
    h.setupTokenRequired = true
    h.authenticated = true
    h.servers = SERVERS
    h.storedSetupToken = 'boot-token'
    h.validatePlex.mockResolvedValue(plexVerifyOk([movieLibrary, tvLibrary]))
    await reachServices()

    // The token card is reachable on the services step, showing the restored value.
    expect(screen.getByLabelText('Setup token')).toHaveValue('boot-token')
    // Every "Test connection" is enabled — the gate reflects the persisted token.
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      expect(button).toBeEnabled()
    }
  })

  it('lets a fresh authed tab (empty per-tab storage) re-enter the token on the server step', () => {
    // A brand-new tab shares the session cookie (authed) but not sessionStorage,
    // so no token is present and the server step's own fetch would 401. The token
    // card must be reachable here too so the operator can supply it — never a
    // terminal-only recovery.
    h.setupTokenRequired = true
    h.authenticated = true
    h.storedSetupToken = null
    h.servers = { servers: [] } // stays on the server step (no verify needed)
    render(<SetupWizard />, { wrapper: Wrapper })

    const field = screen.getByLabelText('Setup token')
    expect(field).toHaveValue('')
    fireEvent.change(field, { target: { value: 'boot-token' } })
    expect(h.setSetupToken).toHaveBeenCalledWith('boot-token')
  })

  describe('a ?setup_token= URL param (issue #65 — the logged ready-to-click link)', () => {
    afterEach(() => {
      // Every test in this block seeds the address bar itself; leave it clean
      // for whatever test runs next.
      window.history.pushState(null, '', '/')
    })

    it('seeds the field, persists it, and strips the token from the address bar', () => {
      h.setupTokenRequired = true
      window.history.pushState(null, '', '/setup?setup_token=url-token')

      render(<SetupWizard />, { wrapper: Wrapper })

      expect(screen.getByLabelText('Setup token')).toHaveValue('url-token')
      expect(h.setSetupToken).toHaveBeenCalledWith('url-token')
      // The bootstrap secret must not linger in browser history / a shareable URL
      // once the field has consumed it.
      expect(window.location.search).toBe('')
      expect(window.location.pathname).toBe('/setup')
    })

    it('mentions the printed docker-logs link and env var in the field hint', () => {
      h.setupTokenRequired = true
      render(<SetupWizard />, { wrapper: Wrapper })

      expect(screen.getByText(/PLEX_MANAGER_SETUP_TOKEN/)).toBeInTheDocument()
      expect(screen.getByText(/docker logs/)).toBeInTheDocument()
    })

    it('prefers the URL token over a stale persisted one for this load', () => {
      h.setupTokenRequired = true
      h.storedSetupToken = 'stale-stored-token'
      window.history.pushState(null, '', '/setup?setup_token=fresh-url-token')

      render(<SetupWizard />, { wrapper: Wrapper })

      expect(screen.getByLabelText('Setup token')).toHaveValue('fresh-url-token')
    })

    it('leaves the field empty and the address bar untouched with no query param', () => {
      h.setupTokenRequired = true
      render(<SetupWizard />, { wrapper: Wrapper })

      expect(screen.getByLabelText('Setup token')).toHaveValue('')
      expect(h.setSetupToken).not.toHaveBeenCalled()
    })
  })
})
