import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useCheckForUpdate,
  useEvict,
  useOpsDisk,
  useOpsHealth,
  useSettings,
  useUpdateStatus,
  useUpdateWhenReady,
} from '../api/hooks'
import type {
  DiskResponse,
  EvictResponse,
  HealthResponse,
  SettingsResponse,
  UpdateStatusResponse,
} from '../api/types'
import { Status } from './Status'

// Status's disk-root error branch renders a LinkButton (a react-router <Link>),
// so every render needs a Router in the tree -- mirrors Settings.test.tsx.
const Wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

// No network: the ops hooks are replaced with controllable stand-ins so the
// test exercises only the Status page's own rendering + wiring.
vi.mock('../api/hooks', () => ({
  useOpsHealth: vi.fn(),
  useOpsDisk: vi.fn(),
  useEvict: vi.fn(),
  useUpdateStatus: vi.fn(),
  useCheckForUpdate: vi.fn(),
  useUpdateWhenReady: vi.fn(),
  // Most tests don't care about settings; default to "not loaded yet" so the
  // disk-threshold fallback constants kick in unless a test overrides this.
  useSettings: vi.fn(() => ({ data: undefined, isLoading: false, isError: false })),
}))

const toastMock = vi.fn()
vi.mock('../components/ui/toast', () => ({ useToast: () => ({ toast: toastMock }) }))

function health(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return {
    subsystems: [
      { name: 'plex', status: 'ok', detail: null, checked_at: '2026-01-01T00:00:00Z' },
      { name: 'prowlarr', status: 'down', detail: 'connection refused', checked_at: '2026-01-01T00:00:00Z' },
      { name: 'tmdb', status: 'not_configured', detail: null, checked_at: '2026-01-01T00:00:00Z' },
    ],
    disks: [],
    reconcile: {
      last_run_at: '2026-01-01T00:00:00Z',
      last_ok_at: '2026-01-01T00:00:00Z',
      last_error_type: null,
      last_error_at: null,
      consecutive_failures: 0,
    },
    autograb: {
      last_run_at: '2026-01-01T00:00:00Z',
      last_ok_at: '2026-01-01T00:00:00Z',
      last_error_type: null,
      last_error_at: null,
      consecutive_failures: 0,
      cooled_down_scopes: 0,
    },
    watchlist: {
      state: 'ok',
      last_run_at: '2026-01-01T00:00:00Z',
      last_ok_at: '2026-01-01T00:00:00Z',
      last_error_type: null,
      last_error_at: null,
      fetched: 0,
      created: 0,
      existing: 0,
      failed_users: 0,
      failed_entries: 0,
    },
    ...overrides,
  }
}

type DiskRoot = DiskResponse['roots'][number]

// A standalone factory (rather than indexing into `disk().roots[0]`) so
// per-test overrides don't rely on array access, which — under this project's
// `noUncheckedIndexedAccess` — types as `DiskRoot | undefined` and would make
// every spread property optional under `exactOptionalPropertyTypes`.
function diskRoot(overrides: Partial<DiskRoot> = {}): DiskRoot {
  return {
    root: 'movies_root',
    path: '/library/movies',
    total_bytes: 1000,
    available_bytes: 100,
    used_percent: 90,
    error: null,
    candidates: [
      {
        request_id: 1,
        media_type: 'movie',
        title: 'Old Watched Movie',
        season: null,
        status: 'available',
        last_viewed_at: '2025-01-01T00:00:00Z',
        size_percent: 4.2,
        library_path: '/library/movies/Old Watched Movie',
      },
    ],
    ...overrides,
  }
}

function disk(overrides: Partial<DiskResponse> = {}): DiskResponse {
  return {
    roots: [diskRoot()],
    ...overrides,
  }
}

