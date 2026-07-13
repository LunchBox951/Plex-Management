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
 * operator-facing error toast, and the stale-run guard. Callers choose only the
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

  // Async-staleness guard: a run may only land while its epoch is still the
  // CURRENT one. The epoch advances on every title change, on every new run
  // (of two concurrent runs only the newest lands), and on every manual clear.
  // A bare title-KEY comparison is not enough: navigating A -> B -> back to A
  // restores the key, so a response started on the FIRST A visit would pass a
  // key guard and resurface as if it were fresh.
  const runEpoch = useRef(0)
  useEffect(() => {
    runEpoch.current += 1
  }, [titleKey])

  // CLEAR (not merely mask) the stored result whenever the title changes. A
  // long-mounted modal can move A -> B -> back to A; a masked-but-retained A
  // result would resurface then, even though the request/blocklist context that
  // produced it may have changed. Adjusting state during render makes the old
  // result invisible in this very render (no stale-frame flash) AND drops it
  // for good. Together with the keyed derivation below this also covers the
  // epoch bump's render-to-effect gap: a stale response landing in that window
  // is stored under the OLD title's key, so it is masked in the same render
  // and wiped by this clear on the next title change — it can never display.
  const [clearedForTitleKey, setClearedForTitleKey] = useState(titleKey)
  if (clearedForTitleKey !== titleKey) {
    setClearedForTitleKey(titleKey)
    setPreviewState(null)
  }

  const preview = previewState?.titleKey === titleKey ? previewState.value : null

  // A manual clear also invalidates any in-flight run: the modal clears on
  // season changes, and a still-resolving preview for the OLD season landing
  // after the clear would show releases the grab path would then send under the
  // NEW season's context (404 release_not_found, or worse).
  const clearPreview = useCallback(() => {
    runEpoch.current += 1
    setPreviewState(null)
  }, [])

  const runPreview = useCallback(
    async (requestId: number | null, seasonOverride?: number | null) => {
      if (!title) return
      const startedKey = `${title.media_type}:${title.tmdb_id}`
      const startedEpoch = ++runEpoch.current
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
        if (runEpoch.current !== startedEpoch) return
        setPreviewState({ titleKey: startedKey, value: result })
      } catch (error) {
        if (runEpoch.current !== startedEpoch) return
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
