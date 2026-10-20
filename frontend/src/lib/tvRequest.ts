import type { CreateRequestBody, RequestResponse, TvEpisodeItem } from '../api/types'

/**
 * Pure helpers behind the TV request sheet (`TvRequestPicker`): the five
 * choices, what each one sends, and the words the footer and button use to
 * restate it. No React, no I/O, so the rules are unit-testable on their own.
 */

/** The five ways to ask for part of a show, in the order the sheet lists them. */
export type TvChoice = 'latest_season' | 'new_episodes' | 'episodes' | 'seasons' | 'every'

export const TV_CHOICES: readonly { value: TvChoice; label: string }[] = [
  { value: 'latest_season', label: 'Latest season' },
  { value: 'new_episodes', label: 'New episodes as they air' },
  { value: 'episodes', label: 'Choose episodes' },
  { value: 'seasons', label: 'Choose seasons' },
  { value: 'every', label: 'Every season' },
]

/** One picked episode. `unaired` is captured at pick time for the footer's "grabbed after it airs". */
export interface PickedEpisode {
  season: number
  episode: number
  unaired: boolean
}

/** Picked episodes keyed by {@link episodeKey}. */
export type EpisodePicks = Readonly<Record<string, PickedEpisode>>

export function episodeKey(season: number, episode: number): string {
  return `${String(season)}:${String(episode)}`
}

/** How long after its newest air date a show still counts as "airing". */
const STILL_AIRING_WINDOW_DAYS = 120
const DAY_MS = 24 * 60 * 60 * 1000

/**
 * A TMDB `YYYY-MM-DD` air date as LOCAL midnight. `new Date('2025-10-18')`
 * would parse as UTC and land on the previous day west of Greenwich.
 */
export function parseAirDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value)
  if (!match) return null
  const [, y, m, d] = match
  return new Date(Number(y), Number(m) - 1, Number(d))
}

function startOfDay(now: Date): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate())
}

/** Aired = has a known air date that is today or earlier. A null date is not aired. */
export function hasAired(airDate: string | null | undefined, now: Date = new Date()): boolean {
  const date = parseAirDate(airDate)
  return date !== null && date.getTime() <= startOfDay(now).getTime()
}

/**
 * Whether the show still looks like it is airing, judged from its NEWEST
 * season's episodes: any episode with no air date yet, one in the future, or
 * one that aired within the last 120 days. An empty season reads as ended.
 *
 * This is a heuristic. TMDB's show-level `in_production` / `status` flag would
 * be the honest answer; swap this out once the seasons endpoint exposes it.
 */
export function isLikelyStillAiring(
  episodes: readonly Pick<TvEpisodeItem, 'air_date'>[],
  now: Date = new Date(),
): boolean {
  const cutoff = startOfDay(now).getTime() - STILL_AIRING_WINDOW_DAYS * DAY_MS
  return episodes.some((episode) => {
    const date = parseAirDate(episode.air_date)
    return date === null || date.getTime() >= cutoff
  })
}

/** The preselected choice: Latest season while a show airs, Every season once it has ended. */
export function defaultTvChoice(
  newestSeasonEpisodes: readonly Pick<TvEpisodeItem, 'air_date'>[],
  now: Date = new Date(),
): TvChoice {
  return isLikelyStillAiring(newestSeasonEpisodes, now) ? 'latest_season' : 'every'
}

/** `"Sat, Oct 18"`, with the year when it isn't this year; `"Date TBA"` when unknown. */
export function formatAirDate(airDate: string | null | undefined, now: Date = new Date()): string {
  const date = parseAirDate(airDate)
  if (!date) return 'Date TBA'
  return date.toLocaleDateString('en-US', {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
  })
}

/** `"Nov 8"` for the "Airs Nov 8" badge; `"TBA"` when unknown. */
export function formatShortAirDate(airDate: string | null | undefined): string {
  const date = parseAirDate(airDate)
  if (!date) return 'TBA'
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

/** `90` → `"1h 30m"`, `45` → `"45m"`, `60` → `"1h"`. */
export function formatRuntime(minutes: number): string {
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours === 0) return `${String(rest)}m`
  return rest === 0 ? `${String(hours)}h` : `${String(hours)}h ${String(rest)}m`
}

function plural(count: number, one: string, many: string): string {
  return `${String(count)} ${count === 1 ? one : many}`
}

/** `[4]` → `"Season 4"`, `[2, 1]` → `"Seasons 1, 2"`. */
export function formatSeasonList(seasons: readonly number[]): string {
  const sorted = [...seasons].sort((a, b) => a - b)
  return sorted.length === 1
    ? `Season ${String(sorted[0])}`
    : `Seasons ${sorted.map(String).join(', ')}`
}

/** What the request already covers, so the sheet can show it as checked and locked. */
export interface AlreadyRequested {
  /** The request tracks the whole show; nothing more can be added. */
  wholeShow: boolean
  /** Seasons requested in full. */
  seasons: ReadonlySet<number>
  /** Seasons requested as an episode subset → those episodes. */
  episodes: ReadonlyMap<number, ReadonlySet<number>>
}

export const NOTHING_REQUESTED: AlreadyRequested = {
  wholeShow: false,
  seasons: new Set(),
  episodes: new Map(),
}

