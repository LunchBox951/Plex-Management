import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PlexLibraryOption, SettingsResponse, SettingsUpdate } from '../api/types'
import { Settings } from './Settings'

// Hoisted shared state so the vi.mock factories (hoisted above imports) can read it.
const h = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  settingsData: null as SettingsResponse | null,
  libraries: [] as PlexLibraryOption[],
  librariesError: null as Error | null,
  librariesRefetch: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useSettings: () => ({
    data: h.settingsData,
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  }),
  useUpdateSettings: () => ({ mutateAsync: h.mutateAsync, isPending: false }),
  usePlexLibraries: () => ({
    data: h.libraries,
    isError: h.librariesError !== null,
    error: h.librariesError,
    refetch: h.librariesRefetch,
  }),
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
}))

const Wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

function lastBody(): SettingsUpdate {
  return h.mutateAsync.mock.calls[0]![0] as SettingsUpdate
}

describe('Settings — movies_root save payload (G2)', () => {
  beforeEach(() => {
    h.mutateAsync.mockReset()
    h.mutateAsync.mockResolvedValue({})
    h.settingsData = {
      plex_url: 'http://old-plex:32400',
      plex_token: '***',
      prowlarr_url: 'http://prowlarr:9696',
      prowlarr_api_key: '***',
      qbittorrent_url: 'http://qb:8080',
      qbittorrent_username: 'admin',
      qbittorrent_password: '***',
      tmdb_api_key: '***',
      movies_root: '/old-plex/movies',
    }
    h.libraries = [{ path: '/old-plex/movies', section_key: '1', title: 'Movies', writable: true }]
    h.librariesError = null
    h.librariesRefetch.mockReset()
  })

  it('clears movies_root when the Plex URL changes and no folder is re-picked', async () => {
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByDisplayValue('http://old-plex:32400'), {
      target: { value: 'http://new-plex:32400' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().plex_url).toBe('http://new-plex:32400')
    // The OLD server's folder must NOT ship with the new creds.
    expect(lastBody().movies_root).toBe('')
  })

  it('clears movies_root when only the Plex token is (re)entered', async () => {
    render(<Settings />, { wrapper: Wrapper })
    // Label "Token" is unique to the Plex section; the field seeds empty, so any
    // typed value is an intentional connection change.
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: 'new-token' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().plex_token).toBe('new-token')
    expect(lastBody().movies_root).toBe('')
  })

  it('keeps movies_root when the Plex connection is untouched', async () => {
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByDisplayValue('admin'), { target: { value: 'newadmin' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().movies_root).toBe('/old-plex/movies')
  })

  it('does not save a stale library re-selection after a Plex change', async () => {
    h.libraries = [
      { path: '/old-plex/movies', section_key: '1', title: 'Movies', writable: true },
      { path: '/old-plex/films', section_key: '2', title: 'Films', writable: true },
    ]
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByDisplayValue('http://old-plex:32400'), {
      target: { value: 'http://new-plex:32400' },
    })
    expect(screen.getByLabelText('Movies library folder')).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().movies_root).toBe('')
  })

  it('shows Plex library picker failures instead of silently falling back to a manual path', () => {
    h.libraries = []
    h.librariesError = new Error('Plex unavailable')

    render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByText("Couldn't load Plex libraries")).toBeInTheDocument()
    expect(screen.getByText('Plex unavailable')).toBeInTheDocument()
    expect(screen.queryByDisplayValue('/old-plex/movies')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(h.librariesRefetch).toHaveBeenCalledTimes(1)
  })
})
