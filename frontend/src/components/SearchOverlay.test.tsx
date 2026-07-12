import * as RadixDialog from '@radix-ui/react-dialog'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useDiscoverHome,
  useDiscoverSearch,
  useRequests,
  useRequestsInvalidated,
} from '../api/hooks'
import type { DiscoverResult, RequestResponse } from '../api/types'
import { resetSettleObservations } from '../lib/tileState'
import { SearchOverlay } from './SearchOverlay'

vi.mock('../api/hooks', () => ({
  useDiscoverHome: vi.fn(),
  useDiscoverSearch: vi.fn(),
  useRequests: vi.fn(),
  useRequestsInvalidated: vi.fn(() => false),
  useCreateRequest: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
}))

vi.mock('./ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }))

// Keep nested-dialog behavior real while avoiding TitleDetailModal's unrelated
// request/queue/correction hook surface. This preserves Radix layer ordering,
// Escape handling, focus restoration, and portal behavior for the overlay tests.
vi.mock('./TitleDetailModal', () => ({
  TitleDetailModal: ({
    title,
    open,
    onOpenChange,
    returnFocusTo,
  }: {
    title: DiscoverResult | null
    open: boolean
    onOpenChange: (open: boolean) => void
    returnFocusTo?: HTMLElement | null | (() => HTMLElement | null)
  }) => (
    <RadixDialog.Root open={open} onOpenChange={onOpenChange}>
      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50" />
        <RadixDialog.Content
          className="fixed z-50"
          aria-label={title ? `Details for ${title.title}` : undefined}
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            const target =
              typeof returnFocusTo === 'function' ? returnFocusTo() : returnFocusTo
            target?.focus()
          }}
        >
          <RadixDialog.Title>{title ? `Details for ${title.title}` : 'Title details'}</RadixDialog.Title>
          <RadixDialog.Description>Request details and actions.</RadixDialog.Description>
          <RadixDialog.Close>Close details</RadixDialog.Close>
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  ),
}))

const POPULAR_MOVIE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 1,
  title: 'Popular Movie',
  year: 2020,
  library_state: 'none',
}

const POPULAR_SHOW: DiscoverResult = {
  media_type: 'tv',
  tmdb_id: 2,
  title: 'Popular Show',
  year: 2021,
  library_state: 'none',
}

const TRENDING_MOVIE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 3,
  title: 'Trending Only',
  year: 2022,
  library_state: 'none',
}

const OWNED_MOVIE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 4,
  title: 'Owned Movie',
  year: 2023,
  library_state: 'available',
}

const FRESH_MOVIE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 5,
  title: 'Fresh Movie',
  year: 2024,
  library_state: 'none',
}

const RETRY_SHOW: DiscoverResult = {
  media_type: 'tv',
  tmdb_id: 6,
  title: 'Retry Show',
  year: 2025,
  library_state: 'none',
}

const DETAIL_MOVIE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 7,
  title: 'Detail Movie',
  year: 2026,
  library_state: 'none',
}

function requestRow(overrides: Partial<RequestResponse> = {}): RequestResponse {
  return {
    id: 11,
    tmdb_id: RETRY_SHOW.tmdb_id,
    media_type: 'tv',
    title: RETRY_SHOW.title,
    status: 'failed',
    is_anime: false,
    keep_forever: false,
    seasons: [{ season_number: 2, status: 'failed' }],
    ...overrides,
  }
}

function setHome(overrides: Record<string, unknown> = {}) {
  ;(useDiscoverHome as unknown as Mock).mockReturnValue({
    data: {
      spotlight: null,
      rows: [
        { row_type: 'popular', title: 'Popular movies', items: [POPULAR_MOVIE] },
        { row_type: 'trending', title: 'Trending', items: [TRENDING_MOVIE] },
        {
          row_type: 'popular_tv',
          title: 'Popular TV',
          items: [POPULAR_SHOW, POPULAR_MOVIE],
        },
      ],
    },
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
    dataUpdatedAt: 100,
    ...overrides,
  })
}

function setSearch(overrides: Record<string, unknown> = {}) {
  ;(useDiscoverSearch as unknown as Mock).mockReturnValue({
    data: undefined,
    isFetching: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
    dataUpdatedAt: 200,
    ...overrides,
  })
}

function setRequests(rows: RequestResponse[] = [], overrides: Record<string, unknown> = {}) {
  ;(useRequests as unknown as Mock).mockReturnValue({
    data: { requests: rows },
    isSuccess: true,
    dataUpdatedAt: 300,
    ...overrides,
  })
}

function openOverlay(): { trigger: HTMLButtonElement; input: HTMLInputElement } {
  const trigger = screen.getByRole('button', {
    name: 'Search TMDB to request',
  }) as HTMLButtonElement
  trigger.focus()
  fireEvent.click(trigger)
  const input = screen.getByRole('searchbox', { name: 'Search TMDB' }) as HTMLInputElement
  return { trigger, input }
}

