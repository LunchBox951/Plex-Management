import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useCreateRequest, useTvSeasonEpisodes, useTvSeasons } from '../api/hooks'
import type { RequestResponse, TvEpisodeItem } from '../api/types'
import { TvRequestPicker } from './TvRequestPicker'

vi.mock('../api/hooks', () => ({
  useCreateRequest: vi.fn(),
  useTvSeasons: vi.fn(),
  useTvSeasonEpisodes: vi.fn(),
}))

const toastState = vi.hoisted(() => ({ toast: vi.fn() }))
vi.mock('./ui/toast', () => ({ useToast: () => ({ toast: toastState.toast }) }))

/** A local `YYYY-MM-DD`, `days` from today (negative = in the past). */
function day(days: number): string {
  const date = new Date()
  date.setDate(date.getDate() + days)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${String(date.getFullYear())}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

function episode(n: number, overrides: Partial<TvEpisodeItem> = {}): TvEpisodeItem {
  return {
    episode_number: n,
    air_date: day(-30 + n),
    name: null,
    overview: null,
    still_url: null,
    runtime_minutes: null,
    in_library: null,
    ...overrides,
  }
}

const CREATED: RequestResponse = {
  id: 70,
  tmdb_id: 500,
  media_type: 'tv',
  title: 'Test Show',
  status: 'pending',
  is_anime: false,
  keep_forever: false,
  can_mutate: true,
  is_owner: true,
  can_withdraw: false,
  has_other_participants: false,
}

function success<T>(data: T) {
  return { data, isSuccess: true, isPending: false, isError: false, error: null, refetch: vi.fn() }
}

const PENDING = {
  data: undefined,
  isSuccess: false,
  isPending: true,
  isError: false,
  error: null,
  refetch: vi.fn(),
}

/** Wire the season list and each season's episodes (unlisted seasons stay pending). */
function setup(options: {
  seasons: number[]
  episodes: Record<number, TvEpisodeItem[]>
  create?: { mutateAsync: Mock; isPending: boolean }
  existingRequest?: RequestResponse | null
}) {
  const create = options.create ?? { mutateAsync: vi.fn().mockResolvedValue(CREATED), isPending: false }
  ;(useCreateRequest as unknown as Mock).mockReturnValue(create)
  ;(useTvSeasons as unknown as Mock).mockReturnValue(
    success({ tmdb_id: 500, title: 'Test Show', seasons: options.seasons }),
  )
  ;(useTvSeasonEpisodes as unknown as Mock).mockImplementation(
    (_tmdbId: number, season: number | null) => {
      const list = season == null ? undefined : options.episodes[season]
      return list
        ? success({ tmdb_id: 500, season_number: season, episodes: list })
        : PENDING
    },
  )
  const onOpenChange = vi.fn()
  const onRequested = vi.fn()
  render(
    <TvRequestPicker
      open
      onOpenChange={onOpenChange}
      tmdbId={500}
      title="Test Show"
      existingRequest={options.existingRequest}
      onRequested={onRequested}
    />,
  )
  return { create, onOpenChange, onRequested }
}

// An airing show: season 3's last episode airs next week.
const AIRING = {
  seasons: [1, 2, 3],
  episodes: {
    3: [
      episode(1, {
        name: 'Pilot',
        overview: 'It begins.',
        runtime_minutes: 90,
        still_url: 'https://image.tmdb.org/t/p/w300/s1.jpg',
        in_library: true,
      }),
      episode(2),
      episode(3, { name: 'Later', air_date: day(7) }),
      episode(4, { air_date: null }),
    ],
    2: [episode(1, { air_date: '2021-01-01' }), episode(2, { air_date: '2021-01-08' })],
  },
}

// An ended show: its newest season finished years ago.
const ENDED = {
  seasons: [1, 2],
  episodes: { 2: [episode(1, { air_date: '2019-05-19' })] },
}

function footerButton(name: string | RegExp) {
  return screen.getByRole('button', { name })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('TvRequestPicker — default choice', () => {
  it('defaults to Latest season for a show that is still airing, and requests it', async () => {
    const { create, onOpenChange, onRequested } = setup(AIRING)

    expect(screen.getByRole('radio', { name: 'Latest season' })).toBeChecked()
    expect(screen.getByText('Season 3 only')).toBeInTheDocument()

    fireEvent.click(footerButton('Request Season 3'))

    await waitFor(() =>
      expect(create.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 500,
        media_type: 'tv',
        tv_mode: 'latest_season',
      }),
    )
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false))
    expect(onRequested).toHaveBeenCalledWith(CREATED, {
      tmdb_id: 500,
      media_type: 'tv',
      tv_mode: 'latest_season',
    })
    expect(toastState.toast).toHaveBeenCalledWith({
      title: 'Requested Test Show',
      description: 'Season 3',
      intent: 'success',
    })
  })

  it('defaults to Every season for a show that has ended, and sends no seasons', async () => {
    const { create } = setup(ENDED)

    expect(screen.getByRole('radio', { name: 'Every season' })).toBeChecked()
    fireEvent.click(footerButton('Request every season'))

    await waitFor(() =>
      expect(create.mutateAsync).toHaveBeenCalledWith({ tmdb_id: 500, media_type: 'tv' }),
    )
  })

  it('lists the five choices in order', () => {
    setup(AIRING)
    expect(screen.getAllByRole('radio').map((r) => r.getAttribute('value'))).toEqual([
      'latest_season',
      'new_episodes',
      'episodes',
      'seasons',
      'every',
    ])
  })
})

