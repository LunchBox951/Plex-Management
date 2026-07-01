/** Regression test for F2: saving Settings must refetch the Plex library picker. */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { describe, expect, it, vi, type Mock } from 'vitest'
import { useEvict, useUpdateSettings } from './hooks'
import { client } from './client'
import { queryKeys } from '../lib/queryClient'
import type { EvictResponse, SettingsResponse } from './types'

// No network: the typed client is replaced with controllable PUT/POST mocks.
vi.mock('./client', () => ({
  client: { PUT: vi.fn(), POST: vi.fn() },
}))

function createWrapper(qc: QueryClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  }
}

describe('useUpdateSettings', () => {
  it('invalidates the Plex library picker so it refetches against the just-saved connection', async () => {
    const saved: SettingsResponse = { plex_url: 'http://new-plex:32400' }
    ;(client.PUT as unknown as Mock).mockResolvedValue({ data: saved, response: { status: 200 } })

    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    const invalidate = vi.spyOn(qc, 'invalidateQueries')

    const { result } = renderHook(() => useUpdateSettings(), {
      wrapper: createWrapper(qc),
    })
    await result.current.mutateAsync({ plex_url: 'http://new-plex:32400' })

    // The movies_root picker (queryKeys.plexLibraries) is refetched so a stale
    // pick-list — or a prior 409/401 frozen by retry:false — cannot survive a
    // connection change. Fails before the fix (key never invalidated); passes after.
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: queryKeys.plexLibraries }),
    )
    // The PUT body is written straight into the settings cache (no extra GET).
    expect(qc.getQueryData(queryKeys.settings)).toEqual(saved)
  })
})

describe('useEvict', () => {
  it('invalidates disk/health/requests even when the sweep reports per-root errors', async () => {
    // R6-C: a partial sweep (one root evicted, a LATER root raised) is still a
    // 200 -- a genuinely successful mutation, never a thrown error -- so the
    // disk/health/requests views must refresh to reflect whatever the sweep
    // actually accomplished, not be left stale just because `errors` is
    // non-empty.
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
