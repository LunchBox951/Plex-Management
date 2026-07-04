import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PlexLibraryOption, ServiceValidateResponse } from '../api/types'
import { SetupWizard } from './SetupWizard'

const h = vi.hoisted(() => ({
  validate: vi.fn(),
  complete: vi.fn(),
  setApiKey: vi.fn(),
  setSetupToken: vi.fn(),
  clearSetupToken: vi.fn(),
  initialized: false,
  setupTokenRequired: false,
}))

vi.mock('../api/hooks', () => ({
  useSetupStatus: () => ({
    data: {
      initialized: h.initialized,
      app_api_key: null,
      setup_token_required: h.setupTokenRequired,
    },
    isLoading: false,
  }),
  useValidateService: () => ({ mutateAsync: h.validate, isPending: false }),
  useCompleteSetup: () => ({ mutateAsync: h.complete, isPending: false }),
}))

vi.mock('../lib/apiKey', () => ({
  setApiKey: h.setApiKey,
  setSetupToken: h.setSetupToken,
  clearSetupToken: h.clearSetupToken,
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}))

const Wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => {
    resolve = res
  })
  return { promise, resolve }
}

function plexOk(libraries: PlexLibraryOption[] = []): ServiceValidateResponse {
  return { ok: true, message: 'Plex ok', libraries }
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

function mockAllServicesOk() {
  h.validate.mockImplementation(async ({ service }: { service: string }) => {
    if (service === 'plex') {
      return plexOk([movieLibrary, tvLibrary])
    }
    return { ok: true, message: `${service} ok` }
  })
}

describe('SetupWizard', () => {
  beforeEach(() => {
    h.validate.mockReset()
    h.complete.mockReset()
    h.setApiKey.mockReset()
    h.setSetupToken.mockReset()
    h.clearSetupToken.mockReset()
    h.initialized = false
    h.setupTokenRequired = false
  })

  it('filters the movie picker to section_type "movie" and the tv picker to "tv"', async () => {
    mockAllServicesOk()

    render(<SetupWizard />, { wrapper: Wrapper })
    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)

    const movieSelect = await screen.findByLabelText('Movies library folder')
    const tvSelect = screen.getByLabelText('TV library folder')

    expect(within(movieSelect).getByText(/Movies —/)).toBeInTheDocument()
    expect(within(movieSelect).queryByText(/TV Shows/)).not.toBeInTheDocument()

    expect(within(tvSelect).getByText(/TV Shows —/)).toBeInTheDocument()
    expect(within(tvSelect).queryByText(/^Movies —/)).not.toBeInTheDocument()
  })

  it('never requires a tv library folder to be chosen (tv_root is optional)', async () => {
    mockAllServicesOk()

    render(<SetupWizard />, { wrapper: Wrapper })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(4))

    const movieSelect = await screen.findByLabelText('Movies library folder')
    fireEvent.change(movieSelect, { target: { value: '/media/movies' } })

    expect(screen.queryByLabelText('TV library folder')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('completes a tv-only install: tv folder chosen, movies left unset', async () => {
    mockAllServicesOk()

    render(<SetupWizard />, { wrapper: Wrapper })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(4))

    const tvSelect = await screen.findByLabelText('TV library folder')
    fireEvent.change(tvSelect, { target: { value: '/media/tv' } })

    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('disables completion until at least one library root is chosen', async () => {
    mockAllServicesOk()

    render(<SetupWizard />, { wrapper: Wrapper })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(4))

    await screen.findByLabelText('Movies library folder')
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeDisabled()
  })

  it('shows the tv section as optional when no folder is chosen', async () => {
    mockAllServicesOk()

    render(<SetupWizard />, { wrapper: Wrapper })
    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)
    const tvSelect = await screen.findByLabelText('TV library folder')
    // Scoped to the TV Library section -- the Anime library section (ADR-0015)
    // also renders an "optional" badge, so an unscoped query would now match
    // both.
    const tvSection = tvSelect.closest('section')
    expect(tvSection).not.toBeNull()
    expect(within(tvSection!).getByText(/^optional$/i)).toBeInTheDocument()
  })
})

describe('SetupWizard — anime library pickers (ADR-0015, optional)', () => {
  beforeEach(() => {
    h.validate.mockReset()
    h.complete.mockReset()
    h.setApiKey.mockReset()
    h.setSetupToken.mockReset()
    h.clearSetupToken.mockReset()
    h.initialized = false
    h.setupTokenRequired = false
    mockAllServicesOk()
  })

  it('reuses the Movies/TV Plex library lists for the anime pickers', async () => {
    render(<SetupWizard />, { wrapper: MemoryRouter })
    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)

    const animeMovieSelect = await screen.findByLabelText('Anime movies library folder')
    const animeTvSelect = screen.getByLabelText('Anime TV library folder')

    expect(within(animeMovieSelect).getByText(/Movies —/)).toBeInTheDocument()
    expect(within(animeMovieSelect).queryByText(/TV Shows/)).not.toBeInTheDocument()

    expect(within(animeTvSelect).getByText(/TV Shows —/)).toBeInTheDocument()
    expect(within(animeTvSelect).queryByText(/^Movies —/)).not.toBeInTheDocument()
  })

  it('never requires an anime library folder -- setup completes with neither chosen', async () => {
    render(<SetupWizard />, { wrapper: MemoryRouter })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(4))

    const movieSelect = await screen.findByLabelText('Movies library folder')
    fireEvent.change(movieSelect, { target: { value: '/media/movies' } })

    // Anime pickers are visible but never touched -- setup still completes.
    expect(screen.queryByLabelText('Anime movies library folder')).toBeInTheDocument()
    expect(screen.queryByLabelText('Anime TV library folder')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('completes an anime-only install: an anime root chosen, movies/tv left unset', async () => {
    // FINDING 2: an anime-only install is backend-supported (anime imports route to
    // their own roots). The completion gate must count anime_movie_root/anime_tv_root,
    // so choosing ONLY an anime folder — with movies_root and tv_root empty — enables
    // completion. Before the fix the gate only accepted movies_root/tv_root, locking an
    // anime-only operator out of setup entirely.
    render(<SetupWizard />, { wrapper: MemoryRouter })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(4))

    // Choose ONLY the anime movies folder; never touch the Movies or TV pickers.
    const animeMovieSelect = await screen.findByLabelText('Anime movies library folder')
    fireEvent.change(animeMovieSelect, { target: { value: '/media/movies' } })

    // movies_root and tv_root remain unset, yet completion is enabled off the anime root.
    expect((screen.getByLabelText('Movies library folder') as HTMLSelectElement).value).toBe('')
    expect((screen.getByLabelText('TV library folder') as HTMLSelectElement).value).toBe('')
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('submits chosen anime roots on complete', async () => {
    h.complete.mockResolvedValue({ app_api_key: null })
    render(<SetupWizard />, { wrapper: MemoryRouter })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(h.validate).toHaveBeenCalledTimes(4))

    const movieSelect = await screen.findByLabelText('Movies library folder')
    fireEvent.change(movieSelect, { target: { value: '/media/movies' } })
    fireEvent.change(screen.getByLabelText('Anime movies library folder'), {
      target: { value: '/media/movies' },
    })

    fireEvent.click(screen.getByRole('button', { name: /complete setup/i }))
    await waitFor(() => expect(h.complete).toHaveBeenCalledTimes(1))
    const body = h.complete.mock.calls[0]![0] as Record<string, string>
    expect(body.anime_movie_root).toBe('/media/movies')
  })
})

describe('SetupWizard — validation & completion flow', () => {
  beforeEach(() => {
    h.validate.mockReset()
    h.complete.mockReset()
    h.setApiKey.mockReset()
    h.setSetupToken.mockReset()
    h.clearSetupToken.mockReset()
    h.initialized = false
    h.setupTokenRequired = false
  })

  it('ignores a stale validation success after fields are edited', async () => {
    const pending = deferred<ServiceValidateResponse>()
    h.validate.mockReturnValueOnce(pending.promise)

    render(<SetupWizard />, { wrapper: Wrapper })

    fireEvent.change(screen.getByLabelText('Server URL'), {
      target: { value: 'http://old-plex:32400' },
    })
    fireEvent.change(screen.getByLabelText('Plex token'), { target: { value: 'old-token' } })
    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)
    fireEvent.change(screen.getByLabelText('Server URL'), {
      target: { value: 'http://new-plex:32400' },
    })

    await act(async () => {
      pending.resolve(plexOk([movieLibrary]))
      await pending.promise
    })

    expect(screen.queryByText('Plex ok')).not.toBeInTheDocument()
    expect(screen.getByText('0/4 verified')).toBeInTheDocument()
    // Movies + TV + the Anime library section (ADR-0015) each hint at Plex.
    expect(screen.getAllByText(/Verify Plex above/i)).toHaveLength(3)
  })

  it('stores and reveals the one-time setup key before navigating away', async () => {
    h.validate.mockImplementation(async ({ service }: { service: string }) => {
      if (service === 'plex') {
        return plexOk([movieLibrary])
      }
      return { ok: true, message: `${service} ok` }
    })
    h.complete.mockResolvedValue({ app_api_key: 'one-time-key' })

    render(<SetupWizard />, { wrapper: Wrapper })

    fireEvent.change(screen.getByLabelText('Server URL'), {
      target: { value: 'http://plex:32400' },
    })
    fireEvent.change(screen.getByLabelText('Plex token'), { target: { value: 'plex-token' } })
    fireEvent.change(screen.getAllByLabelText('URL')[0]!, {
      target: { value: 'http://prowlarr:9696' },
    })
    fireEvent.change(screen.getAllByLabelText('API key')[0]!, {
      target: { value: 'prowlarr-key' },
    })
    fireEvent.change(screen.getAllByLabelText('URL')[1]!, {
      target: { value: 'http://qbittorrent:8080' },
    })
    fireEvent.change(screen.getByLabelText('Username'), { target: { value: 'admin' } })
    fireEvent.change(screen.getByLabelText('Password'), { target: { value: 'password' } })
    fireEvent.change(screen.getAllByLabelText('API key')[1]!, {
      target: { value: 'tmdb-key' },
    })

    const testButtons = screen.getAllByRole('button', { name: /test connection/i })
    fireEvent.click(testButtons[0]!)
    await screen.findByText('Plex ok')
    fireEvent.change(screen.getByLabelText('Movies library folder'), {
      target: { value: '/media/movies' },
    })
    fireEvent.click(testButtons[1]!)
    await screen.findByText('prowlarr ok')
    fireEvent.click(testButtons[2]!)
    await screen.findByText('qbittorrent ok')
    fireEvent.click(testButtons[3]!)
    await screen.findByText('tmdb ok')

    await waitFor(() => expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: /complete setup/i }))

    await waitFor(() => expect(h.setApiKey).toHaveBeenCalledWith('one-time-key'))
    expect(await screen.findByText('one-time-key')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /continue/i })).toBeInTheDocument()
  })

  it('requires the configured setup token before validation or completion', async () => {
    h.setupTokenRequired = true
    h.validate.mockResolvedValue({ ok: true, message: 'Plex ok', libraries: [] })

    render(<SetupWizard />, { wrapper: Wrapper })

    const testButtons = screen.getAllByRole('button', { name: /test connection/i })
    expect(screen.getByLabelText('Setup token')).toBeInTheDocument()
    expect(testButtons[0]).toBeDisabled()
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Setup token'), {
      target: { value: 'boot-token' },
    })

    expect(h.setSetupToken).toHaveBeenCalledWith('boot-token')
    expect(testButtons[0]).toBeEnabled()

    fireEvent.click(testButtons[0]!)
    await waitFor(() => expect(h.validate).toHaveBeenCalled())

    fireEvent.change(screen.getByLabelText('Setup token'), {
      target: { value: '' },
    })
    expect(h.clearSetupToken).toHaveBeenCalled()
    expect(testButtons[0]).toBeDisabled()
  })

  it('keeps each service test disabled while its own validation is pending', async () => {
    const plexPending = deferred<ServiceValidateResponse>()
    const prowlarrPending = deferred<ServiceValidateResponse>()
    h.validate.mockImplementation(({ service }: { service: string }) =>
      service === 'plex' ? plexPending.promise : prowlarrPending.promise,
    )

    render(<SetupWizard />, { wrapper: Wrapper })

    const testButtons = screen.getAllByRole('button', { name: /test connection/i })
    fireEvent.click(testButtons[0]!)
    await waitFor(() => expect(testButtons[0]).toBeDisabled())

    fireEvent.click(testButtons[1]!)
    await waitFor(() => {
      expect(testButtons[0]).toBeDisabled()
      expect(testButtons[1]).toBeDisabled()
    })

    await act(async () => {
      plexPending.resolve(plexOk())
      prowlarrPending.resolve({ ok: true, message: 'Prowlarr ok' })
      await Promise.all([plexPending.promise, prowlarrPending.promise])
    })
  })
})
