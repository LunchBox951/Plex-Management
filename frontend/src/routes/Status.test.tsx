import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useEvict, useOpsDisk, useOpsHealth, useSettings } from '../api/hooks'
import type { DiskResponse, EvictResponse, HealthResponse, SettingsResponse } from '../api/types'
import { Status } from './Status'

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

  it('renders a card per subsystem with its honest status label', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />)

    expect(screen.getByText('plex')).toBeInTheDocument()
    expect(screen.getByText('Healthy')).toBeInTheDocument()
    expect(screen.getByText('Down')).toBeInTheDocument()
    expect(screen.getByText('connection refused')).toBeInTheDocument()
    // not_configured is never confused with down.
    expect(screen.getByText('Not configured')).toBeInTheDocument()
  })

  it('renders the disk usage bar and eviction-candidate preview per root', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />)

    expect(screen.getByText('movies_root')).toBeInTheDocument()
    expect(screen.getByText('90% used')).toBeInTheDocument()
    expect(screen.getByText(/eviction candidates/i)).toBeInTheDocument()
    expect(screen.getByText(/Old Watched Movie/)).toBeInTheDocument()
  })

  it('surfaces an unreadable root\'s error instead of a fabricated gauge', () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({
      data: disk({
        roots: [
          {
            root: 'tv_root',
            path: '/library/tv',
            total_bytes: 0,
            available_bytes: 0,
            used_percent: 0,
            error: 'No such file or directory',
            candidates: [],
          },
        ],
      }),
      isLoading: false,
      isError: false,
    })
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Status />)
    expect(screen.getByText('No such file or directory')).toBeInTheDocument()
  })

  it('"Free space now" triggers the evict mutation and reports an honest outcome', async () => {
    ;(useOpsHealth as unknown as Mock).mockReturnValue({ data: health(), isLoading: false, isError: false })
    ;(useOpsDisk as unknown as Mock).mockReturnValue({ data: disk(), isLoading: false, isError: false })
    const evicted: EvictResponse = { evicted: [] }
    const mutateAsync = vi.fn().mockResolvedValue(evicted)
    ;(useEvict as unknown as Mock).mockReturnValue({ mutateAsync, isPending: false })

    render(<Status />)
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

    render(<Status />)
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

    render(<Status />)

    // Never — a fresh boot must not read as "clean" just because failures==0.
    expect(screen.queryByText('running clean')).not.toBeInTheDocument()
    // Both the reconcile AND auto-grab panels show the honest "starting up".
    expect(screen.getAllByText('starting up')).toHaveLength(2)
    // Both "Last run" and "Last success" render the same honest placeholder.
    expect(screen.getAllByText('never').length).toBeGreaterThan(0)
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

    render(<Status />)

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

    render(<Status />)

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

    render(<Status />)

    // 90% used is below the (settings-supplied) 95% threshold — not under
    // pressure, even though it would have been against the hardcoded default.
    expect(screen.getByText(/only evicted once this root reaches 95% used/i)).toBeInTheDocument()
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

    render(<Status />)
    expect(screen.getByText(/couldn't load health/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(refetch).toHaveBeenCalled()
  })
})
