import { describe, expect, it } from 'vitest'
import type { RequestResponse } from '../api/types'
import { compactRuns, describeRequestScope } from './requestScope'

function request(overrides: Partial<RequestResponse>): RequestResponse {
  return {
    id: 1,
    tmdb_id: 1,
    media_type: 'tv',
    title: 'Show',
    status: 'pending',
    is_anime: false,
    keep_forever: false,
    can_mutate: false,
    is_owner: true,
    can_withdraw: false,
    has_other_participants: false,
    ...overrides,
  }
}

describe('compactRuns', () => {
  it('folds consecutive numbers into ranges and dedupes', () => {
    expect(compactRuns([5, 1, 2, 3, 3, 7, 8])).toBe('1–3, 5, 7–8')
    expect(compactRuns([4])).toBe('4')
    expect(compactRuns([])).toBe('')
  })
})

describe('describeRequestScope', () => {
  it('names a movie and its year', () => {
    expect(describeRequestScope(request({ media_type: 'movie', year: 2023 }))).toBe('Movie · 2023')
  })

  it('describes explicit episodes per season', () => {
    expect(
      describeRequestScope(
        request({ tv_request_mode: 'explicit_episodes', requested_episodes: { 51: [4, 5, 6] } }),
      ),
    ).toBe('TV · S51 E4–6')
  })

  it('describes explicit seasons', () => {
    expect(
      describeRequestScope(request({ tv_request_mode: 'explicit_seasons', requested_seasons: [1, 2] })),
    ).toBe('TV · Seasons 1–2')
    expect(
      describeRequestScope(request({ tv_request_mode: 'explicit_seasons', requested_seasons: [4] })),
    ).toBe('TV · Season 4')
  })

  it('falls back to every season for a whole-show request', () => {
    expect(describeRequestScope(request({ tv_request_mode: 'whole_show' }))).toBe('TV · Every season')
  })
})
