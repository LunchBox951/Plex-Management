import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useCompleteSetup, useSetupStatus, useValidateService } from '../api/hooks'
import { SetupWizard } from './SetupWizard'

vi.mock('../api/hooks', () => ({
  useSetupStatus: vi.fn(),
  useValidateService: vi.fn(),
  useCompleteSetup: vi.fn(),
}))

vi.mock('../components/ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }))

interface ValidateArgs {
  service: string
  body: Record<string, string>
}

// Plex reports one movie AND one tv folder, each tagged by `section_type`; every
// other service just verifies clean.
const validateMock = vi.fn(async (args: ValidateArgs) => {
  if (args.service === 'plex') {
    return {
      ok: true,
      message: 'Connected',
      libraries: [
        { path: '/media/movies', section_key: '1', section_type: 'movie', title: 'Movies', writable: true },
        { path: '/media/tv', section_key: '2', section_type: 'tv', title: 'TV Shows', writable: true },
      ],
    }
  }
  return { ok: true, message: 'Connected' }
})

describe('SetupWizard — tv library picker', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(useSetupStatus as unknown as Mock).mockReturnValue({
      isLoading: false,
      data: { initialized: false, app_api_key: null },
    })
    ;(useValidateService as unknown as Mock).mockReturnValue({
      mutateAsync: validateMock,
      isPending: false,
    })
    ;(useCompleteSetup as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
  })

  it('filters the movie picker to section_type "movie" and the tv picker to "tv"', async () => {
    render(<SetupWizard />, { wrapper: MemoryRouter })
    // Plex is the first service section.
    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)

    const movieSelect = await screen.findByLabelText('Movies library folder')
    const tvSelect = screen.getByLabelText('TV library folder')

    expect(within(movieSelect).getByText(/Movies —/)).toBeInTheDocument()
    expect(within(movieSelect).queryByText(/TV Shows/)).not.toBeInTheDocument()

    expect(within(tvSelect).getByText(/TV Shows —/)).toBeInTheDocument()
    expect(within(tvSelect).queryByText(/^Movies —/)).not.toBeInTheDocument()
  })

  it('never requires a tv library folder to be chosen (tv_root is optional)', async () => {
    render(<SetupWizard />, { wrapper: MemoryRouter })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(validateMock).toHaveBeenCalledTimes(4))

    // Choose the required MOVIE folder...
    const movieSelect = await screen.findByLabelText('Movies library folder')
    fireEvent.change(movieSelect, { target: { value: '/media/movies' } })

    // ...but never touch the tv picker — setup still completes.
    expect(screen.queryByLabelText('TV library folder')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('completes a tv-only install: tv folder chosen, movies left unset', async () => {
    // ADR-0011: a tv-only Plex is legit. The completion gate must accept a tv_root
    // with an empty movies_root — otherwise a tv-only operator (no movie library to
    // point at) can never finish setup.
    render(<SetupWizard />, { wrapper: MemoryRouter })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(validateMock).toHaveBeenCalledTimes(4))

    // Choose ONLY the tv folder; never touch the movie picker.
    const tvSelect = await screen.findByLabelText('TV library folder')
    fireEvent.change(tvSelect, { target: { value: '/media/tv' } })

    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('disables completion until at least one library root is chosen', async () => {
    render(<SetupWizard />, { wrapper: MemoryRouter })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(validateMock).toHaveBeenCalledTimes(4))

    // All services verified but neither library root chosen -> still blocked.
    await screen.findByLabelText('Movies library folder')
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeDisabled()
  })

  it('shows the tv section as optional when no folder is chosen', async () => {
    render(<SetupWizard />, { wrapper: MemoryRouter })
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
    vi.clearAllMocks()
    ;(useSetupStatus as unknown as Mock).mockReturnValue({
      isLoading: false,
      data: { initialized: false, app_api_key: null },
    })
    ;(useValidateService as unknown as Mock).mockReturnValue({
      mutateAsync: validateMock,
      isPending: false,
    })
    ;(useCompleteSetup as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
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
    await waitFor(() => expect(validateMock).toHaveBeenCalledTimes(4))

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
    await waitFor(() => expect(validateMock).toHaveBeenCalledTimes(4))

    // Choose ONLY the anime movies folder; never touch the Movies or TV pickers.
    const animeMovieSelect = await screen.findByLabelText('Anime movies library folder')
    fireEvent.change(animeMovieSelect, { target: { value: '/media/movies' } })

    // movies_root and tv_root remain unset, yet completion is enabled off the anime root.
    expect((screen.getByLabelText('Movies library folder') as HTMLSelectElement).value).toBe('')
    expect((screen.getByLabelText('TV library folder') as HTMLSelectElement).value).toBe('')
    expect(screen.getByRole('button', { name: /complete setup/i })).toBeEnabled()
  })

  it('submits chosen anime roots on complete', async () => {
    const completeMock = vi.fn().mockResolvedValue({ app_api_key: null })
    ;(useCompleteSetup as unknown as Mock).mockReturnValue({
      mutateAsync: completeMock,
      isPending: false,
    })
    render(<SetupWizard />, { wrapper: MemoryRouter })
    for (const button of screen.getAllByRole('button', { name: /test connection/i })) {
      fireEvent.click(button)
    }
    await waitFor(() => expect(validateMock).toHaveBeenCalledTimes(4))

    const movieSelect = await screen.findByLabelText('Movies library folder')
    fireEvent.change(movieSelect, { target: { value: '/media/movies' } })
    fireEvent.change(screen.getByLabelText('Anime movies library folder'), {
      target: { value: '/media/movies' },
    })

    fireEvent.click(screen.getByRole('button', { name: /complete setup/i }))
    await waitFor(() => expect(completeMock).toHaveBeenCalledTimes(1))
    const body = completeMock.mock.calls[0]![0] as Record<string, string>
    expect(body.anime_movie_root).toBe('/media/movies')
  })
})
