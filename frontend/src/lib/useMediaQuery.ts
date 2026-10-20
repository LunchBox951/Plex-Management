import { useSyncExternalStore } from 'react'

/** The phone breakpoint: below Tailwind's `sm` (640px). */
export const PHONE_QUERY = '(max-width: 639.98px)'

function getMatcher(query: string): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  return window.matchMedia(query)
}

/**
 * Live `matchMedia` result. Without `matchMedia` (jsdom, very old browsers) it
 * reports `false`, so layouts fall back to their desktop form.
 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const matcher = getMatcher(query)
      if (!matcher) return () => undefined
      matcher.addEventListener('change', onChange)
      return () => matcher.removeEventListener('change', onChange)
    },
    () => getMatcher(query)?.matches ?? false,
    () => false,
  )
}

/** True on phone-width viewports: sheets slide up, menus become action sheets. */
export function useIsPhone(): boolean {
  return useMediaQuery(PHONE_QUERY)
}
