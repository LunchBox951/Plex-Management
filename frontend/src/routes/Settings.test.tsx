import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  HealthResponse,
  PlexLibraryOption,
  SettingsResponse,
  SettingsUpdate,
} from '../api/types'
import type { ApiError } from '../lib/errors'
import { Settings } from './Settings'

// Hoisted shared state so the vi.mock factories (hoisted above imports) can read it.
const h = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  updatePending: false,
  settingsData: null as SettingsResponse | null,
  settingsLoading: false,
  settingsError: null as ApiError | null,
  settingsRefetch: vi.fn(),
  libraries: [] as PlexLibraryOption[],
  toast: vi.fn(),
  librariesError: null as Error | null,
  librariesRefetch: vi.fn(),
  rotateMutateAsync: vi.fn(),
  revokeMutateAsync: vi.fn(),
  rotatePending: false,
  revokePending: false,
  // Settings → Access recovery-key status ({ exists }). A mutable flag so a
  // generate/revoke mock can flip it; the ensuing re-render reflects the new
  // state (the status endpoint only ever reports existence, never the key).
  appKeyExists: false,
  statusLoading: false,
  // A persistent status-fetch failure. `data` is withheld while erroring so the
  // component can never derive `exists` from a stale/absent body.
  statusIsError: false,
  statusError: null as ApiError | null,
  statusRefetch: vi.fn(),
  healthData: null as HealthResponse | null,
  healthError: null as ApiError | null,
  healthFetching: false,
  healthRefetch: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useSettings: () => ({
    data: h.settingsLoading || h.settingsError ? undefined : h.settingsData,
    isLoading: h.settingsLoading,
    isError: h.settingsError !== null,
    error: h.settingsError,
    refetch: h.settingsRefetch,
  }),
  useUpdateSettings: () => ({ mutateAsync: h.mutateAsync, isPending: h.updatePending }),
  useOpsHealth: () => ({
    data: h.healthError ? undefined : h.healthData,
    isError: h.healthError !== null,
    error: h.healthError,
    isFetching: h.healthFetching,
    refetch: h.healthRefetch,
  }),
  usePlexLibraries: () => ({
    data: h.libraries,
    isError: h.librariesError !== null,
    error: h.librariesError,
    refetch: h.librariesRefetch,
  }),
  useAppKeyStatus: () => ({
    data: h.statusLoading || h.statusIsError ? undefined : { exists: h.appKeyExists },
    isLoading: h.statusLoading,
    isError: h.statusIsError,
    error: h.statusError,
    refetch: h.statusRefetch,
  }),
  useRotateAppKey: () => ({ mutateAsync: h.rotateMutateAsync, isPending: h.rotatePending }),
  useRevokeAppKey: () => ({ mutateAsync: h.revokeMutateAsync, isPending: h.revokePending }),
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: h.toast }),
}))

const Wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

beforeEach(() => {
  h.updatePending = false
  h.settingsLoading = false
  h.settingsError = null
  h.settingsRefetch.mockReset()
  h.healthData = null
  h.healthError = null
  h.healthFetching = false
  h.healthRefetch.mockReset()
})

function lastBody(): SettingsUpdate {
  return h.mutateAsync.mock.calls[0]![0] as SettingsUpdate
}

const CONFIGURED_SERVICES: SettingsResponse = {
  plex_url: 'http://plex:32400',
  plex_token: '***',
  prowlarr_url: 'http://prowlarr:9696/prowlarr',
  prowlarr_api_key: '***',
  qbittorrent_url: 'http://qb:8080',
  qbittorrent_username: 'admin',
  qbittorrent_password: '***',
  tmdb_api_key: '***',
  movies_root: '/media/movies',
}

function healthResponse(
  subsystems: HealthResponse['subsystems'],
): HealthResponse {
  return {
    subsystems,
    disks: [],
    reconcile: { consecutive_failures: 0 },
    autograb: { consecutive_failures: 0, cooled_down_scopes: 0 },
  }
}

