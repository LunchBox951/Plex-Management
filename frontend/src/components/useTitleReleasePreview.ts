import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchPreview } from '../api/hooks'
import type {
  DiscoverResult,
  SearchPreviewRequest,
  SearchPreviewResponse,
} from '../api/types'
import type { ApiError } from '../lib/errors'
import { useToast } from './ui/toast'

function asApiError(error: unknown): ApiError {
  return error as ApiError
}

/**
 * The single release-preview path used by the title modal and its entry actions.
 *
 * It owns request-body construction, TV season scoping, pending/result state, the
 * operator-facing error toast, and the stale-title guard. Callers choose only the
 * stored request id (or ``null`` for an unrequested title) plus the rare explicit
 * season override needed during the create-then-render gap.
 */
export function useTitleReleasePreview(
  title: DiscoverResult | null,
  currentSeason: number | null,
) {
  const { toast } = useToast()
  const searchPreview = useSearchPreview()
  const [previewState, setPreviewState] = useState<{
    titleKey: string
    value: SearchPreviewResponse
  } | null>(null)

  const titleKey = title ? `${title.media_type}:${title.tmdb_id}` : null
  const latestTitleKey = useRef(titleKey)
  useEffect(() => {
    latestTitleKey.current = titleKey
  }, [titleKey])

  // CLEAR (not merely mask) the stored result whenever the title changes. A
  // long-mounted modal can move A -> B -> back to A; a masked-but-retained A
  // result would resurface then, even though the request/blocklist context that
  // produced it may have changed. Adjusting state during render makes the old
  // result invisible in this very render (no stale-frame flash) AND drops it for
  // good; the keyed derivation below stays as the same-render guard for results
  // the async path stored before this render committed.
  const [clearedForTitleKey, setClearedForTitleKey] = useState(titleKey)
  if (clearedForTitleKey !== titleKey) {
    setClearedForTitleKey(titleKey)
    setPreviewState(null)
  }

  const preview = previewState?.titleKey === titleKey ? previewState.value : null

  const clearPreview = useCallback(() => setPreviewState(null), [])

  const runPreview = useCallback(
    async (requestId: number | null, seasonOverride?: number | null) => {
      if (!title) return
      const startedKey = `${title.media_type}:${title.tmdb_id}`
      const body: SearchPreviewRequest =
        requestId !== null
          ? { request_id: requestId }
          : { tmdb_id: title.tmdb_id, media_type: title.media_type, title: title.title }

      if (requestId === null && typeof title.year === 'number') {
        body.year = title.year
      }

      const season = seasonOverride !== undefined ? seasonOverride : currentSeason
      if (title.media_type === 'tv' && season != null) {
        body.season = season
      }

      try {
        const result = await searchPreview.mutateAsync(body)
        if (latestTitleKey.current !== startedKey) return
        setPreviewState({ titleKey: startedKey, value: result })
      } catch (error) {
        if (latestTitleKey.current !== startedKey) return
        toast({
          title: 'Search failed',
          description: asApiError(error).message,
          intent: 'error',
        })
      }
    },
    [currentSeason, searchPreview, title, toast],
  )

  return {
    preview,
    isPending: searchPreview.isPending,
    clearPreview,
    runPreview,
  }
}
