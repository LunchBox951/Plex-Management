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

  it('shows the tv section as optional when no folder is chosen', async () => {
    render(<SetupWizard />, { wrapper: MemoryRouter })
    fireEvent.click(screen.getAllByRole('button', { name: /test connection/i })[0]!)
    await screen.findByLabelText('TV library folder')
    expect(screen.getByText(/^optional$/i)).toBeInTheDocument()
  })
})
