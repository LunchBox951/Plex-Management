/** Regression tests for mutation cache invalidation behavior. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useCheckForUpdate,
  useCreateRequest,
  useEvict,
  useDiscoverHome,
  useExchangeApiKey,
  useMarkFailed,
  useQueue,
  useRelocateDownload,
  useRequests,
  useRequestsInvalidated,
  useRevokeAppKey,
  useRotateAppKey,
  useTileLiveStates,
  useTitleRequests,
  useUpdateSettings,
  useUpdateStatus,
  useUpdateWhenReady,
  useWithdrawSubscription,
} from './hooks'
import { client } from './client'
import {
  POLL_INTERVAL_MS,
  QUERY_STALE_TIME_MS,
  QUEUE_REALTIME_FLOOR_MS,
  REQUESTS_POLL_INTERVAL_MS,
  REQUESTS_REALTIME_FLOOR_MS,
  UPDATE_STATUS_POLL_INTERVAL_MS,
  queryKeys,
} from '../lib/queryClient'
import { setRealtimeConnected } from '../lib/realtimeState'
import type {
  AppApiKeyResponse,
  AuthMeResponse,
  EvictResponse,
  PlexLibraryOption,
  QueueItem,
  SettingsResponse,
  UpdateStatusResponse,
} from './types'

// No network: the typed client is replaced with controllable GET/PUT/POST mocks.
vi.mock('./client', () => ({
  client: { GET: vi.fn(), PUT: vi.fn(), POST: vi.fn(), DELETE: vi.fn() },
}))

function createWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

beforeEach(() => {
  vi.mocked(client.GET).mockReset()
  vi.mocked(client.POST).mockReset()
  vi.mocked(client.PUT).mockReset()
  vi.mocked(client.DELETE).mockReset()
  setRealtimeConnected(false)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useUpdateSettings', () => {
  it('removes stale Plex library picker data when the Plex connection changes', async () => {
    const saved: SettingsResponse = { plex_url: 'http://new-plex:32400' }
    ;(client.PUT as unknown as Mock).mockResolvedValue({ data: saved, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const oldLibraries: PlexLibraryOption[] = [
      {
        path: '/old-plex/movies',
        section_key: '1',
        section_type: 'movie',
        title: 'Movies',
        writable: true,
      },
    ]
    qc.setQueryData(queryKeys.settings, { plex_url: 'http://old-plex:32400' })
    qc.setQueryData(queryKeys.plexLibraries, oldLibraries)
    const remove = vi.spyOn(qc, 'removeQueries')

    const { result } = renderHook(() => useUpdateSettings(), {
      wrapper: createWrapper(qc),
    })
    await result.current.mutateAsync({ plex_url: 'http://new-plex:32400' })

    await waitFor(() => expect(remove).toHaveBeenCalledWith({ queryKey: queryKeys.plexLibraries }))
    expect(qc.getQueryData(queryKeys.plexLibraries)).toBeUndefined()
    expect(qc.getQueryData(queryKeys.settings)).toEqual(saved)
  })

  it('invalidates the Plex library picker when the Plex connection is unchanged', async () => {
    const saved: SettingsResponse = { plex_url: 'http://plex:32400', qbittorrent_username: 'next' }
    ;(client.PUT as unknown as Mock).mockResolvedValue({ data: saved, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    qc.setQueryData(queryKeys.settings, { plex_url: 'http://plex:32400' })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useUpdateSettings(), {
      wrapper: createWrapper(qc),
    })
    await result.current.mutateAsync({
      plex_url: 'http://plex:32400',
      qbittorrent_username: 'next',
    })

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.plexLibraries }),
    )
    expect(qc.getQueryData(queryKeys.settings)).toEqual(saved)
  })

  it('invalidates Discover so it refetches after a TMDB api-key change (issue #14)', async () => {
    const saved: SettingsResponse = { tmdb_api_key: '***' }
    ;(client.PUT as unknown as Mock).mockResolvedValue({ data: saved, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useUpdateSettings(), {
      wrapper: createWrapper(qc),
    })
    await result.current.mutateAsync({ tmdb_api_key: 'sk-new-key' })

    // A prefix match on ['discover'] covers both queryKeys.discoverHome and
    // every queryKeys.discover(query, year) variant. Fails before the fix
    // (Discover data stays keyed to the old TMDB credentials).
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] }))
  })

  it('invalidates ops health so saved service cards never show the old server (Codex P2)', async () => {
    const saved: SettingsResponse = { prowlarr_url: 'http://new-prowlarr:9696' }
    ;(client.PUT as unknown as Mock).mockResolvedValue({ data: saved, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    qc.setQueryData(queryKeys.settings, { prowlarr_url: 'http://old-prowlarr:9696' })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useUpdateSettings(), {
      wrapper: createWrapper(qc),
    })
    await result.current.mutateAsync({ prowlarr_url: 'http://new-prowlarr:9696' })

    // Settings reads health with poll:false, so without this invalidation a
    // disconnected realtime stream leaves the cards claiming the OLD server's
    // Connected/Down status after the save. Fails before the fix.
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.opsHealth }),
    )
  })

  it('invalidates update status when update policy changes', async () => {
    const saved: SettingsResponse = { automatic_updates_enabled: true }
    ;(client.PUT as unknown as Mock).mockResolvedValue({ data: saved, response: { status: 200 } })
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useUpdateSettings(), { wrapper: createWrapper(qc) })
    await result.current.mutateAsync({ automatic_updates_enabled: true })

    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.updateStatus })
  })
})

function updateStatus(overrides: Partial<UpdateStatusResponse> = {}): UpdateStatusResponse {
  return {
    state: 'idle',
    updater_available: true,
    current_build: '1.4.0',
    channel: 'stable',
    ...overrides,
  }
}

describe('update hooks', () => {
  it('reads the status endpoint on the updater heartbeat polling cadence', async () => {
    vi.useFakeTimers()
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: updateStatus(),
      response: { status: 200 },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = renderHook(() => useUpdateStatus(), { wrapper: createWrapper(qc) })

    await act(async () => void (await vi.advanceTimersByTimeAsync(0)))
    expect(client.GET).toHaveBeenCalledWith('/api/v1/updates/status')
    await act(async () => void (await vi.advanceTimersByTimeAsync(UPDATE_STATUS_POLL_INTERVAL_MS)))
    expect(client.GET).toHaveBeenCalledTimes(2)

    unmount()
    qc.clear()
  })

  it('sends bodyless check and update requests and refreshes the shared status cache', async () => {
    const checking = updateStatus({ state: 'checking' })
    const queued = updateStatus({ state: 'waiting_for_window', available_build: '1.5.0' })
    ;(client.POST as unknown as Mock)
      .mockResolvedValueOnce({ data: checking, response: { status: 200 } })
      .mockResolvedValueOnce({ data: queued, response: { status: 200 } })
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const check = renderHook(() => useCheckForUpdate(), { wrapper: createWrapper(qc) })
    await check.result.current.mutateAsync()
    expect(client.POST).toHaveBeenNthCalledWith(1, '/api/v1/updates/check-now')
    expect(qc.getQueryData(queryKeys.updateStatus)).toEqual(checking)

    const update = renderHook(() => useUpdateWhenReady(), { wrapper: createWrapper(qc) })
    await update.result.current.mutateAsync()
    expect(client.POST).toHaveBeenNthCalledWith(2, '/api/v1/updates/update-when-ready')
    expect(qc.getQueryData(queryKeys.updateStatus)).toEqual(queued)
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.updateStatus })

    check.unmount()
    update.unmount()
    qc.clear()
  })
})

describe('useDiscoverHome', () => {
  it('keys and serializes the mount load id across invalidation refetches', async () => {
    const loadId = '00000000-0000-4000-8000-000000000191'
    const home = { spotlights: [], rows: [] }
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: home,
      response: { status: 200 },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const { result } = renderHook(() => useDiscoverHome({ loadId }), {
      wrapper: createWrapper(qc),
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(qc.getQueryData(queryKeys.discoverHome(loadId))).toEqual(home)
    expect(client.GET).toHaveBeenCalledWith('/api/v1/discover/home', {
      params: { query: { load_id: loadId } },
    })

    await act(async () => {
      await qc.invalidateQueries({ queryKey: ['discover'] })
    })
    await waitFor(() => expect(client.GET).toHaveBeenCalledTimes(2))
    expect(vi.mocked(client.GET).mock.calls).toEqual([
      ['/api/v1/discover/home', { params: { query: { load_id: loadId } } }],
      ['/api/v1/discover/home', { params: { query: { load_id: loadId } } }],
    ])
  })

  it('keeps direct/legacy callers on the standard home key without a load id', async () => {
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: { spotlights: [], rows: [] },
      response: { status: 200 },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const { result } = renderHook(() => useDiscoverHome(), {
      wrapper: createWrapper(qc),
    })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(client.GET).toHaveBeenCalledWith('/api/v1/discover/home', {
      params: { query: {} },
    })
    expect(qc.getQueryData(queryKeys.discoverHome())).toBeDefined()
  })
})

describe('useEvict', () => {
  it('invalidates disk/health/requests even when the sweep reports per-root errors', async () => {
    const partial: EvictResponse = {
      evicted: [
        {
          request_id: 1,
          media_type: 'movie',
          title: 'Old Movie',
          season: null,
          library_path: '/library/movies/Old Movie',
          freed_bytes: 1024,
        },
      ],
      errors: [{ root: 'tv_root', detail: 'sweep failed (PlexLibraryError)' }],
    }
    ;(client.POST as unknown as Mock).mockResolvedValue({ data: partial, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useEvict(), { wrapper: createWrapper(qc) })
    const outcome = await result.current.mutateAsync()

    expect(outcome.errors).toEqual([{ root: 'tv_root', detail: 'sweep failed (PlexLibraryError)' }])
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.opsDisk }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.opsHealth })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.requests })
  })
})

describe('useMarkFailed', () => {
  it('invalidates requests after rejecting a queued release', async () => {
    const item: QueueItem = {
      id: 7,
      media_request_id: 4,
      progress: 0,
      seed_ratio: 0,
      status: 'failed',
      tmdb_id: 603,
      torrent_hash: 'deadbeef',
    }
    ;(client.POST as unknown as Mock).mockResolvedValue({ data: item, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useMarkFailed(), {
      wrapper: createWrapper(qc),
    })
    await result.current.mutateAsync({ downloadId: 7, blocklist: true })

    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.requests }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.queue })
  })
})

describe('useWithdrawSubscription', () => {
  it('DELETEs the subscription path, returns the settled outcome, and invalidates requests, queue, and discover', async () => {
    ;(client.DELETE as unknown as Mock).mockResolvedValue({
      data: { settled: true },
      response: { status: 200 },
    })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useWithdrawSubscription(), {
      wrapper: createWrapper(qc),
    })
    const outcome = await result.current.mutateAsync(42)
    expect(outcome).toEqual({ settled: true })

    expect(client.DELETE).toHaveBeenCalledWith('/api/v1/requests/{request_id}/subscription', {
      params: { path: { request_id: 42 } },
    })
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.requests }))
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.queue })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['discover'] })
  })

  it('throws the normalized ApiError on failure without invalidating anything', async () => {
    ;(client.DELETE as unknown as Mock).mockResolvedValue({
      error: { detail: 'has_other_participants' },
      response: { status: 409 },
    })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useWithdrawSubscription(), {
      wrapper: createWrapper(qc),
    })
    await expect(result.current.mutateAsync(42)).rejects.toMatchObject({
      code: 'has_other_participants',
    })
    expect(invalidate).not.toHaveBeenCalled()
  })
})

describe('issue #370 phase 2 — compact live-state / by-title invalidation', () => {
  it('a request mutation invalidates the nested live-state and by-title queries by construction', async () => {
    // The highest-risk omission flagged in the design: every request-mutation
    // onSuccess must also invalidate the two new query keys, or a created/
    // cancelled/withdrawn request leaves a stale tile/modal. Rather than add a
    // second invalidateQueries call at every one of the ~9 call sites (easy to
    // forget on the NEXT mutation too), queryKeys.requestsLiveState/
    // requestsByTitle are nested UNDER the ['requests'] prefix every existing
    // mutation already invalidates — so this is a structural guarantee, not a
    // per-call-site convention. Proven here against a REAL QueryClient (default
    // exact:false prefix matching), not a mock.
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    qc.setQueryData(queryKeys.requestsLiveState(['movie:603']), { states: {} })
    qc.setQueryData(queryKeys.requestsByTitle('movie', 603), { requests: [], next_cursor: null })

    ;(client.POST as unknown as Mock).mockResolvedValue({
      data: { id: 1, tmdb_id: 603, media_type: 'movie', title: 'Fight Club', status: 'pending' },
      response: { status: 201 },
    })
    const { result } = renderHook(() => useCreateRequest(), { wrapper: createWrapper(qc) })
    await result.current.mutateAsync({ tmdb_id: 603, media_type: 'movie' })

    await waitFor(() =>
      expect(qc.getQueryState(queryKeys.requestsLiveState(['movie:603']))?.isInvalidated).toBe(
        true,
      ),
    )
    expect(qc.getQueryState(queryKeys.requestsByTitle('movie', 603))?.isInvalidated).toBe(true)
  })

  it('queryKeys.requestsLiveState/requestsByTitle nest under the requests prefix', () => {
    // Pins the key SHAPE the structural guarantee above depends on: any future
    // rename that moves these off the ['requests', ...] prefix would silently
    // break every mutation's invalidation without failing loudly elsewhere.
    expect(queryKeys.requestsLiveState(['movie:1', 'tv:2'])).toEqual([
      'requests',
      'live-state',
      'movie:1',
      'tv:2',
    ])
    expect(queryKeys.requestsByTitle('tv', 5)).toEqual(['requests', 'by-title', 'tv', 5])
  })
})

describe('useTileLiveStates', () => {
  it('POSTs the deduped, sorted key set and never fetches for an empty item list', async () => {
    ;(client.POST as unknown as Mock).mockResolvedValue({
      data: { states: { 'movie:1': { status: 'pending', request_id: 9, has_history: true, has_coexisting_available: false } } },
      response: { status: 200 },
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

    const { result, rerender } = renderHook<
      ReturnType<typeof useTileLiveStates>,
      { media_type: 'movie' | 'tv'; tmdb_id: number }[]
    >((items) => useTileLiveStates(items), { wrapper: createWrapper(qc), initialProps: [] })
    // No items: disabled, no POST at all.
    expect(client.POST).not.toHaveBeenCalled()
    expect(result.current.data).toBeUndefined()

    rerender([
      { media_type: 'tv', tmdb_id: 2 },
      { media_type: 'movie', tmdb_id: 1 },
      { media_type: 'movie', tmdb_id: 1 }, // duplicate — deduped
    ])
    await waitFor(() => expect(client.POST).toHaveBeenCalledTimes(1))
    expect(client.POST).toHaveBeenCalledWith('/api/v1/requests/live-state', {
      body: {
        keys: [
          { media_type: 'movie', tmdb_id: 1 },
          { media_type: 'tv', tmdb_id: 2 },
        ],
      },
    })
    await waitFor(() =>
      expect(result.current.data?.states['movie:1']?.request_id).toBe(9),
    )
  })
})

describe('live-state authority predicate (issue #397 Codex P2 rounds 1–2)', () => {
  // The ONE shared freshness predicate (`useLiveStateAuthority`) both
  // `useTitleRequests` and `useTileLiveStates` compute for their consumers:
  // data is authoritative only once a fetch has completed during the CURRENT
  // observation epoch (query key + enabled streak). Driven here through the
  // real hooks against a real QueryClient — the consumer suites only pin that
  // the modal/tiles HONOR the flag; this suite pins what the flag MEANS.
  function qcWithNoRetry() {
    return new QueryClient({ defaultOptions: { queries: { retry: false } } })
  }

  it('useTitleRequests: a stale cached answer is NOT authoritative until the epoch refetch lands (round 2)', async () => {
    const qc = qcWithNoRetry()
    // A previous visit cached "no rows", 60s ago — old enough to be outside
    // the staleTime freshness allowance. A request may have been created since.
    qc.setQueryData(
      queryKeys.requestsByTitle('movie', 42),
      { requests: [] },
      { updatedAt: Date.now() - 60_000 },
    )
    let resolveGet!: (value: unknown) => void
    ;(client.GET as unknown as Mock).mockReturnValue(
      new Promise((resolve) => {
        resolveGet = resolve
      }),
    )

    const { result } = renderHook(() => useTitleRequests('movie', 42), {
      wrapper: createWrapper(qc),
    })

    // React Query serves the cached answer immediately with isLoading FALSE —
    // exactly the shape the round-1 isLoading gate wrongly trusted…
    expect(result.current.data).toEqual({ requests: [] })
    expect(result.current.isLoading).toBe(false)
    // …but the predicate holds until the reopen's own fetch completes.
    expect(result.current.authoritative).toBe(false)

    resolveGet({
      data: { requests: [{ id: 1, tmdb_id: 42, media_type: 'movie', title: 'T', status: 'pending' }] },
      response: { status: 200 },
    })
    await waitFor(() => expect(result.current.authoritative).toBe(true))
  })

  it('useTitleRequests: re-enabling (modal reopen) restarts the epoch', async () => {
    const qc = qcWithNoRetry()
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: { requests: [] },
      response: { status: 200 },
    })
    const { result, rerender } = renderHook(
      ({ enabled }: { enabled: boolean }) => useTitleRequests('movie', 42, { enabled }),
      { wrapper: createWrapper(qc), initialProps: { enabled: true } },
    )
    await waitFor(() => expect(result.current.authoritative).toBe(true))

    // The modal closes (observer disabled): nothing is authoritative while
    // no one is looking.
    rerender({ enabled: false })
    expect(result.current.authoritative).toBe(false)

    // While closed, the cache ages past the freshness allowance and the next
    // fetch is held open — the reopen must NOT trust the old answer meanwhile.
    qc.setQueryData(
      queryKeys.requestsByTitle('movie', 42),
      { requests: [] },
      { updatedAt: Date.now() - 60_000 },
    )
    let resolveGet!: (value: unknown) => void
    ;(client.GET as unknown as Mock).mockReturnValue(
      new Promise((resolve) => {
        resolveGet = resolve
      }),
    )
    rerender({ enabled: true })
    expect(result.current.data).toEqual({ requests: [] })
    expect(result.current.authoritative).toBe(false)

    resolveGet({ data: { requests: [] }, response: { status: 200 } })
    await waitFor(() => expect(result.current.authoritative).toBe(true))
  })

  it('useTitleRequests: data still fresh at epoch start stays authoritative (no fetch is coming)', async () => {
    // The app's REAL staleTime (the other tests use the bare default of 0, so
    // everything is instantly stale there): React Query will not refetch
    // fresh data on subscribe, so the predicate must not wait for one — a
    // same-second close-and-reopen would otherwise wedge on 'checking' until
    // the next poll tick (up to the 45s realtime floor).
    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: QUERY_STALE_TIME_MS } },
    })
    qc.setQueryData(queryKeys.requestsByTitle('movie', 42), { requests: [] })
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: { requests: [] },
      response: { status: 200 },
    })
    const { result } = renderHook(() => useTitleRequests('movie', 42), {
      wrapper: createWrapper(qc),
    })
    expect(result.current.authoritative).toBe(true)
    // No refetch was started for it either — this authority is not transient.
    expect(client.GET).not.toHaveBeenCalled()
  })

  it('useTitleRequests: a FAILED epoch fetch releases the hold instead of wedging it shut', async () => {
    const qc = qcWithNoRetry()
    qc.setQueryData(
      queryKeys.requestsByTitle('movie', 42),
      { requests: [] },
      { updatedAt: Date.now() - 60_000 },
    )
    ;(client.GET as unknown as Mock).mockResolvedValue({
      error: { detail: 'boom' },
      response: { status: 500 },
    })
    const { result } = renderHook(() => useTitleRequests('movie', 42), {
      wrapper: createWrapper(qc),
    })
    expect(result.current.authoritative).toBe(false)
    // Backend unreachable: best-known cached state + poll retries beats a
    // permanently wedged action zone (honesty over silence, north star #3).
    await waitFor(() => expect(result.current.authoritative).toBe(true))
  })

  it('useTitleRequests: never authoritative with NO data at all, even after a failed fetch', async () => {
    const qc = qcWithNoRetry()
    ;(client.GET as unknown as Mock).mockResolvedValue({
      error: { detail: 'boom' },
      response: { status: 500 },
    })
    const { result } = renderHook(() => useTitleRequests('movie', 42), {
      wrapper: createWrapper(qc),
    })
    await waitFor(() => expect(result.current.isError).toBe(true))
    // Zero knowledge fails CLOSED — no affordance may be offered on it.
    expect(result.current.authoritative).toBe(false)
  })

  it('useTitleRequests: an invalidated key is not authoritative while its superseding refetch is in flight', async () => {
    const qc = qcWithNoRetry()
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: { requests: [] },
      response: { status: 200 },
    })
    const { result } = renderHook(() => useTitleRequests('movie', 42), {
      wrapper: createWrapper(qc),
    })
    await waitFor(() => expect(result.current.authoritative).toBe(true))

    // A mutation/realtime event tells us this answer is superseded; the
    // replacement fetch (started by invalidateQueries in the same notify
    // batch) is held open — the epoch's earlier completed fetch must NOT
    // keep the stale answer trusted through this window.
    let resolveGet!: (value: unknown) => void
    ;(client.GET as unknown as Mock).mockReturnValue(
      new Promise((resolve) => {
        resolveGet = resolve
      }),
    )
    await act(async () => {
      void qc.invalidateQueries({ queryKey: queryKeys.requestsByTitle('movie', 42) })
      await Promise.resolve()
    })
    expect(result.current.authoritative).toBe(false)

    resolveGet({ data: { requests: [] }, response: { status: 200 } })
    await waitFor(() => expect(result.current.authoritative).toBe(true))
  })

  it('useTileLiveStates: a stale cached compact result is NOT authoritative while its epoch refetch is in flight (round 2)', async () => {
    const qc = qcWithNoRetry()
    // Search cached an EMPTY result for this tile set; SSE was down when
    // another client created a request, so nothing ever invalidated this
    // nested key (Layout's safety-net poll refetches only ['requests'] itself).
    qc.setQueryData(
      queryKeys.requestsLiveState(['movie:1']),
      { states: {} },
      { updatedAt: Date.now() - 60_000 },
    )
    let resolvePost!: (value: unknown) => void
    ;(client.POST as unknown as Mock).mockReturnValue(
      new Promise((resolve) => {
        resolvePost = resolve
      }),
    )

    const { result } = renderHook(
      () => useTileLiveStates([{ media_type: 'movie', tmdb_id: 1 }]),
      { wrapper: createWrapper(qc) },
    )

    // Cached success data is served while the background POST runs — the old
    // `isSuccess && !invalidated` fold trusted this window.
    expect(result.current.data).toEqual({ states: {} })
    expect(result.current.authoritative).toBe(false)

    resolvePost({
      data: {
        states: {
          'movie:1': {
            status: 'pending',
            request_id: 9,
            has_history: true,
            has_coexisting_available: false,
          },
        },
      },
      response: { status: 200 },
    })
    await waitFor(() => expect(result.current.authoritative).toBe(true))
    expect(result.current.data?.states['movie:1']?.has_history).toBe(true)
  })

  it('useTileLiveStates: never authoritative while disabled (no visible tiles)', () => {
    const qc = qcWithNoRetry()
    const { result } = renderHook(() => useTileLiveStates([]), {
      wrapper: createWrapper(qc),
    })
    expect(result.current.authoritative).toBe(false)
    expect(client.POST).not.toHaveBeenCalled()
  })
})

describe('useRelocateDownload', () => {
  it('invalidates the queue but NOT requests (nothing about the owning request changes yet)', async () => {
    const item: QueueItem = {
      id: 9,
      media_request_id: 4,
      progress: 0,
      seed_ratio: 0,
      status: 'import_blocked',
      failed_reason: 'download path not visible inside the container /downloads/movie',
      tmdb_id: 603,
      torrent_hash: 'deadbeef',
    }
    ;(client.POST as unknown as Mock).mockResolvedValue({ data: item, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useRelocateDownload(), {
      wrapper: createWrapper(qc),
    })
    await result.current.mutateAsync(9)

    expect(client.POST).toHaveBeenCalledWith('/api/v1/queue/{download_id}/relocate', {
      params: { path: { download_id: 9 } },
    })
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.queue }))
    expect(invalidate).not.toHaveBeenCalledWith({ queryKey: queryKeys.requests })
  })
})

describe('useRequestsInvalidated', () => {
  it('tracks the /requests invalidated flag reactively (Codex P2 quick-request gate)', async () => {
    // The flag lives on the query CACHE state, not the useQuery result, and only
    // invalidateQueries sets it — the fix's whole premise. Prove the bridge hook
    // sees it flip both ways against a real QueryClient.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    qc.setQueryData(queryKeys.requests, { requests: [] })

    const { result } = renderHook(() => useRequestsInvalidated(), { wrapper: createWrapper(qc) })
    // A settled, non-invalidated query reads false.
    expect(result.current).toBe(false)

    // invalidateQueries marks it invalidated (refetchType 'none' keeps the flag set
    // deterministically instead of racing an immediate refetch).
    await act(async () => {
      await qc.invalidateQueries({ queryKey: queryKeys.requests, refetchType: 'none' })
    })
    expect(result.current).toBe(true)

    // A settling fetch clears the flag (setQueryData dispatches a success that resets
    // isInvalidated) — the gate reopens once the refetch lands.
    act(() => {
      qc.setQueryData(queryKeys.requests, { requests: [] })
    })
    await waitFor(() => expect(result.current).toBe(false))
  })
})

describe('useExchangeApiKey', () => {
  it('sends the key in the X-Api-Key header and seeds the /auth/me answer', async () => {
    const me: AuthMeResponse = { authenticated: true, auth_method: 'api_key', is_admin: true }
    ;(client.POST as unknown as Mock).mockResolvedValue({ data: me, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const { result } = renderHook(() => useExchangeApiKey(), { wrapper: createWrapper(qc) })
    const outcome = await result.current.mutateAsync('recovery-key')

    // The raw key rides a single request header — never a stored value (CodeQL #263).
    expect(client.POST).toHaveBeenCalledWith('/api/v1/auth/api-key', {
      headers: { 'X-Api-Key': 'recovery-key' },
    })
    expect(outcome.auth_method).toBe('api_key')
    // The gate re-renders authenticated at once from the seeded answer.
    expect(qc.getQueryData(queryKeys.authMe)).toEqual(me)
  })
})

describe('useRotateAppKey', () => {
  it('rotates and invalidates the status without touching the browser credential', async () => {
    const rotated: AppApiKeyResponse = { app_api_key: 'brand-new-key' }
    ;(client.POST as unknown as Mock).mockResolvedValue({
      data: rotated,
      response: { status: 200 },
    })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useRotateAppKey(), { wrapper: createWrapper(qc) })
    const outcome = await result.current.mutateAsync()

    expect(client.POST).toHaveBeenCalledWith('/api/v1/settings/app-key/rotate')
    // The plaintext is returned once for the operator to copy elsewhere.
    expect(outcome.app_api_key).toBe('brand-new-key')
    // Minting flips the Access card from Generate to Rotate/Revoke: the status
    // query must be invalidated so the card reflects that a key now exists.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.appKeyStatus })
    // The browser authenticates by the session cookie, not the raw key: rotating
    // the shared key never disturbs this session (no own-credential to update).
  })
})

describe('useRevokeAppKey', () => {
  it('deletes the key and invalidates the status', async () => {
    ;(client.DELETE as unknown as Mock).mockResolvedValue({
      data: undefined,
      response: { status: 204 },
    })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useRevokeAppKey(), { wrapper: createWrapper(qc) })
    await result.current.mutateAsync()

    expect(client.DELETE).toHaveBeenCalledWith('/api/v1/settings/app-key')
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.appKeyStatus })
  })
})

describe('realtime polling fallback', () => {
  it('keeps an explicitly disabled queue query completely idle', async () => {
    vi.useFakeTimers()
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: { queue: [] },
      response: { status: 200 },
    })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = renderHook(() => useQueue({ enabled: false, poll: true }), {
      wrapper: createWrapper(qc),
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(QUEUE_REALTIME_FLOOR_MS + POLL_INTERVAL_MS)
    })
    expect(client.GET).not.toHaveBeenCalled()
    unmount()
    qc.clear()
  })

  it('drops queue polling to its slow floor while realtime is connected', async () => {
    vi.useFakeTimers()
    setRealtimeConnected(true)
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: { queue: [] },
      response: { status: 200 },
    })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = renderHook(() => useQueue({ poll: true }), {
      wrapper: createWrapper(qc),
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(client.GET).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_INTERVAL_MS)
    })
    expect(client.GET).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(QUEUE_REALTIME_FLOOR_MS)
    })
    expect(client.GET).toHaveBeenCalledTimes(2)
    unmount()
    qc.clear()
  })

  it('keeps request polling while realtime is disconnected', async () => {
    vi.useFakeTimers()
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: { requests: [] },
      response: { status: 200 },
    })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = renderHook(() => useRequests({ poll: true }), {
      wrapper: createWrapper(qc),
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(client.GET).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(REQUESTS_POLL_INTERVAL_MS)
    })
    expect(client.GET).toHaveBeenCalledTimes(2)
    unmount()
    qc.clear()
  })

  it('drops to a slow polling floor (never off) while realtime is connected', async () => {
    vi.useFakeTimers()
    setRealtimeConnected(true)
    ;(client.GET as unknown as Mock).mockResolvedValue({
      data: { requests: [] },
      response: { status: 200 },
    })

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = renderHook(() => useRequests({ poll: true }), {
      wrapper: createWrapper(qc),
    })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(client.GET).toHaveBeenCalledTimes(1)

    // Fast cadence is suppressed while connected...
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REQUESTS_POLL_INTERVAL_MS)
    })
    expect(client.GET).toHaveBeenCalledTimes(1)

    // ...but the slow floor still fires — a zombie stream self-heals within one
    // slow tick regardless of the watchdog.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(REQUESTS_REALTIME_FLOOR_MS)
    })
    expect(client.GET).toHaveBeenCalledTimes(2)
    unmount()
    qc.clear()
  })
})
