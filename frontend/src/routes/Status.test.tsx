import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useEvict, useOpsDisk, useOpsHealth, useSettings } from '../api/hooks'
import type { DiskResponse, EvictResponse, HealthResponse, SettingsResponse } from '../api/types'
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

describe('Status', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // `clearAllMocks` only clears call history, not a previously-set
    // `mockReturnValue` — re-seed the default explicitly so one test's
    // settings override can't leak into the next.
    ;(useSettings as unknown as Mock).mockReturnValue({ data: undefined, isLoading: false, isError: false })
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
      'Subsystems',
      'Background loops',
      'Disk',
    ])
    expect(screen.getByRole('heading', { level: 3, name: 'plex' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Reconcile loop' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'movies_root' })).toBeInTheDocument()

    const cards = container.querySelectorAll('article')
    expect(cards).toHaveLength(6)
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
      data: health({ reconcile: freshLoop, autograb: freshLoop }),
      isLoading: false,
      isError: false,
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    // Never — a fresh boot must not read as "clean" just because failures==0.
    expect(screen.queryByText('running clean')).not.toBeInTheDocument()
    // Both the reconcile AND auto-grab panels show the honest "starting up".
    expect(screen.getAllByText('starting up')).toHaveLength(2)
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

  it('keeps cached health and disk snapshots visible when a background refetch fails', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({
      data: health(),
      isLoading: false,
      isError: true,
      error: { code: 'unknown_error', message: 'Health refresh failed', status: 0 },
    })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk(),
      isLoading: false,
      isError: true,
      error: { code: 'unknown_error', message: 'Disk refresh failed', status: 0 },
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />, { wrapper: Wrapper })

    expect(screen.getByRole('heading', { level: 3, name: 'plex' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Reconcile loop' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'movies_root' })).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load health")).not.toBeInTheDocument()
    expect(screen.queryByText("Couldn't load disk usage")).not.toBeInTheDocument()
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
