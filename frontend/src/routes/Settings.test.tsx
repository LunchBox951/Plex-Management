import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  toast: vi.fn(),
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
  usePlexLibraries: () => ({ data: h.libraries }),
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: h.toast }),
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
    h.libraries = [
      { path: '/old-plex/movies', section_key: '1', section_type: 'movie', title: 'Movies', writable: true },
    ]
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
      { path: '/old-plex/movies', section_key: '1', section_type: 'movie', title: 'Movies', writable: true },
      { path: '/new-plex/movies', section_key: '2', section_type: 'movie', title: 'Films', writable: true },
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
})

describe('Settings — tv_root library picker (optional)', () => {
  beforeEach(() => {
    h.mutateAsync.mockReset()
    h.mutateAsync.mockResolvedValue({})
    h.settingsData = {
      plex_url: 'http://plex:32400',
      plex_token: '***',
      prowlarr_url: 'http://prowlarr:9696',
      prowlarr_api_key: '***',
      qbittorrent_url: 'http://qb:8080',
      qbittorrent_username: 'admin',
      qbittorrent_password: '***',
      tmdb_api_key: '***',
      movies_root: '/plex/movies',
      tv_root: null,
    }
    h.libraries = [
      { path: '/plex/movies', section_key: '1', section_type: 'movie', title: 'Movies', writable: true },
      { path: '/plex/tv', section_key: '2', section_type: 'tv', title: 'TV Shows', writable: true },
    ]
  })

  it('filters the movie picker to section_type "movie" and the tv picker to "tv"', () => {
    render(<Settings />, { wrapper: Wrapper })
    const movieSelect = screen.getByLabelText('Movies library folder')
    const tvSelect = screen.getByLabelText('TV library folder')

    expect(within(movieSelect).getByText(/Movies —/)).toBeInTheDocument()
    expect(within(movieSelect).queryByText(/TV Shows/)).not.toBeInTheDocument()

    expect(within(tvSelect).getByText(/TV Shows —/)).toBeInTheDocument()
    expect(within(tvSelect).queryByText(/^Movies —/)).not.toBeInTheDocument()
  })

  it('saves with an empty tv_root when none is chosen (never required)', async () => {
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().tv_root).toBe('')
    // Saving never fails / gets blocked for having no tv_root.
    expect(lastBody().movies_root).toBe('/plex/movies')
  })

  it('clears tv_root when the Plex connection changes and no folder is re-picked', async () => {
    h.settingsData = { ...h.settingsData!, tv_root: '/plex/tv' }
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByDisplayValue('http://plex:32400'), {
      target: { value: 'http://new-plex:32400' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().tv_root).toBe('')
  })

  it('keeps tv_root when the Plex connection is untouched', async () => {
    h.settingsData = { ...h.settingsData!, tv_root: '/plex/tv' }
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().tv_root).toBe('/plex/tv')
  })
})

