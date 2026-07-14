import { useState } from 'react'

/**
 * Walk an ordered list of image sources, advancing to the next on load failure.
 *
 * The artwork fallback chain (issue #66): a poster/backdrop tries Plex-native art
 * first, then TMDB art, then nothing (the caller renders its gradient placeholder
 * when `src` is `null`). The `<img>`'s `onError` advances past a source that fails
 * to load — a 404 from the Plex proxy for a title with no native art, a dead TMDB
 * URL — so the tile always lands on the first source that actually renders, never
 * a broken image.
 *
 * Nullish/empty entries are dropped so callers can pass `plexUrl ?? null` inline.
 * The failure cursor resets whenever the source list itself changes (a re-used
 * card slot showing a different title), using React's supported adjust-state-
 * during-render pattern rather than an effect.
 */
export function useImageFallback(sources: readonly (string | null | undefined)[]): {
  src: string | null
  onError: () => void
} {
  const list = sources.filter((s): s is string => typeof s === 'string' && s.length > 0)
  const signature = list.join('\n')
  const [failedCount, setFailedCount] = useState(0)
  const [previousSignature, setPreviousSignature] = useState(signature)

  if (previousSignature !== signature) {
    setPreviousSignature(signature)
    setFailedCount(0)
  }

  const src = failedCount < list.length ? list[failedCount]! : null
  return {
    src,
    onError: () => setFailedCount((count) => count + 1),
  }
}
