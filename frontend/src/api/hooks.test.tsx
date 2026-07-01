/** Regression tests for mutation cache invalidation behavior. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useEvict, useMarkFailed, useUpdateSettings } from './hooks'
import { client } from './client'
import { queryKeys } from '../lib/queryClient'
import type { EvictResponse, PlexLibraryOption, QueueItem, SettingsResponse } from './types'

vi.mock('./client', () => ({
  client: { POST: vi.fn(), PUT: vi.fn() },
}))

function createWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

beforeEach(() => {
  vi.mocked(client.POST).mockReset()
  vi.mocked(client.PUT).mockReset()
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