/**
 * Read the existing request's intent (`tv_request_mode` + `requested_seasons` /
 * `requested_episodes`). A season listed in `requested_seasons` that ALSO has
 * an `requested_episodes` entry is only partly requested (the backend keeps
 * both). A legacy row with no recorded intent falls back to its tracked seasons.
 */
export function alreadyRequestedFrom(request: RequestResponse | null | undefined): AlreadyRequested {
  if (!request || request.media_type !== 'tv') return NOTHING_REQUESTED
  if (request.tv_request_mode === 'whole_show') {
    return { wholeShow: true, seasons: new Set(), episodes: new Map() }
  }
  const episodes = new Map<number, ReadonlySet<number>>()
  for (const [season, values] of Object.entries(request.requested_episodes ?? {})) {
    episodes.set(Number(season), new Set(values))
  }
  const seasonSource =
    request.requested_seasons ??
    (request.tv_request_mode ? [] : (request.seasons ?? []).map((s) => s.season_number))
  const seasons = new Set(seasonSource.filter((season) => !episodes.has(season)))
  return { wholeShow: false, seasons, episodes }
}

/** True when this exact episode is already covered by the existing request. */
export function isEpisodeRequested(
  already: AlreadyRequested,
  season: number,
  episode: number,
): boolean {
  return (
    already.wholeShow || already.seasons.has(season) || (already.episodes.get(season)?.has(episode) ?? false)
  )
}

export interface TvChoiceInput {
  choice: TvChoice
  /** The newest season number, or null when TMDB lists none. */
  latestSeason: number | null
  episodePicks: EpisodePicks
  seasonPicks: readonly number[]
}

/** The request body fields for a choice (merged onto `{ tmdb_id, media_type: 'tv' }`). */
export function buildTvRequestBody(
  input: TvChoiceInput,
): Pick<CreateRequestBody, 'tv_mode' | 'seasons' | 'episodes'> {
  switch (input.choice) {
    case 'latest_season':
      return { tv_mode: 'latest_season' }
    case 'new_episodes':
      return { tv_mode: 'new_episodes' }
    case 'episodes': {
      const episodes: Record<string, number[]> = {}
      for (const pick of Object.values(input.episodePicks)) {
        ;(episodes[String(pick.season)] ??= []).push(pick.episode)
      }
      for (const list of Object.values(episodes)) list.sort((a, b) => a - b)
      return { episodes }
    }
    case 'seasons':
      return { seasons: [...input.seasonPicks].sort((a, b) => a - b) }
    case 'every':
      // Omitting `seasons` is the backend's "whole show".
      return {}
  }
}

export interface TvChoiceWords {
  /** The footer's restatement, e.g. "3 episodes from Season 51". */
  summary: string
  /** A trailing note, e.g. "1 grabbed after it airs". */
  note: string | null
  /** The primary button, e.g. "Request 3 episodes". */
  button: string
  /** False when nothing is picked yet: the button is disabled. */
  ready: boolean
  /** Short form for the success toast, e.g. "Season 4". */
  toast: string
}

export function describeTvChoice(input: TvChoiceInput): TvChoiceWords {
  switch (input.choice) {
    case 'latest_season': {
      if (input.latestSeason === null) {
        return {
          summary: 'No seasons listed yet',
          note: null,
          button: 'Request latest season',
          ready: false,
          toast: 'Latest season',
        }
      }
      const season = `Season ${String(input.latestSeason)}`
      return {
        summary: `${season} only`,
        note: null,
        button: `Request ${season}`,
        ready: true,
        toast: season,
      }
    }
    case 'new_episodes':
      return {
        summary: 'New episodes from now on',
        note: null,
        button: 'Get new episodes',
        ready: true,
        toast: 'New episodes as they air',
      }
    case 'episodes': {
      const picks = Object.values(input.episodePicks)
      if (picks.length === 0) {
        return {
          summary: 'Pick the episodes you want',
          note: null,
          button: 'Request episodes',
          ready: false,
          toast: '',
        }
      }
      const seasons = [...new Set(picks.map((p) => p.season))]
      const unaired = picks.filter((p) => p.unaired).length
      const count = plural(picks.length, 'episode', 'episodes')
      return {
        summary: `${count} from ${formatSeasonList(seasons)}`,
        note: unaired > 0 ? `${String(unaired)} grabbed after ${unaired === 1 ? 'it airs' : 'they air'}` : null,
        button: `Request ${count}`,
        ready: true,
        toast: count,
      }
    }
    case 'seasons': {
      const picks = input.seasonPicks
      if (picks.length === 0) {
        return {
          summary: 'Pick the seasons you want',
          note: null,
          button: 'Request seasons',
          ready: false,
          toast: '',
        }
      }
      if (picks.length === 1) {
        const season = formatSeasonList(picks)
        return {
          summary: `${season} only`,
          note: null,
          button: `Request ${season}`,
          ready: true,
          toast: season,
        }
      }
      return {
        summary: formatSeasonList(picks),
        note: null,
        button: `Request ${plural(picks.length, 'season', 'seasons')}`,
        ready: true,
        toast: formatSeasonList(picks),
      }
    }
    case 'every':
      return {
        summary: 'Every season',
        note: null,
        button: 'Request every season',
        ready: true,
        toast: 'Every season',
      }
  }
}