function serviceSection(name: 'Plex' | 'Prowlarr' | 'qBittorrent' | 'TMDB') {
  const section = screen.getByRole('heading', { name }).closest('section')
  if (section === null) throw new Error(`${name} settings section not found`)
  return within(section)
}

describe('Settings — admin grammar and saved service health', () => {
  beforeEach(() => {
    h.mutateAsync.mockReset()
    h.mutateAsync.mockResolvedValue({})
    h.toast.mockReset()
    h.settingsData = CONFIGURED_SERVICES
    h.libraries = []
    h.librariesError = null
    h.statusLoading = false
    h.statusIsError = false
    h.statusError = null
    h.appKeyExists = false
  })

  it('puts the single Save action in the admin header and preserves the mutation', async () => {
    render(<Settings />, { wrapper: Wrapper })

    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save.closest('header')).not.toBeNull()
    expect(screen.getAllByRole('button', { name: 'Save changes' })).toHaveLength(1)

    fireEvent.click(save)
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
  })

  it('exposes the header Save loading state without leaving it interactive', () => {
    h.updatePending = true
    render(<Settings />, { wrapper: Wrapper })

    const save = screen.getByRole('button', { name: 'Save changes' })
    expect(save.closest('header')).not.toBeNull()
    expect(save).toBeDisabled()
    expect(save.querySelector('[aria-hidden="true"]')).not.toBeNull()
  })

  it('keeps the Settings header stable while loading and on an actionable error', () => {
    h.settingsLoading = true
    const { rerender } = render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()

    h.settingsLoading = false
    h.settingsError = {
      code: 'unknown_error',
      message: 'Settings are offline',
      status: 503,
    }
    rerender(<Settings />)

    expect(screen.getByRole('heading', { level: 1, name: 'Settings' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(h.settingsRefetch).toHaveBeenCalledTimes(1)
  })

  it('maps every persisted subsystem state honestly and renders sanitized diagnostics', () => {
    h.healthData = healthResponse([
      {
        name: 'plex',
        status: 'ok',
        detail: null,
        checked_at: '2026-07-12T12:00:00Z',
      },
      {
        name: 'prowlarr',
        status: 'degraded',
        detail: 'Two indexers are unavailable.',
        checked_at: '2026-07-12T12:00:00Z',
      },
      {
        name: 'qbittorrent',
        status: 'down',
        detail: 'Authentication failed.',
        note: 'The default save path is not visible inside this container.',
        checked_at: '2026-07-12T12:00:00Z',
      },
      {
        name: 'tmdb',
        status: 'not_configured',
        detail: null,
        checked_at: '2026-07-12T12:00:00Z',
      },
    ])

    render(<Settings />, { wrapper: Wrapper })

    expect(serviceSection('Plex').getByText('Connected')).toBeInTheDocument()
    expect(serviceSection('Prowlarr').getByText('Degraded')).toBeInTheDocument()
    expect(serviceSection('Prowlarr').getByText('Two indexers are unavailable.')).toHaveClass(
      'text-searching',
    )
    expect(serviceSection('qBittorrent').getByText('Down')).toBeInTheDocument()
    expect(serviceSection('qBittorrent').getByText('Authentication failed.')).toHaveClass(
      'text-error',
    )
    expect(
      serviceSection('qBittorrent').getByText(/default save path is not visible/i),
    ).toHaveClass('text-searching')
    expect(serviceSection('TMDB').getByText('Not configured')).toBeInTheDocument()
  })

  it('uses a neutral unavailable state when saved health cannot be resolved', () => {
    h.healthError = {
      code: 'unknown_error',
      message: 'Health is unavailable',
      status: 503,
    }

    render(<Settings />, { wrapper: Wrapper })

    const unavailable = screen.getAllByText('Status unavailable')
    expect(unavailable).toHaveLength(4)
    for (const label of unavailable) {
      expect(label.querySelector('[aria-hidden="true"]')).toHaveClass('bg-faint')
      expect(label.querySelector('[aria-hidden="true"]')).not.toHaveClass('bg-available')
    }
  })

  it('refreshes only saved health, then disables validation for a dirty card', () => {
    h.healthData = healthResponse([
      {
        name: 'plex',
        status: 'ok',
        checked_at: '2026-07-12T12:00:00Z',
      },
    ])

    render(<Settings />, { wrapper: Wrapper })

    const validate = screen.getByRole('button', { name: 'Validate Plex connection' })
    fireEvent.click(validate)
    expect(h.healthRefetch).toHaveBeenCalledTimes(1)
    expect(h.mutateAsync).not.toHaveBeenCalled()

    const token = serviceSection('Plex').getByLabelText('Token')
    expect(token).toHaveValue('')
    h.healthFetching = true
    fireEvent.change(token, { target: { value: 'candidate-token' } })

    expect(serviceSection('Plex').getByText('Unsaved changes')).toBeInTheDocument()
    expect(validate).toBeDisabled()
    expect(validate.querySelector('[aria-hidden="true"]')).toBeNull()
    expect(
      serviceSection('Plex').getByText('Save changes before validating this connection.'),
    ).toBeInTheDocument()
    expect(serviceSection('Plex').queryByText('Connected')).not.toBeInTheDocument()
    expect(h.mutateAsync).not.toHaveBeenCalled()
  })

  it('retains all services, safety knobs, toggles, and navigation paths', () => {
    render(<Settings />, { wrapper: Wrapper })

    for (const service of ['Plex', 'Prowlarr', 'qBittorrent', 'TMDB']) {
      expect(screen.getByRole('heading', { level: 2, name: service })).toBeInTheDocument()
    }
    expect(serviceSection('Plex').getByLabelText('Token')).toHaveValue('')
    expect(serviceSection('Prowlarr').getByLabelText('API key')).toHaveValue('')
    expect(serviceSection('qBittorrent').getByLabelText('Password')).toHaveValue('')
    expect(serviceSection('TMDB').getByLabelText('API key')).toHaveValue('')

    for (const label of [
      'Pressure threshold (%)',
      'Pressure target (%)',
      'Eviction grace period (days)',
      'Eviction check interval (minutes)',
      'Log retention (days)',
    ]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
    expect(screen.getByRole('checkbox', { name: /^Enable automatic eviction/i })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /^Proactive eviction/i })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /^Enable auto-grab/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View profile' })).toHaveAttribute('href', '/quality')
    expect(screen.getByRole('link', { name: 'Manage blocklist' })).toHaveAttribute(
      'href',
      '/blocklist',
    )
  })
})

describe('Settings — changed service credential consent', () => {
  beforeEach(() => {
    h.mutateAsync.mockReset()
    h.mutateAsync.mockResolvedValue({})
    h.toast.mockReset()
    h.settingsData = CONFIGURED_SERVICES
    h.libraries = []
    h.librariesError = null
  })

  it.each([
    {
      service: 'Plex' as const,
      oldUrl: 'http://plex:32400',
      newUrl: 'https://plex:32400',
      field: 'Token',
      credential: 'Plex token',
      bodyKey: 'plex_token' as const,
      value: 'new-plex-token',
    },
    {
      service: 'Prowlarr' as const,
      oldUrl: 'http://prowlarr:9696/prowlarr',
      newUrl: 'http://prowlarr:9696/other',
      field: 'API key',
      credential: 'Prowlarr API key',
      bodyKey: 'prowlarr_api_key' as const,
      value: 'new-prowlarr-key',
    },
  ])(
    'requires the configured $credential when its canonical base changes',
    async ({ service, oldUrl, newUrl, field, credential, bodyKey, value }) => {
      render(<Settings />, { wrapper: Wrapper })
      const section = serviceSection(service)
      const secret = section.getByLabelText(field)

      expect(secret).not.toBeRequired()
      expect(section.getByText('•••• set (leave blank to keep)')).toBeInTheDocument()

      fireEvent.change(screen.getByDisplayValue(oldUrl), { target: { value: newUrl } })

      expect(secret).toBeRequired()
      expect(
        section.getByText(`Re-enter the ${credential} because the service address changed.`),
      ).toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
      await waitFor(() =>
        expect(h.toast).toHaveBeenCalledWith(
          expect.objectContaining({
            title: 'Save failed',
            description: `Re-enter the ${credential} because the service address changed.`,
          }),
        ),
      )
      expect(h.mutateAsync).not.toHaveBeenCalled()

      fireEvent.change(secret, { target: { value } })
      fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
      await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
      expect(lastBody()[bodyKey]).toBe(value)
    },
  )

  it('sends an explicit blank password when a configured qBittorrent base changes', async () => {
    render(<Settings />, { wrapper: Wrapper })
    const section = serviceSection('qBittorrent')
    const password = section.getByLabelText('Password')

    fireEvent.change(screen.getByDisplayValue('http://qb:8080'), {
      target: { value: 'http://qb:9090' },
    })

    expect(password).not.toBeRequired()
    expect(section.getByText(/leave it blank only if the new service uses an empty password/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().qbittorrent_password).toBe('')
  })

  it.each([
    {
      service: 'Plex' as const,
      oldUrl: 'http://plex:32400',
      field: 'Token',
      urlKey: 'plex_url' as const,
      secretKey: 'plex_token' as const,
    },
    {
      service: 'Prowlarr' as const,
      oldUrl: 'http://prowlarr:9696/prowlarr',
      field: 'API key',
      urlKey: 'prowlarr_url' as const,
      secretKey: 'prowlarr_api_key' as const,
    },
    {
      service: 'qBittorrent' as const,
      oldUrl: 'http://qb:8080',
      field: 'Password',
      urlKey: 'qbittorrent_url' as const,
      secretKey: 'qbittorrent_password' as const,
    },
  ])(
    'allows clearing the configured $service URL without re-entering or overwriting its secret',
    async ({ service, oldUrl, field, urlKey, secretKey }) => {
      render(<Settings />, { wrapper: Wrapper })
      const secret = serviceSection(service).getByLabelText(field)

      fireEvent.change(screen.getByDisplayValue(oldUrl), { target: { value: '' } })

      expect(secret).not.toBeRequired()
      fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
      await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
      expect(lastBody()[urlKey]).toBe('')
      expect(lastBody()).not.toHaveProperty(secretKey)
    },
  )

  it('keeps all configured secrets optional for canonical-equivalent base spellings', async () => {
    h.settingsData = {
      ...CONFIGURED_SERVICES,
      plex_url: 'HTTP://PLEX.local:80/plex/',
      prowlarr_url: 'https://PROWLARR.local:443/prowlarr/',
      qbittorrent_url: 'http://QB.local:80/qbt/',
    }
    render(<Settings />, { wrapper: Wrapper })

    fireEvent.change(screen.getByDisplayValue('HTTP://PLEX.local:80/plex/'), {
      target: { value: 'http://plex.local/plex' },
    })
    fireEvent.change(screen.getByDisplayValue('https://PROWLARR.local:443/prowlarr/'), {
      target: { value: 'https://prowlarr.local/prowlarr' },
    })
    fireEvent.change(screen.getByDisplayValue('http://QB.local:80/qbt/'), {
      target: { value: 'http://qb.local/qbt' },
    })

    for (const [service, field] of [
      ['Plex', 'Token'],
      ['Prowlarr', 'API key'],
      ['qBittorrent', 'Password'],
    ] as const) {
      const section = serviceSection(service)
      expect(section.getByLabelText(field)).not.toBeRequired()
      expect(section.getByText('•••• set (leave blank to keep)')).toBeInTheDocument()
    }

    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody()).not.toHaveProperty('plex_token')
    expect(lastBody()).not.toHaveProperty('prowlarr_api_key')
    expect(lastBody()).not.toHaveProperty('qbittorrent_password')
  })
})

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
    h.librariesError = null
    h.librariesRefetch.mockReset()
    h.toast.mockReset()
  })

  it('clears movies_root when the Plex URL changes and no folder is re-picked', async () => {
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByDisplayValue('http://old-plex:32400'), {
      target: { value: 'http://new-plex:32400' },
    })
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: 'new-token' } })
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
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: 'new-token' } })
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
    h.librariesError = null
    h.librariesRefetch.mockReset()
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
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: 'new-token' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().tv_root).toBe('')
  })

  it('does not save a stale tv library re-selection after a Plex change', async () => {
    h.libraries = [
      { path: '/old-plex/tv', section_key: '2', section_type: 'tv', title: 'Old TV', writable: true },
      { path: '/new-plex/tv', section_key: '3', section_type: 'tv', title: 'New TV', writable: true },
    ]
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByDisplayValue('http://plex:32400'), {
      target: { value: 'http://new-plex:32400' },
    })

    expect(screen.getByLabelText('TV library folder')).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: 'new-token' } })
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