describe('Settings — operability fields (ADR-0012, R3-1)', () => {
  beforeEach(() => {
    h.mutateAsync.mockReset()
    h.mutateAsync.mockResolvedValue({})
    h.toast.mockReset()
    h.libraries = []
  })

  it('prefills unset knobs with the backend defaults, then round-trips edits into the SettingsUpdate body', async () => {
    // Every operability knob unset (null) — the form must still prefill with
    // the SAME defaults the backend applies (web/deps.py), not a blank/zero.
    h.settingsData = {
      plex_url: 'http://plex:32400',
      plex_token: '***',
      prowlarr_url: 'http://prowlarr:9696',
      prowlarr_api_key: '***',
      qbittorrent_url: 'http://qb:8080',
      qbittorrent_username: 'admin',
      qbittorrent_password: '***',
      tmdb_api_key: '***',
      movies_root: '/plex/movies',
      disk_pressure_threshold_percent: null,
      disk_pressure_target_percent: null,
      eviction_grace_days: null,
      eviction_enabled: null,
      eviction_proactive_enabled: null,
      eviction_interval_minutes: null,
      log_retention_days: null,
    }
    render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByLabelText('Pressure threshold (%)')).toHaveValue(90)
    expect(screen.getByLabelText('Pressure target (%)')).toHaveValue(80)
    expect(screen.getByLabelText('Eviction grace period (days)')).toHaveValue(30)
    expect(screen.getByLabelText('Eviction check interval (minutes)')).toHaveValue(30)
    expect(screen.getByLabelText('Log retention (days)')).toHaveValue(7)
    expect(screen.getByRole('checkbox', { name: /^Enable automatic eviction/i })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /^Proactive eviction/i })).not.toBeChecked()

    // Edit every knob, including flipping both checkboxes.
    fireEvent.change(screen.getByLabelText('Pressure threshold (%)'), {
      target: { value: '85' },
    })
    fireEvent.change(screen.getByLabelText('Pressure target (%)'), { target: { value: '70' } })
    fireEvent.change(screen.getByLabelText('Eviction grace period (days)'), {
      target: { value: '14' },
    })
    fireEvent.change(screen.getByLabelText('Eviction check interval (minutes)'), {
      target: { value: '15' },
    })
    fireEvent.change(screen.getByLabelText('Log retention (days)'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /^Enable automatic eviction/i }))
    fireEvent.click(screen.getByRole('checkbox', { name: /^Proactive eviction/i }))

    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))

    const body = lastBody()
    expect(body.disk_pressure_threshold_percent).toBe(85)
    expect(body.disk_pressure_target_percent).toBe(70)
    expect(body.eviction_grace_days).toBe(14)
    expect(body.eviction_interval_minutes).toBe(15)
    expect(body.log_retention_days).toBe(3)
    expect(body.eviction_enabled).toBe(false)
    expect(body.eviction_proactive_enabled).toBe(true)
  })

  it('surfaces the backend 422 (target above threshold) instead of swallowing it', async () => {
    h.settingsData = {
      plex_url: 'http://plex:32400',
      plex_token: '***',
      prowlarr_url: 'http://prowlarr:9696',
      prowlarr_api_key: '***',
      qbittorrent_url: 'http://qb:8080',
      qbittorrent_username: 'admin',
      qbittorrent_password: '***',
      tmdb_api_key: '***',
      movies_root: '/plex/movies',
      disk_pressure_threshold_percent: 90,
      disk_pressure_target_percent: 80,
      eviction_grace_days: 30,
      eviction_enabled: true,
      eviction_proactive_enabled: false,
      eviction_interval_minutes: 30,
      log_retention_days: 7,
    }
    h.mutateAsync.mockRejectedValueOnce({
      code: 'validation_error',
      message: 'disk_pressure_target_percent must be <= disk_pressure_threshold_percent',
      status: 422,
    })

    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByLabelText('Pressure target (%)'), { target: { value: '95' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(h.toast).toHaveBeenCalledTimes(1))
    expect(h.toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Save failed',
        description: expect.stringContaining('disk_pressure_target_percent'),
        intent: 'error',
      }),
    )
  })

  it('R5-1: clearing a numeric field aborts the save with a visible error instead of sending 0', async () => {
    h.settingsData = {
      plex_url: 'http://plex:32400',
      plex_token: '***',
      prowlarr_url: 'http://prowlarr:9696',
      prowlarr_api_key: '***',
      qbittorrent_url: 'http://qb:8080',
      qbittorrent_username: 'admin',
      qbittorrent_password: '***',
      tmdb_api_key: '***',
      movies_root: '/plex/movies',
      disk_pressure_threshold_percent: 90,
      disk_pressure_target_percent: 80,
      eviction_grace_days: 30,
      eviction_enabled: true,
      eviction_proactive_enabled: false,
      eviction_interval_minutes: 30,
      log_retention_days: 7,
    }

    render(<Settings />, { wrapper: Wrapper })
    // Clearing "Log retention" leaves the input at '' -- Number('') === 0, the
    // exact silent-zero this fix must reject before ever calling mutateAsync.
    fireEvent.change(screen.getByLabelText('Log retention (days)'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(h.toast).toHaveBeenCalledTimes(1))
    expect(h.toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Save failed',
        description: expect.stringContaining('Log retention'),
        intent: 'error',
      }),
    )
    // No request was ever sent -- the invalid value never reached the backend
    // (not even coerced to 0), and no misleading "Settings saved" toast fired.
    expect(h.mutateAsync).not.toHaveBeenCalled()
  })

  it('R5-1: a valid numeric edit still round-trips and saves as today', async () => {
    h.settingsData = {
      plex_url: 'http://plex:32400',
      plex_token: '***',
      prowlarr_url: 'http://prowlarr:9696',
      prowlarr_api_key: '***',
      qbittorrent_url: 'http://qb:8080',
      qbittorrent_username: 'admin',
      qbittorrent_password: '***',
      tmdb_api_key: '***',
      movies_root: '/plex/movies',
      disk_pressure_threshold_percent: 90,
      disk_pressure_target_percent: 80,
      eviction_grace_days: 30,
      eviction_enabled: true,
      eviction_proactive_enabled: false,
      eviction_interval_minutes: 30,
      log_retention_days: 7,
    }

    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByLabelText('Log retention (days)'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().log_retention_days).toBe(3)
    expect(h.toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Settings saved', intent: 'success' }),
    )
  })
})