function updateStatus(overrides: Partial<UpdateStatusResponse> = {}): UpdateStatusResponse {
  return {
    state: 'idle',
    updater_available: true,
    current_build: '1.4.0',
    current_digest: 'sha256:current',
    available_build: null,
    available_digest: null,
    channel: 'stable',
    next_window_start: '2026-07-13T07:00:00Z',
    next_window_end: '2026-07-13T09:00:00Z',
    blocker: null,
    last_checked_at: null,
    last_result: null,
    ...overrides,
  }
}

const checkMutateAsync = vi.fn()
const updateMutateAsync = vi.fn()
const updatesRefetch = vi.fn()

describe('Status', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // `clearAllMocks` only clears call history, not a previously-set
    // `mockReturnValue` — re-seed the default explicitly so one test's
    // settings override can't leak into the next.
    ;(useSettings as unknown as Mock).mockReturnValue({ data: undefined, isLoading: false, isError: false })
    ;(useUpdateStatus as unknown as Mock).mockReturnValue({
      data: updateStatus(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: updatesRefetch,
    })
    ;(useCheckForUpdate as unknown as Mock).mockReturnValue({
      mutateAsync: checkMutateAsync,
      isPending: false,
    })
    ;(useUpdateWhenReady as unknown as Mock).mockReturnValue({
      mutateAsync: updateMutateAsync,
      isPending: false,
    })
    checkMutateAsync.mockResolvedValue(updateStatus({ state: 'checking' }))
    updateMutateAsync.mockResolvedValue(updateStatus({ state: 'waiting_for_window' }))
  })

  it('uses the shared heading hierarchy and canonical dense card grammar', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    const { container } = render(<Status />, { wrapper: Wrapper })

    const pageHeading = screen.getByRole('heading', { level: 1, name: 'Status' })
    expect(pageHeading).toBeInTheDocument()
    expect(pageHeading.closest('header')?.parentElement).toHaveClass(
      'max-w-[1160px]',
      'px-5',
      'sm:px-8',
      'lg:px-11',
    )
    expect(screen.getByRole('button', { name: 'Free space now' })).toHaveClass('h-8')
    expect(screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent)).toEqual([
      'Updates',
      'Subsystems',
      'Background loops',
      'Disk',
    ])
    expect(screen.getByRole('heading', { level: 3, name: 'plex' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Reconcile loop' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'movies_root' })).toBeInTheDocument()

    const cards = container.querySelectorAll('article')
    expect(cards).toHaveLength(8)
    for (const card of cards) {
      expect(card).toHaveClass(
        'rounded-[10px]',
        'border-hairline',
        'bg-surface',
        'px-[14px]',
        'py-[11px]',
      )
      expect(card).not.toHaveClass('p-4')
    }
  })

  it('renders updater availability, build/channel/window/blocker, and the last reported outcome', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
    ;(useUpdateStatus as unknown as Mock).mockReturnValue({
      data: updateStatus({
        state: 'waiting_for_idle',
        available_build: '1.5.0',
        last_checked_at: '2026-07-12T12:00:00Z',
        blocker: 'active_critical_work',
        last_result: {
          operation: 'check',
          outcome: 'update_available',
          finished_at: '2026-07-12T12:00:00Z',
          from_build: '1.4.0',
          to_build: '1.5.0',
          detail_code: null,
        },
      }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: updatesRefetch,
    })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByText('Waiting for critical work')).toBeInTheDocument()
    expect(screen.getByText('Sidecar connected')).toBeInTheDocument()
    expect(screen.getByText('1.4.0')).toBeInTheDocument()
    expect(screen.getAllByText('1.5.0').length).toBeGreaterThan(0)
    expect(screen.getByText('stable')).toBeInTheDocument()
    expect(screen.getByText('active critical work')).toBeInTheDocument()
    expect(screen.getByText(/check · update available/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update queued' })).toBeDisabled()
  })

  it.each([
    ['installing', 'Installing update'],
    ['rollback', 'Rolling back'],
  ] as const)('renders the active %s phase and disables update actions', (state, label) => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
    ;(useUpdateStatus as unknown as Mock).mockReturnValue({
      data: updateStatus({ state }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: updatesRefetch,
    })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByText(label)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check now' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Update when ready' })).toBeDisabled()
  })

  it('keeps the manual update action available outside the automatic schedule window', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
    ;(useUpdateStatus as unknown as Mock).mockReturnValue({
      data: updateStatus({
        state: 'waiting_for_window',
        available_build: '1.5.0',
        blocker: 'outside_update_window',
      }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: updatesRefetch,
    })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByText('Waiting for update window')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Update when ready' })).toBeEnabled()
  })

  it('keeps manual actions available when automatic updates are disabled', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
    ;(useUpdateStatus as unknown as Mock).mockReturnValue({
      data: updateStatus({
        state: 'disabled',
        blocker: 'automatic_updates_disabled',
      }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: updatesRefetch,
    })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByText('Automatic updates disabled')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check now' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Update when ready' })).toBeEnabled()
  })

  it('renders queued install preflight honestly and prevents duplicate actions', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
    ;(useUpdateStatus as unknown as Mock).mockReturnValue({
      data: updateStatus({ state: 'checking', blocker: 'checking_for_update' }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: updatesRefetch,
    })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByText('Checking for an update')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check now' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Update when ready' })).toBeDisabled()
  })

  it('disables actions when the sidecar is unavailable', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
    ;(useUpdateStatus as unknown as Mock).mockReturnValue({
      data: updateStatus({
        state: 'unavailable',
        updater_available: false,
        blocker: 'updater_unavailable',
      }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: updatesRefetch,
    })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByText('Sidecar not connected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Check now' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Update when ready' })).toBeDisabled()
    expect(screen.getByText(/Enable the automatic-update Compose profile/)).toBeInTheDocument()
  })

  it('describes action acceptance without claiming the check or installation completed', async () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    fireEvent.click(screen.getByRole('button', { name: 'Check now' }))
    await waitFor(() => expect(checkMutateAsync).toHaveBeenCalledTimes(1))
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Update check requested',
        description: expect.stringContaining('when the check finishes'),
      }),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Update when ready' }))
    await waitFor(() => expect(updateMutateAsync).toHaveBeenCalledTimes(1))
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Update requested',
        description: expect.stringContaining('does not wait for the automatic schedule window'),
      }),
    )
    expect(toastMock).not.toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringMatching(/complete|installed/i) }),
    )
  })

  it('renders a card per subsystem with its honest status label', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByText('plex')).toBeInTheDocument()
    expect(screen.getByText('Healthy')).toBeInTheDocument()
    expect(screen.getByText('Down')).toBeInTheDocument()
    expect(screen.getByText('connection refused')).toBeInTheDocument()
    // not_configured is never confused with down.
    expect(screen.getByText('Not configured')).toBeInTheDocument()
  })

  it('renders a subsystem\'s non-blocking note under its detail, distinct from a failure detail (issues #133/#157)', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({
      data: health({
        subsystems: [
          {
            name: 'qbittorrent',
            status: 'ok',
            detail: null,
            note: "the client's default save path isn't visible inside this container",
            checked_at: '2026-01-01T00:00:00Z',
          },
        ],
      }),
      isLoading: false,
      isError: false,
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    // Still reads "Healthy" -- the note never flips status -- but the caution
    // is surfaced, not swallowed.
    expect(screen.getByText('Healthy')).toBeInTheDocument()
    expect(
      screen.getByText(/default save path isn't visible inside this container/),
    ).toBeInTheDocument()
  })

  it('renders no note line when a subsystem has none', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.queryByText(/⚠/)).not.toBeInTheDocument()
  })

  it('renders the disk usage bar and eviction-candidate preview per root', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByText('movies_root')).toBeInTheDocument()
    expect(screen.getByText('90% used')).toBeInTheDocument()
    const gauge = screen.getByRole('progressbar', { name: 'movies_root disk usage' })
    expect(gauge).toHaveAttribute('aria-valuemin', '0')
    expect(gauge).toHaveAttribute('aria-valuemax', '100')
    expect(gauge).toHaveAttribute('aria-valuenow', '90')
    expect(gauge.firstElementChild).toHaveStyle({ width: '90%' })
    expect(gauge.firstElementChild).toHaveClass(
      'motion-safe:transition-[width]',
      'motion-safe:duration-500',
    )
    expect(screen.getByText(/eviction candidates/i)).toBeInTheDocument()
    expect(screen.getByText(/Old Watched Movie/)).toBeInTheDocument()
  })

  it('colors the disk gauge by the same pressure tiers the sweep uses (truthful, not decorative)', () => {
    // The bar color must key off the SAME two settings the eviction sweep reads
    // (threshold/target), so a red bar always means "a sweep would evict here".
    // Defaults (settings unloaded): threshold 90%, target 80%.
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk({
        roots: [
          diskRoot({ root: 'over_threshold', used_percent: 95, candidates: [] }),
          diskRoot({ root: 'between_target_threshold', used_percent: 85, candidates: [] }),
          diskRoot({ root: 'below_target', used_percent: 50, candidates: [] }),
        ],
      }),
      isLoading: false,
      isError: false,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    // At/over the pressure threshold -> the "a sweep would evict" red.
    expect(
      screen.getByRole('progressbar', { name: 'over_threshold disk usage' }).firstElementChild,
    ).toHaveClass('bg-error')
    // Above target but below threshold -> the cautionary tone, not red.
    const caution = screen.getByRole('progressbar', { name: 'between_target_threshold disk usage' })
      .firstElementChild
    expect(caution).toHaveClass('bg-searching')
    expect(caution).not.toHaveClass('bg-error')
    // Comfortably below target -> the healthy tone.
    expect(
      screen.getByRole('progressbar', { name: 'below_target disk usage' }).firstElementChild,
    ).toHaveClass('bg-available')
  })

  it('re-tiers the disk gauge color against a settings-supplied threshold, not a hardcoded one', () => {
    // A root at 85% is healthy (green) under a 95%/90% policy, but the same
    // percentage is cautionary under the default 90%/80% — the color must track
    // the operator's actual settings, never a baked-in cutoff.
    ;(useSettings as unknown as Mock).mockReturnValue({
      data: { disk_pressure_threshold_percent: 95, disk_pressure_target_percent: 90 } as SettingsResponse,
      isLoading: false,
      isError: false,
    })
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk({ roots: [diskRoot({ root: 'movies_root', used_percent: 85, candidates: [] })] }),
      isLoading: false,
      isError: false,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    const fill = screen.getByRole('progressbar', { name: 'movies_root disk usage' }).firstElementChild
    expect(fill).toHaveClass('bg-available')
    expect(fill).not.toHaveClass('bg-searching')
    expect(fill).not.toHaveClass('bg-error')
  })

  it('surfaces an unreadable root\'s error instead of a fabricated gauge, with a Fix-in-Settings link', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk({
        roots: [
          diskRoot({
            root: 'tv_root',
            path: '/library/tv',
            total_bytes: 0,
            available_bytes: 0,
            used_percent: 0,
            error: 'No such file or directory',
            candidates: [],
          }),
        ],
      }),
      isLoading: false,
      isError: false,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })
    expect(screen.getByText(/isn't visible to Plex Manager/)).toBeInTheDocument()
    expect(screen.getByText('No such file or directory')).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    const link = screen.getByRole('link', { name: /fix in settings/i })
    expect(link).toHaveAttribute('href', '/settings')
  })

  it('"Free space now" triggers the evict mutation and reports an honest outcome', async () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    const evicted: EvictResponse = { evicted: [] }
    const mutateAsync = vi.fn().mockResolvedValue(evicted)
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync, isPending: false })

    render(<Status />, { wrapper: Wrapper })
    fireEvent.click(screen.getByRole('button', { name: /free space now/i }))

    await waitFor(() => expect(mutateAsync).toHaveBeenCalled())
    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({ title: 'Nothing to free' }),
      ),
    )
  })

  it('shows a warning toast naming the failed root(s) on a partial sweep, while still reporting freed space', async () => {
    // R6-C: the movies root evicted, but the tv root's OWN sweep raised (e.g.
    // a transient PlexLibraryError). The backend still returns 200 with the
    // movie outcome in `evicted` AND the tv failure in `errors` -- the UI
    // must surface BOTH: the success toast for whatever freed, plus a
    // SEPARATE warning naming exactly which root(s) failed, never silently
    // dropping the failure just because something else succeeded.
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    const partial: EvictResponse = {
      evicted: [
        {
          request_id: 1,
          media_type: 'movie',
          title: 'Old Watched Movie',
          season: null,
          library_path: '/library/movies/Old Watched Movie',
          freed_bytes: 1024,
        },
      ],
      errors: [{ root: 'tv_root', detail: 'sweep failed (PlexLibraryError)' }],
    }
    const mutateAsync = vi.fn().mockResolvedValue(partial)
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync, isPending: false })

    render(<Status />, { wrapper: Wrapper })
    fireEvent.click(screen.getByRole('button', { name: /free space now/i }))

    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({ title: 'Freed 1 title', intent: 'success' }),
      ),
    )
    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          title: expect.stringContaining('tv_root'),
          intent: 'warning',
        }),
      ),
    )
  })

  it('does not overstate reconcile health before the first cycle has run', () => {
    const freshLoop = {
      last_run_at: null,
      last_ok_at: null,
      last_error_type: null,
      last_error_at: null,
      consecutive_failures: 0,
      cooled_down_scopes: 0,
    }
    ;(useOpsHealth as unknown as Mock).mockReturnValue({
      // Both background loops are fresh (never run), so neither panel may read
      // as "clean" just because failures==0.
      data: health({
        reconcile: freshLoop,
        autograb: freshLoop,
        watchlist: {
          state: 'starting',
          last_run_at: null,
          last_ok_at: null,
          last_error_type: null,
          last_error_at: null,
          fetched: 0,
          created: 0,
          existing: 0,
          failed_users: 0,
          failed_entries: 0,
        },
      }),
      isLoading: false,
      isError: false,
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    // Never — a fresh boot must not read as "clean" just because failures==0.
    expect(screen.queryByText('running clean')).not.toBeInTheDocument()
    // All three background panels show the honest "starting up".
    expect(screen.getAllByText('starting up')).toHaveLength(3)
    // Both "Last run" and "Last success" render the same honest placeholder.
    const neverValues = screen.getAllByText('never')
    expect(neverValues.length).toBeGreaterThan(0)
    for (const value of neverValues) {
      expect(value.tagName).toBe('DD')
      expect(value).toHaveClass('text-right', 'text-ink', 'tabular-nums')
    }
  })

  it('keeps loop statistics as semantic key/value pairs with honest emphasis', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({
      data: health({
        reconcile: {
          last_run_at: '2026-01-01T00:00:00Z',
          last_ok_at: '2025-12-31T23:59:00Z',
          last_error_type: 'ReconcileDeadlineExceeded',
          last_error_at: '2026-01-01T00:00:00Z',
          consecutive_failures: 2,
        },
        autograb: {
          last_run_at: '2026-01-01T00:00:00Z',
          last_ok_at: '2025-12-31T23:59:00Z',
          last_error_type: 'GrabPipelineUnavailable',
          last_error_at: '2026-01-01T00:00:00Z',
          consecutive_failures: 1,
          cooled_down_scopes: 3,
        },
      }),
      isLoading: false,
      isError: false,
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    const reconcileCard = screen
      .getByRole('heading', { level: 3, name: 'Reconcile loop' })
      .closest('article')
    expect(reconcileCard).not.toBeNull()
    const reconcileStats = within(reconcileCard as HTMLElement)
    expect(reconcileStats.getByText('Consecutive failures').tagName).toBe('DT')
    expect(reconcileStats.getByText('2')).toHaveClass(
      'text-right',
      'tabular-nums',
      'text-error',
    )
    expect(reconcileStats.getByText(/ReconcileDeadlineExceeded/)).toHaveClass(
      'text-right',
      'tabular-nums',
      'text-error',
    )

    const autograbCard = screen
      .getByRole('heading', { level: 3, name: 'Auto-grab loop' })
      .closest('article')
    expect(autograbCard).not.toBeNull()
    const autograbStats = within(autograbCard as HTMLElement)
    expect(autograbStats.getByText('Cooling scopes').tagName).toBe('DT')
    expect(autograbStats.getByText('3')).toHaveClass(
      'text-right',
      'tabular-nums',
      'text-searching',
    )
    expect(autograbStats.getByText(/GrabPipelineUnavailable/)).toHaveClass('text-error')
  })

  it('renders a degraded watchlist cycle without overwriting its last successful run', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({
      data: health({
        watchlist: {
          state: 'degraded',
          last_run_at: '2026-01-02T00:00:00Z',
          last_ok_at: '2026-01-01T00:00:00Z',
          last_error_type: 'WatchlistEntryError',
          last_error_at: '2026-01-02T00:00:01Z',
          fetched: 7,
          created: 2,
          existing: 4,
          failed_users: 1,
          failed_entries: 3,
        },
      }),
      isLoading: false,
      isError: false,
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk(),
      isLoading: false,
      isError: false,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    const heading = screen.getByRole('heading', { name: 'Watchlist sync' })
    const panel = heading.closest('article')
    expect(panel).not.toBeNull()
    const watchlist = within(panel as HTMLElement)
    expect(watchlist.getByText('degraded')).toBeInTheDocument()
    expect(watchlist.getByText('Existing requests')).toBeInTheDocument()
    expect(watchlist.getByText('4')).toBeInTheDocument()
    expect(watchlist.getByText('1')).toBeInTheDocument()
    expect(watchlist.getByText('Failed entries')).toBeInTheDocument()
    expect(watchlist.getByText('3')).toBeInTheDocument()
    expect(watchlist.getByText(/WatchlistEntryError/)).toBeInTheDocument()

    const lastRun = watchlist.getByText('Last run').nextElementSibling
    const lastSuccess = watchlist.getByText('Last success').nextElementSibling
    expect(lastRun).not.toBeNull()
    expect(lastSuccess).not.toBeNull()
    expect(lastRun).not.toHaveTextContent('never')
    expect(lastSuccess).not.toHaveTextContent('never')
    expect(lastRun?.textContent).not.toBe(lastSuccess?.textContent)
  })

  it('surfaces how many scopes are in a grab-pipeline cooldown', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({
      data: health({
        autograb: {
          last_run_at: '2026-01-01T00:00:00Z',
          last_ok_at: '2026-01-01T00:00:00Z',
          last_error_type: null,
          last_error_at: null,
          consecutive_failures: 0,
          cooled_down_scopes: 3,
        },
      }),
      isLoading: false,
      isError: false,
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    // The operator SEES the grab pipeline failing: a labelled, non-zero count.
    expect(screen.getByText('Cooling scopes')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
  })

  it('notes that a candidate preview is not currently under pressure', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk({ roots: [diskRoot({ used_percent: 40 })] }),
      isLoading: false,
      isError: false,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    // Candidates are listed (backend preview has no pressure gate), but the
    // page must say so isn't currently evicted — a healthy disk contradicting
    // "Free space now" reporting nothing to free would otherwise look broken.
    expect(screen.getByText(/only evicted once this root reaches 90% used/i)).toBeInTheDocument()
  })

  it('reads the eviction threshold from settings rather than a hardcoded value', () => {
    ;(useSettings as unknown as Mock).mockReturnValue({
      data: { disk_pressure_threshold_percent: 95, disk_pressure_target_percent: 85 } as SettingsResponse,
      isLoading: false,
      isError: false,
    })
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false }) // used_percent: 90
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    // 90% used is below the (settings-supplied) 95% threshold — not under
    // pressure, even though it would have been against the hardcoded default.
    expect(screen.getByText(/only evicted once this root reaches 95% used/i)).toBeInTheDocument()
  })

  it('uses the canonical no-root empty state without losing its correction guidance', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk({ roots: [] }),
      isLoading: false,
      isError: false,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    const title = screen.getByText('No library root configured')
    expect(title.closest('[role="status"]')).toHaveClass('rounded-[10px]', 'border-dashed')
    expect(
      screen.getByText('Set a Movies or TV library folder in Settings to see disk usage.'),
    ).toBeInTheDocument()
  })

  it('keeps cached snapshots visible on a failed background refetch, but never silently stale', () => {
    // Stale beats blank — the last good snapshot stays up — but the page must
    // SAY the probe is failing (north star #3), per query, with its own retry.
    const healthRefetch = vi.fn()
    const diskRefetch = vi.fn()
    ;(useOpsHealth as unknown as Mock).mockReturnValue({
      data: health(),
      isLoading: false,
      isError: true,
      error: { code: 'unknown_error', message: 'Health refresh failed', status: 0 },
      refetch: healthRefetch,
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk(),
      isLoading: false,
      isError: true,
      error: { code: 'unknown_error', message: 'Disk refresh failed', status: 0 },
      refetch: diskRefetch,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    // The cached cards are all still on screen…
    expect(screen.getByRole('heading', { level: 3, name: 'plex' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Reconcile loop' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'movies_root' })).toBeInTheDocument()
    // …without the blank-view error states (those are for the no-cache case)…
    expect(screen.queryByText("Couldn't load health")).not.toBeInTheDocument()
    expect(screen.queryByText("Couldn't load disk usage")).not.toBeInTheDocument()
    // …but each failing probe announces its staleness, naming its own error.
    const healthNotice = screen.getByText(/couldn't refresh health \(Health refresh failed\)/i)
    expect(healthNotice.closest('[role="status"]')).toHaveClass('text-error')
    expect(healthNotice.textContent).toMatch(/showing the last known snapshot/i)
    const diskNotice = screen.getByText(/couldn't refresh disk usage \(Disk refresh failed\)/i)
    expect(diskNotice.closest('[role="status"]')).toHaveClass('text-error')

    // Each notice's Retry drives ITS query's refetch, not the other's.
    const retries = screen.getAllByRole('button', { name: /^retry$/i })
    expect(retries).toHaveLength(2)
    fireEvent.click(retries[0] as HTMLElement)
    expect(healthRefetch).toHaveBeenCalledTimes(1)
    expect(diskRefetch).not.toHaveBeenCalled()
    fireEvent.click(retries[1] as HTMLElement)
    expect(diskRefetch).toHaveBeenCalledTimes(1)
    expect(healthRefetch).toHaveBeenCalledTimes(1)
  })

  it('shows no staleness notice while a refetch is merely succeeding', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.queryByText(/couldn't refresh/i)).not.toBeInTheDocument()
  })

  it('shows a retry action when the health read fails', () => {
    const refetch = vi.fn()
    ;(useOpsHealth as unknown as Mock).mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { code: 'unknown_error', message: 'Network down', status: 0 },
      refetch,
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })
    expect(screen.getByText(/couldn't load health/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(refetch).toHaveBeenCalled()
  })

  it('keeps the disk failure actionable with its own Retry button', () => {
    const refetch = vi.fn()
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { code: 'unknown_error', message: 'Disk probe failed', status: 0 },
      refetch,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })
    expect(screen.getByText(/couldn't load disk usage/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(refetch).toHaveBeenCalled()
  })
})