async function enterSearch(query: string): Promise<HTMLInputElement> {
  const input = screen.getByRole('searchbox', { name: 'Search TMDB' }) as HTMLInputElement
  fireEvent.change(input, { target: { value: query } })
  await waitFor(
    () => {
      expect(useDiscoverSearch).toHaveBeenCalledWith(query)
    },
    { timeout: 1500 },
  )
  return input
}

beforeEach(() => {
  vi.clearAllMocks()
  resetSettleObservations()
  setHome()
  setSearch()
  setRequests()
  ;(useRequestsInvalidated as unknown as Mock).mockReturnValue(false)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('SearchOverlay — trigger, keyboard, and focus', () => {
  it('focuses the input on open and restores trigger focus after close button or Escape', async () => {
    render(<SearchOverlay />)

    const first = openOverlay()
    expect(first.input).toHaveFocus()
    fireEvent.change(first.input, { target: { value: 'temporary query' } })

    fireEvent.click(screen.getByRole('button', { name: 'Close search' }))
    await waitFor(() => {
      expect(screen.queryByRole('searchbox', { name: 'Search TMDB' })).not.toBeInTheDocument()
      expect(first.trigger).toHaveFocus()
    })

    const second = openOverlay()
    expect(second.input).toHaveValue('')
    fireEvent.keyDown(second.input, { key: 'Escape' })
    await waitFor(() => {
      expect(screen.queryByRole('searchbox', { name: 'Search TMDB' })).not.toBeInTheDocument()
      expect(second.trigger).toHaveFocus()
    })
  })

  it('opens with / and prevents the shortcut keystroke default', () => {
    render(<SearchOverlay />)
    const slash = new KeyboardEvent('keydown', { key: '/', bubbles: true, cancelable: true })

    act(() => document.dispatchEvent(slash))

    expect(slash.defaultPrevented).toBe(true)
    expect(screen.getByRole('searchbox', { name: 'Search TMDB' })).toHaveFocus()
  })

  it('does not hijack / from editable controls or another open dialog', () => {
    const first = render(
      <>
        <input aria-label="Elsewhere" />
        <SearchOverlay />
      </>,
    )
    const elsewhere = screen.getByRole('textbox', { name: 'Elsewhere' })
    elsewhere.focus()
    const editableSlash = new KeyboardEvent('keydown', {
      key: '/',
      bubbles: true,
      cancelable: true,
    })

    act(() => elsewhere.dispatchEvent(editableSlash))

    expect(editableSlash.defaultPrevented).toBe(false)
    expect(screen.queryByRole('searchbox', { name: 'Search TMDB' })).not.toBeInTheDocument()

    first.unmount()
    render(
      <>
        <div role="alertdialog" aria-label="Existing dialog" />
        <SearchOverlay />
      </>,
    )
    const dialogSlash = new KeyboardEvent('keydown', {
      key: '/',
      bubbles: true,
      cancelable: true,
    })

    act(() => document.dispatchEvent(dialogSlash))

    expect(dialogSlash.defaultPrevented).toBe(false)
    expect(screen.queryByRole('searchbox', { name: 'Search TMDB' })).not.toBeInTheDocument()
  })
})

describe('SearchOverlay — popular suggestions and debounced search', () => {
  it('combines popular movie and TV rows in server order and removes duplicates', () => {
    render(<SearchOverlay />)
    openOverlay()

    expect(screen.getByRole('heading', { name: 'Popular to request' })).toBeInTheDocument()
    const cards = screen.getAllByRole('button', { name: /View details for/ })
    expect(cards.map((card) => card.getAttribute('aria-label'))).toEqual([
      'View details for Popular Movie (2020)',
      'View details for Popular Show (2021)',
    ])
    expect(screen.queryByText(TRENDING_MOVIE.title)).not.toBeInTheDocument()
    expect(useDiscoverSearch).toHaveBeenCalledWith('')
  })

  it('does not enable a non-empty search until the full 300 ms debounce elapses', () => {
    vi.useFakeTimers()
    setSearch({ data: { results: [OWNED_MOVIE] } })
    render(<SearchOverlay />)
    const { input } = openOverlay()

    fireEvent.change(input, { target: { value: 'owned' } })
    expect(useDiscoverSearch).not.toHaveBeenCalledWith('owned')

    act(() => vi.advanceTimersByTime(299))
    expect(useDiscoverSearch).not.toHaveBeenCalledWith('owned')

    act(() => vi.advanceTimersByTime(1))
    expect(useDiscoverSearch).toHaveBeenCalledWith('owned')
    expect(screen.getByRole('img', { name: 'In library' })).toBeInTheDocument()
  })

  it('discards a superseded query: prior results never clobber a newer term', () => {
    // The mocked hook returns the same payload for any argument, so the ONLY
    // thing that can keep stale results off-screen is the client-side gate. This
    // proves out-of-order/late responses for a superseded query can never leak:
    // the instant the input changes, the overlay stops observing the old query
    // (passes '' to the hook, disabling it) and suppresses its results until the
    // debounce settles on the new term.
    vi.useFakeTimers()
    setSearch({ data: { results: [OWNED_MOVIE] } })
    render(<SearchOverlay />)
    const { input } = openOverlay()

    fireEvent.change(input, { target: { value: 'first' } })
    act(() => vi.advanceTimersByTime(300))
    expect(useDiscoverSearch).toHaveBeenCalledWith('first')
    expect(screen.getByRole('heading', { name: '1 result for “first”' })).toBeInTheDocument()

    // Type a newer term. Before the debounce settles the overlay must drop the
    // 'first' results and disable the in-flight query rather than show them under
    // the new term.
    fireEvent.change(input, { target: { value: 'firstly' } })
    expect(useDiscoverSearch).toHaveBeenLastCalledWith('')
    expect(useDiscoverSearch).not.toHaveBeenCalledWith('firstly')
    expect(
      screen.queryByRole('heading', { name: '1 result for “first”' }),
    ).not.toBeInTheDocument()
    expect(screen.getByText('Searching…')).toBeInTheDocument()

    // Once the debounce settles, results are attributed to the current term only.
    act(() => vi.advanceTimersByTime(300))
    expect(useDiscoverSearch).toHaveBeenLastCalledWith('firstly')
    expect(screen.getByRole('heading', { name: '1 result for “firstly”' })).toBeInTheDocument()
  })

  it('renders result glyphs and keeps the one-click request gate scope-safe', async () => {
    setSearch({ data: { results: [OWNED_MOVIE, FRESH_MOVIE, RETRY_SHOW] } })
    setRequests([requestRow()])
    render(<SearchOverlay />)
    openOverlay()

    await enterSearch('safe')

    expect(screen.getByRole('img', { name: 'In library' })).toBeInTheDocument()
    expect(screen.queryByText('In library')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Request Fresh Movie' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Request Retry Show' })).not.toBeInTheDocument()
  })
})

