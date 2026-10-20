import { describe, expect, it } from 'vitest'
import type { RequestResponse } from '../api/types'
import {
  alreadyRequestedFrom,
  buildTvRequestBody,
  defaultTvChoice,
  describeTvChoice,
  episodeKey,
  formatAirDate,
  formatRuntime,
  formatSeasonList,
  hasAired,
  isEpisodeRequested,
  isLikelyStillAiring,
  parseAirDate,
  type EpisodePicks,
  type TvChoiceInput,
} from './tvRequest'

// Tue, Oct 20 2026, local time.
const NOW = new Date(2026, 9, 20, 15, 30)

function input(overrides: Partial<TvChoiceInput>): TvChoiceInput {
  return { choice: 'every', latestSeason: 4, episodePicks: {}, seasonPicks: [], ...overrides }
}

function picks(...entries: [number, number, boolean?][]): EpisodePicks {
  return Object.fromEntries(
    entries.map(([season, episode, unaired = false]) => [
      episodeKey(season, episode),
      { season, episode, unaired },
    ]),
  )
}

describe('isLikelyStillAiring (the stand-in for a real in_production flag)', () => {
  it('reads a season with an episode in the future as airing', () => {
    expect(isLikelyStillAiring([{ air_date: '2025-01-01' }, { air_date: '2026-11-08' }], NOW)).toBe(
      true,
    )
  })

  it('reads an episode with no air date yet as airing', () => {
    expect(isLikelyStillAiring([{ air_date: '2020-01-01' }, { air_date: null }], NOW)).toBe(true)
  })

  it('reads an episode that aired within the last 120 days as airing', () => {
    // 2026-06-22 is exactly 120 days before Oct 20.
    expect(isLikelyStillAiring([{ air_date: '2026-06-22' }], NOW)).toBe(true)
    expect(isLikelyStillAiring([{ air_date: '2026-06-21' }], NOW)).toBe(false)
  })

  it('reads a long-finished season, or an empty one, as ended', () => {
    expect(isLikelyStillAiring([{ air_date: '2019-05-19' }], NOW)).toBe(false)
    expect(isLikelyStillAiring([], NOW)).toBe(false)
  })

  it('defaults to Latest season while airing and Every season once ended', () => {
    expect(defaultTvChoice([{ air_date: '2026-10-18' }], NOW)).toBe('latest_season')
    expect(defaultTvChoice([{ air_date: '2019-05-19' }], NOW)).toBe('every')
  })
})

describe('dates and runtimes', () => {
  it('parses TMDB dates as local midnight (no UTC day shift)', () => {
    const date = parseAirDate('2025-10-18')
    expect(date?.getFullYear()).toBe(2025)
    expect(date?.getMonth()).toBe(9)
    expect(date?.getDate()).toBe(18)
    expect(parseAirDate(null)).toBeNull()
    expect(parseAirDate('soon')).toBeNull()
  })

  it('counts today as aired and a null date as not aired', () => {
    expect(hasAired('2026-10-20', NOW)).toBe(true)
    expect(hasAired('2026-10-21', NOW)).toBe(false)
    expect(hasAired(null, NOW)).toBe(false)
  })

  it('formats air dates like "Sat, Oct 18", adding the year only when it differs', () => {
    expect(formatAirDate('2026-10-17', NOW)).toBe('Sat, Oct 17')
    expect(formatAirDate('1975-10-11', NOW)).toBe('Sat, Oct 11, 1975')
    expect(formatAirDate(null, NOW)).toBe('Date TBA')
  })

  it('formats runtimes', () => {
    expect(formatRuntime(90)).toBe('1h 30m')
    expect(formatRuntime(45)).toBe('45m')
    expect(formatRuntime(60)).toBe('1h')
  })

  it('formats season lists in ascending order', () => {
    expect(formatSeasonList([4])).toBe('Season 4')
    expect(formatSeasonList([2, 1])).toBe('Seasons 1, 2')
  })
})