describe('Settings — anime library roots (ADR-0015, optional)', () => {
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
      tv_root: '/plex/tv',
      anime_movie_root: null,
      anime_tv_root: null,
    }
    h.libraries = [
      { path: '/plex/movies', section_key: '1', section_type: 'movie', title: 'Movies', writable: true },
      { path: '/plex/tv', section_key: '2', section_type: 'tv', title: 'TV Shows', writable: true },
      { path: '/plex/anime-movies', section_key: '3', section_type: 'movie', title: 'Anime Movies', writable: true },
      { path: '/plex/anime-tv', section_key: '4', section_type: 'tv', title: 'Anime', writable: true },
    ]
  })

  it('never blocks Save when neither anime root is chosen', async () => {
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().anime_movie_root).toBe('')
    expect(lastBody().anime_tv_root).toBe('')
    // The normal roots are untouched by the anime-only pickers being unset.
    expect(lastBody().movies_root).toBe('/plex/movies')
    expect(lastBody().tv_root).toBe('/plex/tv')
  })

  it('picks anime roots from the same Plex library lists as Movies/TV and saves them', async () => {
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByLabelText('Anime movies library folder'), {
      target: { value: '/plex/anime-movies' },
    })
    fireEvent.change(screen.getByLabelText('Anime TV library folder'), {
      target: { value: '/plex/anime-tv' },
    })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().anime_movie_root).toBe('/plex/anime-movies')
    expect(lastBody().anime_tv_root).toBe('/plex/anime-tv')
  })

  it('gates the anime pickers during a Plex connection change exactly like Movies/TV', async () => {
    // Mid-reconnect the disabled libraries query still serves the OLD server's
    // cached list; pre-fix the anime pickers stayed enabled, so a stale anime
    // root could be selected and saved (clearAnime*Root treats the changed
    // value as a deliberate re-selection). They must be disabled placeholders,
    // mirroring the Movies/TV pickers.
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByDisplayValue('http://plex:32400'), {
      target: { value: 'http://new-plex:32400' },
    })

    expect(screen.getByLabelText('Anime movies library folder')).toBeDisabled()
    expect(screen.getByLabelText('Anime TV library folder')).toBeDisabled()
    // Nothing stale selectable: saving carries no anime root from the old server.
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: 'new-token' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().anime_movie_root).toBe('')
    expect(lastBody().anime_tv_root).toBe('')
  })

  it('clears anime roots when the Plex connection changes and they are not re-picked', async () => {
    h.settingsData = {
      ...h.settingsData!,
      anime_movie_root: '/plex/anime-movies',
      anime_tv_root: '/plex/anime-tv',
    }
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByDisplayValue('http://plex:32400'), {
      target: { value: 'http://new-plex:32400' },
    })
    fireEvent.change(screen.getByLabelText('Token'), { target: { value: 'new-token' } })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().anime_movie_root).toBe('')
    expect(lastBody().anime_tv_root).toBe('')
  })

  it('keeps anime roots when the Plex connection is untouched', async () => {
    h.settingsData = {
      ...h.settingsData!,
      anime_movie_root: '/plex/anime-movies',
      anime_tv_root: '/plex/anime-tv',
    }
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.click(screen.getByRole('button', { name: /save changes/i }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))
    expect(lastBody().anime_movie_root).toBe('/plex/anime-movies')
    expect(lastBody().anime_tv_root).toBe('/plex/anime-tv')
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
      log_max_rows: null,
    }
    render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByLabelText('Pressure threshold (%)')).toHaveValue(90)
    expect(screen.getByLabelText('Pressure target (%)')).toHaveValue(80)
    expect(screen.getByLabelText('Eviction grace period (days)')).toHaveValue(30)
    expect(screen.getByLabelText('Eviction check interval (minutes)')).toHaveValue(30)
    expect(screen.getByLabelText('Log retention (days)')).toHaveValue(7)
    expect(screen.getByLabelText('Log retention (max rows)')).toHaveValue(100000)
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
    fireEvent.change(screen.getByLabelText('Log retention (max rows)'), {
      target: { value: '50000' },
    })
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
    expect(body.log_max_rows).toBe(50000)
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

