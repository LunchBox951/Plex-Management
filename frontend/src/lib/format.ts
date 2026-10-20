/** Small, dependency-free display formatters shared by the Status page's disk
 * gauges (and anywhere else a byte count / instant needs a human string). */

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'] as const

/** `1536` -> `"1.5 KB"`. Negative/NaN inputs (shouldn't happen, but honesty
 * over a crash) render as `"0 B"` rather than a nonsense unit. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const exponent = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), UNITS.length - 1)
  const value = bytes / 1024 ** exponent
  const precision = exponent === 0 ? 0 : 1
  return `${value.toFixed(precision)} ${UNITS[exponent]}`
}

/** An ISO timestamp -> the browser's local datetime string, or an honest
 * placeholder for the "never happened yet" case (`null`/`undefined`). */
export function formatTimestamp(value: string | null | undefined): string {
  if (!value) return 'never'
  return new Date(value).toLocaleString()
}

/** An ISO timestamp -> a short relative age: `"now"`, `"5m"`, `"3h"`, `"2d"`, then
 * a short date. `now` is injectable for tests. */
export function formatRelative(value: string, now: number = Date.now()): string {
  const then = new Date(value).getTime()
  if (!Number.isFinite(then)) return ''
  const minutes = Math.floor((now - then) / 60_000)
  if (minutes < 1) return 'now'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d`
  return new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}