describe('TvRequestPicker — New episodes as they air', () => {
  it('shows the first upcoming episode and sends tv_mode new_episodes', async () => {
    const { create } = setup(AIRING)
    fireEvent.click(screen.getByRole('radio', { name: 'New episodes as they air' }))

    expect(screen.getByText('New episodes from now on')).toBeInTheDocument()
    expect(screen.getByText('S3 E3 · Later')).toBeInTheDocument()

    fireEvent.click(footerButton('Get new episodes'))
    await waitFor(() =>
      expect(create.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 500,
        media_type: 'tv',
        tv_mode: 'new_episodes',
      }),
    )
  })
})

describe('TvRequestPicker — Choose episodes', () => {
  it('shows newest-first season tabs and rich, honest episode rows', () => {
    setup(AIRING)
    fireEvent.click(screen.getByRole('radio', { name: 'Choose episodes' }))

    expect(screen.getAllByRole('tab').map((t) => t.textContent)).toEqual([
      'Season 3',
      'Season 2',
      'Season 1',
    ])
    expect(screen.getByRole('tab', { name: 'Season 3' })).toHaveAttribute('aria-selected', 'true')

    const list = screen.getByRole('list', { name: 'Season 3 episodes' })
    // In Plex: checked, locked, badged; its still image and overview render.
    const pilot = within(list).getByRole('checkbox', { name: 'E1 Pilot' })
    expect(pilot).toBeChecked()
    expect(pilot).toBeDisabled()
    expect(within(list).getByText('In Plex')).toBeInTheDocument()
    expect(within(list).getByText('It begins.')).toBeInTheDocument()
    expect(list.querySelector('img')).toHaveAttribute('src', 'https://image.tmdb.org/t/p/w300/s1.jpg')
    // Name fallback, unaired badge, unknown date.
    expect(within(list).getByRole('checkbox', { name: 'E2 Episode 2' })).toBeEnabled()
    expect(within(list).getByText(/^Airs /)).toBeInTheDocument()
    expect(within(list).getAllByText('NOT AIRED YET')).toHaveLength(1)
    expect(within(list).getByText('Date TBA')).toBeInTheDocument()
  })

  it('disables Request until something is picked, then restates the picks', async () => {
    const { create } = setup(AIRING)
    fireEvent.click(screen.getByRole('radio', { name: 'Choose episodes' }))

    expect(footerButton('Request episodes')).toBeDisabled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'E2 Episode 2' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'E3 Later' }))

    expect(screen.getByText('2 episodes from Season 3')).toBeInTheDocument()
    expect(screen.getByText(/1 grabbed after it airs/)).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Season 3, 2 picked' })).toBeInTheDocument()

    // Unticking takes it back out.
    fireEvent.click(screen.getByRole('checkbox', { name: 'E3 Later' }))
    expect(screen.getByText('1 episode from Season 3')).toBeInTheDocument()

    fireEvent.click(footerButton('Request 1 episode'))
    await waitFor(() =>
      expect(create.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 500,
        media_type: 'tv',
        episodes: { '3': [2] },
      }),
    )
  })

  it('"Select all aired" picks aired episodes not already in Plex, across tabs', async () => {
    const { create } = setup(AIRING)
    fireEvent.click(screen.getByRole('radio', { name: 'Choose episodes' }))

    fireEvent.click(screen.getByRole('button', { name: 'Select all aired' }))
    // Season 3: E1 is in Plex, E3/E4 haven't aired -> only E2.
    expect(screen.getByRole('checkbox', { name: 'E2 Episode 2' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'E3 Later' })).not.toBeChecked()

    fireEvent.click(screen.getByRole('tab', { name: 'Season 2' }))
    fireEvent.click(screen.getByRole('button', { name: 'Select all aired' }))

    expect(screen.getByText('3 episodes from Seasons 2, 3')).toBeInTheDocument()
    fireEvent.click(footerButton('Request 3 episodes'))
    await waitFor(() =>
      expect(create.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 500,
        media_type: 'tv',
        episodes: { '3': [2], '2': [1, 2] },
      }),
    )
  })
})