describe('Settings — automatic container updates (ADR-0023)', () => {
  beforeEach(() => {
    h.mutateAsync.mockReset()
    h.mutateAsync.mockResolvedValue({})
    h.toast.mockReset()
    h.settingsData = CONFIGURED_SERVICES
    h.libraries = []
    h.librariesError = null
    h.statusLoading = false
    h.statusIsError = false
    h.statusError = null
    h.appKeyExists = false
  })

  it('prefills the opt-in policy with safe defaults and sends the complete policy on save', async () => {
    render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByRole('checkbox', { name: /^Enable automatic updates/i })).not.toBeChecked()
    expect(screen.getByLabelText('IANA timezone')).not.toHaveValue('')
    expect(screen.getByLabelText('Window start')).toHaveValue('03:00')
    expect(screen.getByLabelText('Window end')).toHaveValue('05:00')
    for (const day of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']) {
      expect(screen.getByRole('checkbox', { name: day })).toBeChecked()
    }
    expect(
      screen.getByRole('checkbox', { name: /^Wait for critical work to become idle/i }),
    ).toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))

    expect(lastBody()).toEqual(
      expect.objectContaining({
        automatic_updates_enabled: false,
        automatic_update_timezone: expect.any(String),
        automatic_update_weekdays: [
          'monday',
          'tuesday',
          'wednesday',
          'thursday',
          'friday',
          'saturday',
          'sunday',
        ],
        automatic_update_window_start: '03:00',
        automatic_update_window_end: '05:00',
        automatic_update_idle_only: true,
      }),
    )
  })

  it('round-trips edited policy and discloses overnight/day and channel semantics', async () => {
    h.settingsData = {
      ...CONFIGURED_SERVICES,
      automatic_updates_enabled: true,
      automatic_update_timezone: 'America/Toronto',
      automatic_update_weekdays: ['friday', 'saturday'],
      automatic_update_window_start: '23:00',
      automatic_update_window_end: '02:00',
      automatic_update_idle_only: false,
    }
    render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByRole('checkbox', { name: /^Enable automatic updates/i })).toBeChecked()
    expect(screen.getByLabelText('IANA timezone')).toHaveValue('America/Toronto')
    expect(screen.getByRole('checkbox', { name: 'Fri' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Sat' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Mon' })).not.toBeChecked()
    expect(screen.getByText(/overnight window belongs to the weekday on which it starts/i)).toBeInTheDocument()
    expect(screen.getByText(/controlled exclusively by/i)).toHaveTextContent('PLEX_MANAGER_IMAGE')

    fireEvent.change(screen.getByLabelText('IANA timezone'), { target: { value: 'UTC' } })
    fireEvent.click(screen.getByRole('checkbox', { name: 'Fri' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledTimes(1))

    expect(lastBody()).toEqual(
      expect.objectContaining({
        automatic_updates_enabled: true,
        automatic_update_timezone: 'UTC',
        automatic_update_weekdays: ['saturday'],
        automatic_update_window_start: '23:00',
        automatic_update_window_end: '02:00',
        automatic_update_idle_only: false,
      }),
    )
  })

  it('rejects an empty weekday set before sending a save', async () => {
    render(<Settings />, { wrapper: Wrapper })
    for (const day of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']) {
      fireEvent.click(screen.getByRole('checkbox', { name: day }))
    }
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() =>
      expect(h.toast).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Save failed',
          description: 'Select at least one automatic update weekday.',
          intent: 'error',
        }),
      ),
    )
    expect(h.mutateAsync).not.toHaveBeenCalled()
  })

  it('rejects an invalid timezone and equal window endpoints locally', async () => {
    const { rerender } = render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByLabelText('IANA timezone'), {
      target: { value: 'Toronto-ish' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(h.toast).toHaveBeenCalledWith(
        expect.objectContaining({ description: expect.stringContaining('valid IANA timezone') }),
      ),
    )
    expect(h.mutateAsync).not.toHaveBeenCalled()

    h.toast.mockReset()
    h.settingsData = { ...CONFIGURED_SERVICES, automatic_update_timezone: 'UTC' }
    // Re-mount so Settings' deliberately one-time form seeding picks up the
    // replacement fixture instead of preserving the invalid in-progress edit.
    rerender(<></>)
    render(<Settings />, { wrapper: Wrapper })
    fireEvent.change(screen.getByLabelText('Window end'), { target: { value: '03:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() =>
      expect(h.toast).toHaveBeenCalledWith(
        expect.objectContaining({ description: expect.stringContaining('start and end must differ') }),
      ),
    )
    expect(h.mutateAsync).not.toHaveBeenCalled()
  })
})

describe('Settings — Access recovery key (opt-in, ADR-0016)', () => {
  // The exact one-time reveal caption the operator must see (verbatim per the
  // Task 13 amendment) — asserted so a copy drift is a test failure.
  const CAPTION =
    'Store this somewhere safe. It can sign you in if plex.tv is down and authenticates API automations.'

  beforeEach(() => {
    h.rotateMutateAsync.mockReset()
    h.revokeMutateAsync.mockReset()
    h.toast.mockReset()
    h.rotatePending = false
    h.revokePending = false
    h.statusLoading = false
    h.statusIsError = false
    h.statusError = null
    h.statusRefetch.mockReset()
    h.appKeyExists = false
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
    }
    h.libraries = []
  })

  it('offers Generate (not Rotate/Revoke) when no recovery key exists', () => {
    h.appKeyExists = false
    render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByRole('button', { name: /generate recovery key/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^rotate$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^revoke$/i })).not.toBeInTheDocument()
    // Nothing is revealed until the operator generates one.
    expect(screen.queryByText(CAPTION)).not.toBeInTheDocument()
  })

  it('generates a key, reveals it exactly once with the storage caption, and flips to Rotate/Revoke', async () => {
    h.appKeyExists = false
    h.rotateMutateAsync.mockImplementation(async () => {
      h.appKeyExists = true // the mint endpoint now reports a key exists
      return { app_api_key: 'fresh-recovery-key' }
    })
    render(<Settings />, { wrapper: Wrapper })

    fireEvent.click(screen.getByRole('button', { name: /generate recovery key/i }))

    await waitFor(() => expect(screen.getByText('fresh-recovery-key')).toBeInTheDocument())
    expect(screen.getByText(CAPTION)).toBeInTheDocument()
    expect(h.rotateMutateAsync).toHaveBeenCalledTimes(1)
    // Minting flips the card to the key-exists controls.
    expect(screen.getByRole('button', { name: /^rotate$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^revoke$/i })).toBeInTheDocument()
  })

  it('offers Rotate + Revoke (no Generate, no key shown) when a key already exists', () => {
    h.appKeyExists = true
    render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByRole('button', { name: /^rotate$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^revoke$/i })).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /generate recovery key/i }),
    ).not.toBeInTheDocument()
    // The status endpoint never carries the plaintext, so nothing is shown on load.
    expect(screen.queryByText(CAPTION)).not.toBeInTheDocument()
  })

  it('rotates and re-reveals the new key once', async () => {
    h.appKeyExists = true
    h.rotateMutateAsync.mockResolvedValue({ app_api_key: 'rotated-recovery-key' })
    render(<Settings />, { wrapper: Wrapper })

    fireEvent.click(screen.getByRole('button', { name: /^rotate$/i }))

    await waitFor(() => expect(screen.getByText('rotated-recovery-key')).toBeInTheDocument())
    expect(screen.getByText(CAPTION)).toBeInTheDocument()
    expect(h.rotateMutateAsync).toHaveBeenCalledTimes(1)
  })

  it('revokes behind an in-app confirm dialog (never window.confirm) and flips back to Generate', async () => {
    h.appKeyExists = true
    h.revokeMutateAsync.mockImplementation(async () => {
      h.appKeyExists = false // the key no longer exists after a revoke
    })
    // A native window.confirm would block automation + break the app's UX
    // conventions — assert it is NEVER used.
    const confirmSpy = vi.spyOn(window, 'confirm')
    render(<Settings />, { wrapper: Wrapper })

    fireEvent.click(screen.getByRole('button', { name: /^revoke$/i }))

    expect(confirmSpy).not.toHaveBeenCalled()
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(/revoke the recovery key\?/i)).toBeInTheDocument()
    // Not revoked until confirmed.
    expect(h.revokeMutateAsync).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: /revoke key/i }))

    await waitFor(() => expect(h.revokeMutateAsync).toHaveBeenCalledTimes(1))
    // Status flips to no-key: the Generate control returns, Rotate/Revoke are gone.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /generate recovery key/i })).toBeInTheDocument(),
    )
    expect(screen.queryByRole('button', { name: /^revoke$/i })).not.toBeInTheDocument()

    confirmSpy.mockRestore()
  })

  it('cancelling the revoke dialog revokes nothing', () => {
    h.appKeyExists = true
    render(<Settings />, { wrapper: Wrapper })

    fireEvent.click(screen.getByRole('button', { name: /^revoke$/i }))
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /^cancel$/i }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(h.revokeMutateAsync).not.toHaveBeenCalled()
  })

  it('shows the honest error (never a blind Generate) when the status fetch persistently fails', async () => {
    // A persistent status 5xx leaves `exists` underivable. The pre-fix code fell
    // through to the no-key "Generate recovery key" button — but the single mint
    // endpoint ROTATES an existing key, so clicking it would silently invalidate
    // every other device/automation. The error branch must surface the failure
    // and offer only a Retry, never a destructive action on unknown state.
    h.statusIsError = true
    h.statusError = {
      code: 'upstream_error',
      message: 'An upstream service failed. Try again shortly.',
      status: 500,
    }
    render(<Settings />, { wrapper: Wrapper })

    expect(screen.getByRole('alert')).toHaveTextContent('An upstream service failed')
    // No mint control of ANY kind is offered while the key's existence is unknown.
    expect(
      screen.queryByRole('button', { name: /generate recovery key/i }),
    ).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^rotate$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^revoke$/i })).not.toBeInTheDocument()

    // Retry re-runs the status query so the operator can recover without a reload.
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(h.statusRefetch).toHaveBeenCalledTimes(1)
  })

  it('surfaces a rotate CAS conflict (app_key_changed) honestly, matching the production copy', async () => {
    h.appKeyExists = true
    // What production renders: unwrap() throws a normalized ApiError whose message
    // is the crafted DETAIL_MESSAGES copy for this code (lib/errors.ts, Task 11).
    h.rotateMutateAsync.mockRejectedValueOnce({
      code: 'app_key_changed',
      message: 'The recovery key changed while you were rotating it. Refresh and try again.',
      status: 409,
    })
    render(<Settings />, { wrapper: Wrapper })

    fireEvent.click(screen.getByRole('button', { name: /^rotate$/i }))

    await waitFor(() => expect(h.toast).toHaveBeenCalledTimes(1))
    expect(h.toast).toHaveBeenCalledWith(
      expect.objectContaining({
        intent: 'error',
        description: expect.stringContaining('recovery key changed while you were rotating it'),
      }),
    )
    // No dead key painted as if the rotation had succeeded.
    expect(screen.queryByText(CAPTION)).not.toBeInTheDocument()
  })
})
