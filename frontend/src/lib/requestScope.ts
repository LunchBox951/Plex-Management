import type { RequestResponse } from '../api/types'

/** "1, 2, 5" → "1–2, 5": compact runs of consecutive numbers. */
export function compactRuns(numbers: readonly number[]): string {
  const sorted = [...new Set(numbers)].sort((a, b) => a - b)
  const parts: string[] = []
  let start: number | undefined
  let previous: number | undefined
  for (const value of [...sorted, Number.NaN]) {
    if (start !== undefined && previous !== undefined && value === previous + 1) {
      previous = value
      continue
    }
    if (start !== undefined && previous !== undefined) {
      parts.push(start === previous ? String(start) : `${start}–${previous}`)
    }
    start = value
    previous = value
  }
  return parts.join(', ')
}

/**
 * What was asked for, in words, for a request row: "Movie · 2023",
 * "TV · Every season", "TV · Seasons 1–2", "TV · S51 E4–6".
 */
export function describeRequestScope(request: RequestResponse): string {
  if (request.media_type !== 'tv') {
    return request.year != null ? `Movie · ${request.year}` : 'Movie'
  }
  const episodes = request.requested_episodes
  if (request.tv_request_mode === 'explicit_episodes' && episodes) {
    const seasons = Object.keys(episodes)
      .map(Number)
      .sort((a, b) => a - b)
    const described = seasons.map((season) => `S${season} E${compactRuns(episodes[season] ?? [])}`)
    return `TV · ${described.join('; ')}`
  }
  if (request.tv_request_mode === 'explicit_seasons' && request.requested_seasons?.length) {
    const seasons = request.requested_seasons
    return `TV · ${seasons.length === 1 ? 'Season' : 'Seasons'} ${compactRuns(seasons)}`
  }
  return 'TV · Every season'
}
