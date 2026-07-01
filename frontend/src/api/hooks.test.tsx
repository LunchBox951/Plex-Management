/** Regression test for F2: saving Settings must refetch the Plex library picker. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useMarkFailed, useUpdateSettings } from './hooks'
import { client } from './client'
import { queryKeys } from '../lib/queryClient'
import type { PlexLibraryOption, QueueItem, SettingsResponse } from './types'

// No network: the typed client is replaced with controllable mutation mocks.
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
      { path: '/old-plex/movies', section_key: '1', title: 'Movies', writable: true },
    ]
    qc.setQueryData(queryKeys.settings, { plex_url: 'http://old-plex:32400' })
    qc.setQueryData(queryKeys.plexLibraries, oldLibraries)
    const remove = vi.spyOn(qc, 'removeQueries')

    const { result } = renderHook(() => useUpdateSettings(), {
      wrapper: createWrapper(qc),
    })
    await result.current.mutateAsync({ plex_url: 'http://new-plex:32400' })

    // The movies_root picker cache is removed before the form can re-enable it, so
    // folders from the old Plex server are not visible while the new query refetches.
    await waitFor(() => expect(remove).toHaveBeenCalledWith({ queryKey: queryKeys.plexLibraries }))
    expect(qc.getQueryData(queryKeys.plexLibraries)).toBeUndefined()
    // The PUT body is written straight into the settings cache (no extra GET).
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

    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.requests }),
    )
    expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.queue })
  })
})
