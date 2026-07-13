import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DiscoverResult, SearchPreviewResponse } from '../api/types'
import { useTitleReleasePreview } from './useTitleReleasePreview'

const mocks = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  toast: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useSearchPreview: () => ({ mutateAsync: mocks.mutateAsync, isPending: false }),
}))

vi.mock('./ui/toast', () => ({
  useToast: () => ({ toast: mocks.toast }),
}))

const MOVIE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 42,
  title: 'Test Movie',
  year: 2021,
  library_state: 'none',
}

const PREVIEW: SearchPreviewResponse = {
  accepted: [],
  rejected: [],
  no_acceptable_release: true,
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.mutateAsync.mockResolvedValue(PREVIEW)
})

describe('useTitleReleasePreview', () => {
  it('builds an explicit movie preview body and owns result clearing', async () => {
    const hook = renderHook(() => useTitleReleasePreview(MOVIE, null))

    await act(async () => hook.result.current.runPreview(null))
    expect(mocks.mutateAsync).toHaveBeenCalledWith({
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      year: 2021,
    })
    expect(hook.result.current.preview).toEqual(PREVIEW)

    act(() => hook.result.current.clearPreview())
    expect(hook.result.current.preview).toBeNull()
  })

  it('threads the current or overridden TV season through request-id previews', async () => {
    const tv: DiscoverResult = {
      media_type: 'tv',
      tmdb_id: 100,
      title: 'Test Show',
      year: 2022,
      library_state: 'processing',
    }
    const hook = renderHook(() => useTitleReleasePreview(tv, 2))

    await act(async () => hook.result.current.runPreview(12))
    expect(mocks.mutateAsync).toHaveBeenLastCalledWith({ request_id: 12, season: 2 })

    await act(async () => hook.result.current.runPreview(12, 3))
    expect(mocks.mutateAsync).toHaveBeenLastCalledWith({ request_id: 12, season: 3 })
  })

  it('clears (not merely masks) the result when the title changes', async () => {
    const other: DiscoverResult = {
      ...MOVIE,
      tmdb_id: 43,
      title: 'Other Movie',
    }
    const hook = renderHook(
      ({ title }: { title: DiscoverResult }) => useTitleReleasePreview(title, null),
      { initialProps: { title: MOVIE } },
    )

    await act(async () => hook.result.current.runPreview(7))
    expect(hook.result.current.preview).toEqual(PREVIEW)

    // A long-mounted modal moving A -> B -> back to A must NOT resurface A's old
    // result: the request/blocklist context that produced it may have changed.
    hook.rerender({ title: other })
    expect(hook.result.current.preview).toBeNull()
    hook.rerender({ title: MOVIE })
    expect(hook.result.current.preview).toBeNull()
  })

  it('drops a run started on a PREVIOUS visit to the same title', async () => {
    // The guard must be an epoch, not a title-key comparison: navigating
    // A -> B -> back to A restores the key, so a response from A's FIRST visit
    // would pass a key guard and resurface as if it were fresh — after the
    // render-time clear had already wiped it.
    let resolvePreview: ((value: SearchPreviewResponse) => void) | undefined
    mocks.mutateAsync.mockReturnValue(
      new Promise<SearchPreviewResponse>((resolve) => {
        resolvePreview = resolve
      }),
    )
    const other: DiscoverResult = {
      ...MOVIE,
      tmdb_id: 43,
      title: 'Other Movie',
    }
    const hook = renderHook(
      ({ title }: { title: DiscoverResult }) => useTitleReleasePreview(title, null),
      { initialProps: { title: MOVIE } },
    )

    let pending: Promise<void> | undefined
    act(() => {
      pending = hook.result.current.runPreview(7)
    })
    hook.rerender({ title: other })
    hook.rerender({ title: MOVIE })
    await act(async () => {
      resolvePreview?.(PREVIEW)
      await pending
    })

    expect(hook.result.current.preview).toBeNull()
  })

  it('drops an in-flight run when the preview is manually cleared', async () => {
    // The modal clears the preview on season changes; a still-resolving run for
    // the OLD season landing afterwards would show releases the grab path would
    // then send under the NEW season's context.
    let resolvePreview: ((value: SearchPreviewResponse) => void) | undefined
    mocks.mutateAsync.mockReturnValue(
      new Promise<SearchPreviewResponse>((resolve) => {
        resolvePreview = resolve
      }),
    )
    const hook = renderHook(() => useTitleReleasePreview(MOVIE, null))

    let pending: Promise<void> | undefined
    act(() => {
      pending = hook.result.current.runPreview(7)
    })
    act(() => hook.result.current.clearPreview())
    await act(async () => {
      resolvePreview?.(PREVIEW)
      await pending
    })

    expect(hook.result.current.preview).toBeNull()
  })

  it('drops a result that resolves after the modal moves to another title', async () => {
    let resolvePreview: ((value: SearchPreviewResponse) => void) | undefined
    mocks.mutateAsync.mockReturnValue(
      new Promise<SearchPreviewResponse>((resolve) => {
        resolvePreview = resolve
      }),
    )
    const other: DiscoverResult = {
      ...MOVIE,
      tmdb_id: 43,
      title: 'Other Movie',
    }
    const hook = renderHook(
      ({ title }: { title: DiscoverResult }) => useTitleReleasePreview(title, null),
      { initialProps: { title: MOVIE } },
    )

    let pending: Promise<void> | undefined
    act(() => {
      pending = hook.result.current.runPreview(7)
    })
    hook.rerender({ title: other })
    await act(async () => {
      resolvePreview?.(PREVIEW)
      await pending
    })

    expect(hook.result.current.preview).toBeNull()
  })

  it('shows the shared error toast for the current title', async () => {
    mocks.mutateAsync.mockRejectedValue({ message: 'Indexer unavailable' })
    const hook = renderHook(() => useTitleReleasePreview(MOVIE, null))

    await act(async () => hook.result.current.runPreview(7))
    expect(mocks.toast).toHaveBeenCalledWith({
      title: 'Search failed',
      description: 'Indexer unavailable',
      intent: 'error',
    })
    expect(hook.result.current.preview).toBeNull()
  })
})
