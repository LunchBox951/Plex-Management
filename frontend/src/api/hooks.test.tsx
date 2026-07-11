/** Regression tests for mutation cache invalidation behavior. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useEvict,
  useMarkFailed,
  useQueue,
  useRelocateDownload,
  useRequests,
  useRequestsInvalidated,
  useRevokeAppKey,
  useRotateAppKey,
  useUpdateSettings,
} from './hooks'
import { client } from './client'
import {
  POLL_INTERVAL_MS,
  QUEUE_REALTIME_FLOOR_MS,
  REQUESTS_POLL_INTERVAL_MS,
  REQUESTS_REALTIME_FLOOR_MS,
  queryKeys,
} from '../lib/queryClient'
import * as apiKeyLib from '../lib/apiKey'
import { setRealtimeConnected } from '../lib/realtimeState'
import type {
  AppApiKeyResponse,
  EvictResponse,
  PlexLibraryOption,
  QueueItem,
  SettingsResponse,
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
  apiKeyLib.clearApiKey()
  setRealtimeConnected(false)
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  apiKeyLib.clearApiKey()
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

describe('useRotateAppKey', () => {
  it('persists the rotated key immediately so the current session survives (issue #28)', async () => {
    const rotated: AppApiKeyResponse = { app_api_key: 'brand-new-key' }
    ;(client.POST as unknown as Mock).mockResolvedValue({
      data: rotated,
      response: { status: 200 },
    })
    const finishRotation = vi.fn(() => {
      // The replacement must already be visible when the old-key barrier opens.
      expect(apiKeyLib.getApiKey()).toBe('brand-new-key')
    })
    const beginRotationSpy = vi
      .spyOn(apiKeyLib, 'beginApiKeyRotation')
      .mockReturnValue(finishRotation)
    const setApiKeySpy = vi.spyOn(apiKeyLib, 'setApiKey')

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useRotateAppKey(), { wrapper: createWrapper(qc) })
    const outcome = await result.current.mutateAsync()

    expect(outcome.app_api_key).toBe('brand-new-key')
    expect(beginRotationSpy).toHaveBeenCalledTimes(1)
    // Fails before the fix: a rotated key never written into THIS browser's
    // own store would 401 the very next request from the device that just
    // rotated it -- every other device is correctly locked out, but this one
    // must not be.
    expect(setApiKeySpy).toHaveBeenCalledWith('brand-new-key')
    expect(finishRotation).toHaveBeenCalledTimes(1)
    // Minting flips the Access card from Generate to Rotate/Revoke: the status
    // query must be invalidated so the card reflects that a key now exists.
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.appKeyStatus })
  })
})

describe('useRevokeAppKey', () => {
  it('deletes the key and invalidates the status without persisting anything locally', async () => {
    ;(client.DELETE as unknown as Mock).mockResolvedValue({
      data: undefined,
      response: { status: 204 },
    })
    const setApiKeySpy = vi.spyOn(apiKeyLib, 'setApiKey')
    setApiKeySpy.mockClear()

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidateSpy = vi.spyOn(qc, 'invalidateQueries')
    const { result } = renderHook(() => useRevokeAppKey(), { wrapper: createWrapper(qc) })
    await result.current.mutateAsync()

    expect(client.DELETE).toHaveBeenCalledWith('/api/v1/settings/app-key')
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: queryKeys.appKeyStatus })
    // Revoke clears the SHARED server-side key; it must never write a value into
    // this browser's own key store.
    expect(setApiKeySpy).not.toHaveBeenCalled()
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