describe('SearchOverlay — honest search states', () => {
  it('shows Searching while the debounced query is fetching without results', async () => {
    setSearch({ isFetching: true })
    render(<SearchOverlay />)
    openOverlay()

    await enterSearch('loading')

    expect(screen.getByText('Searching…')).toBeInTheDocument()
  })

  it('shows Search failed with a working Retry action', async () => {
    const refetch = vi.fn()
    setSearch({ isError: true, error: new Error('TMDB is unavailable'), refetch })
    render(<SearchOverlay />)
    openOverlay()

    await enterSearch('error')
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(screen.getByText('Search failed')).toBeInTheDocument()
    expect(screen.getByText('TMDB is unavailable')).toBeInTheDocument()
    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('shows the existing no-match copy for an empty result set', async () => {
    setSearch({ data: { results: [] } })
    render(<SearchOverlay />)
    openOverlay()

    await enterSearch('missing')

    expect(screen.getByText('No matches')).toBeInTheDocument()
    expect(screen.getByText('Nothing on TMDB matched “missing”.')).toBeInTheDocument()
  })

  it('keeps cached results visible during a background refetch', async () => {
    setSearch({ data: { results: [OWNED_MOVIE] }, isFetching: true })
    render(<SearchOverlay />)
    openOverlay()

    await enterSearch('cached')

    expect(screen.getByRole('heading', { name: '1 result for “cached”' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /View details for Owned Movie/ })).toBeInTheDocument()
    expect(screen.getByRole('status', { name: '' })).toHaveTextContent('Updating…')
    expect(screen.queryByText('Searching…')).not.toBeInTheDocument()
  })
})

describe('SearchOverlay — nested title details', () => {
  it('preserves the query/results and closes details before search on successive Escapes', async () => {
    setSearch({ data: { results: [DETAIL_MOVIE] } })
    render(<SearchOverlay />)
    const { trigger } = openOverlay()
    const input = await enterSearch('detail')
    const card = screen.getByRole('button', { name: /View details for Detail Movie/ })
    card.focus()
    fireEvent.click(card)

    const details = screen.getByRole('dialog', { name: 'Details for Detail Movie' })
    expect(input).toBeInTheDocument()
    expect(input).toHaveValue('detail')

    fireEvent.keyDown(details, { key: 'Escape' })
    await waitFor(() => {
      expect(
        screen.queryByRole('dialog', { name: 'Details for Detail Movie' }),
      ).not.toBeInTheDocument()
    })
    expect(input).toBeInTheDocument()
    expect(input).toHaveValue('detail')
    expect(card).toHaveFocus()

    fireEvent.keyDown(card, { key: 'Escape' })
    await waitFor(() => {
      expect(input).not.toBeInTheDocument()
      expect(trigger).toHaveFocus()
    })
  })

  it('returns to the search input if a refreshed result removes the opening poster', async () => {
    setSearch({ data: { results: [DETAIL_MOVIE] } })
    const view = render(<SearchOverlay />)
    openOverlay()
    const input = await enterSearch('detail')
    fireEvent.click(screen.getByRole('button', { name: /View details for Detail Movie/ }))

    setSearch({ data: { results: [] } })
    view.rerender(<SearchOverlay />)
    fireEvent.click(screen.getByRole('button', { name: 'Close details' }))

    await waitFor(() => {
      expect(
        screen.queryByRole('dialog', { name: 'Details for Detail Movie' }),
      ).not.toBeInTheDocument()
      expect(input).toHaveFocus()
    })
  })
})