describe('buildTvRequestBody', () => {
  it('sends tv_mode for the two automatic choices', () => {
    expect(buildTvRequestBody(input({ choice: 'latest_season' }))).toEqual({
      tv_mode: 'latest_season',
    })
    expect(buildTvRequestBody(input({ choice: 'new_episodes' }))).toEqual({
      tv_mode: 'new_episodes',
    })
  })

  it('sends episodes grouped by season, sorted', () => {
    expect(
      buildTvRequestBody(input({ choice: 'episodes', episodePicks: picks([51, 5], [51, 3], [50, 1]) })),
    ).toEqual({ episodes: { '51': [3, 5], '50': [1] } })
  })

  it('sends sorted seasons, and nothing at all for every season', () => {
    expect(buildTvRequestBody(input({ choice: 'seasons', seasonPicks: [2, 1] }))).toEqual({
      seasons: [1, 2],
    })
    expect(buildTvRequestBody(input({ choice: 'every' }))).toEqual({})
  })
})

describe('describeTvChoice: the footer restates the choice and the button repeats it', () => {
  it.each([
    [input({ choice: 'latest_season', latestSeason: 4 }), 'Season 4 only', 'Request Season 4'],
    [input({ choice: 'new_episodes' }), 'New episodes from now on', 'Get new episodes'],
    [
      input({ choice: 'episodes', episodePicks: picks([51, 3], [51, 4], [51, 6, true]) }),
      '3 episodes from Season 51',
      'Request 3 episodes',
    ],
    [input({ choice: 'episodes', episodePicks: picks([51, 3]) }), '1 episode from Season 51', 'Request 1 episode'],
    [input({ choice: 'seasons', seasonPicks: [4] }), 'Season 4 only', 'Request Season 4'],
    [input({ choice: 'seasons', seasonPicks: [2, 1] }), 'Seasons 1, 2', 'Request 2 seasons'],
    [input({ choice: 'every' }), 'Every season', 'Request every season'],
  ])('%#: %s', (choiceInput, summary, button) => {
    const words = describeTvChoice(choiceInput)
    expect(words.summary).toBe(summary)
    expect(words.button).toBe(button)
    expect(words.ready).toBe(true)
  })

  it('notes picked episodes that are grabbed after they air', () => {
    const words = describeTvChoice(
      input({ choice: 'episodes', episodePicks: picks([51, 3], [51, 6, true]) }),
    )
    expect(words.note).toBe('1 grabbed after it airs')
  })

  it('is not ready while nothing is picked in the choose modes', () => {
    expect(describeTvChoice(input({ choice: 'episodes' })).ready).toBe(false)
    expect(describeTvChoice(input({ choice: 'seasons' })).ready).toBe(false)
    expect(describeTvChoice(input({ choice: 'latest_season', latestSeason: null })).ready).toBe(false)
  })
})

describe('alreadyRequestedFrom', () => {
  const base: RequestResponse = {
    id: 1,
    tmdb_id: 9,
    media_type: 'tv',
    title: 'Show',
    status: 'downloading',
    is_anime: false,
    keep_forever: false,
    can_mutate: true,
    is_owner: true,
    can_withdraw: false,
    has_other_participants: false,
  }

  it('reads whole seasons and episode subsets separately', () => {
    const already = alreadyRequestedFrom({
      ...base,
      tv_request_mode: 'explicit_episodes',
      requested_seasons: [1, 3],
      requested_episodes: { '3': [2, 4] },
    })
    expect([...already.seasons]).toEqual([1])
    expect(isEpisodeRequested(already, 1, 7)).toBe(true)
    expect(isEpisodeRequested(already, 3, 2)).toBe(true)
    expect(isEpisodeRequested(already, 3, 3)).toBe(false)
    expect(isEpisodeRequested(already, 2, 1)).toBe(false)
  })

  it('treats a whole-show request as covering everything', () => {
    const already = alreadyRequestedFrom({ ...base, tv_request_mode: 'whole_show' })
    expect(already.wholeShow).toBe(true)
    expect(isEpisodeRequested(already, 12, 1)).toBe(true)
  })

  it('falls back to tracked seasons for a row with no recorded intent', () => {
    const already = alreadyRequestedFrom({
      ...base,
      seasons: [{ season_number: 2, status: 'downloading' }],
    })
    expect([...already.seasons]).toEqual([2])
  })

  it('reads nothing for no request or a movie', () => {
    expect(alreadyRequestedFrom(null).seasons.size).toBe(0)
    expect(alreadyRequestedFrom({ ...base, media_type: 'movie' }).wholeShow).toBe(false)
  })
})