describe('TvRequestPicker — Choose seasons', () => {
  it('requests the ticked seasons and words one season as "Season N"', async () => {
    const { create } = setup(AIRING)
    fireEvent.click(screen.getByRole('radio', { name: 'Choose seasons' }))

    expect(footerButton('Request seasons')).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Season 2' }))
    expect(screen.getByText('Season 2 only')).toBeInTheDocument()
    expect(footerButton('Request Season 2')).toBeEnabled()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Season 1' }))
    expect(screen.getByText('Seasons 1, 2')).toBeInTheDocument()

    fireEvent.click(footerButton('Request 2 seasons'))
    await waitFor(() =>
      expect(create.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 500,
        media_type: 'tv',
        seasons: [1, 2],
      }),
    )
  })
})

describe('TvRequestPicker — Request more', () => {
  it('shows what the request already covers as checked and locked', () => {
    setup({
      ...AIRING,
      existingRequest: {
        ...CREATED,
        tv_request_mode: 'explicit_episodes',
        requested_seasons: [1, 3],
        requested_episodes: { '3': [2] },
      },
    })
    expect(screen.getByText(/Season 1 requested/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('radio', { name: 'Choose seasons' }))
    expect(screen.getByRole('checkbox', { name: 'Season 1' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'Season 1' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Season 3' })).toBeEnabled()

    fireEvent.click(screen.getByRole('radio', { name: 'Choose episodes' }))
    const requested = screen.getByRole('checkbox', { name: 'E2 Episode 2' })
    expect(requested).toBeChecked()
    expect(requested).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'E3 Later' })).toBeEnabled()
  })
})

describe('TvRequestPicker — failures stay in the sheet', () => {
  it('says "not available yet" for a 501 and keeps the sheet open', async () => {
    const create = {
      mutateAsync: vi.fn().mockRejectedValue({
        code: 'not_implemented',
        message: 'Not implemented',
        status: 501,
      }),
      isPending: false,
    }
    const { onOpenChange, onRequested } = setup({ ...AIRING, create })

    fireEvent.click(footerButton('Request Season 3'))

    expect(
      await screen.findByText("Requesting only the latest season isn't available yet."),
    ).toBeInTheDocument()
    expect(screen.getByText(/Choose seasons/, { selector: 'div' })).toBeInTheDocument()
    expect(onOpenChange).not.toHaveBeenCalled()
    expect(onRequested).not.toHaveBeenCalled()
    expect(toastState.toast).not.toHaveBeenCalled()

    // Changing the choice clears the stale notice.
    fireEvent.click(screen.getByRole('radio', { name: 'Every season' }))
    expect(
      screen.queryByText("Requesting only the latest season isn't available yet."),
    ).not.toBeInTheDocument()
  })

  it('shows any other error inline above the buttons', async () => {
    const create = {
      mutateAsync: vi.fn().mockRejectedValue({
        code: 'tmdb_unavailable',
        message: 'TMDB is unavailable right now. Try again shortly.',
        status: 503,
      }),
      isPending: false,
    }
    const { onOpenChange } = setup({ ...ENDED, create })

    fireEvent.click(footerButton('Request every season'))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'TMDB is unavailable right now. Try again shortly.',
    )
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('renders nothing while closed', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
    render(
      <TvRequestPicker open={false} onOpenChange={() => {}} tmdbId={500} title="Test Show" />,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(useTvSeasons).not.toHaveBeenCalled()
  })
})
