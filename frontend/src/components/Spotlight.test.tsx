import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useCreateRequest } from '../api/hooks'
import type { DiscoverResult, RequestResponse } from '../api/types'
import { requestStatus, type StatusPresentation } from '../lib/status'
import { Spotlight } from './Spotlight'
import { useToast } from './ui/toast'

vi.mock('../api/hooks', () => ({ useCreateRequest: vi.fn() }))
vi.mock('./ui/toast', () => ({ useToast: vi.fn() }))

const MOVIE: DiscoverResult = {
  tmdb_id: 1,
  media_type: 'movie',
  title: 'First Feature',
  year: 2024,
  overview: 'The first featured title.',
  backdrop_url: 'https://image/first.jpg',
  library_state: 'none',
}

const SHOW: DiscoverResult = {
  tmdb_id: 2,
  media_type: 'tv',
  title: 'Second Feature',
  year: 2025,
  overview: 'The second featured title.',
  backdrop_url: 'https://image/second.jpg',
  library_state: 'none',
}

const THIRD: DiscoverResult = {
  tmdb_id: 3,
  media_type: 'movie',
  title: 'Third Feature',
  year: 2026,
  backdrop_url: 'https://image/third.jpg',
  library_state: 'none',
}

interface MotionMock {
  setMatches: (matches: boolean) => void
}

function mockMotion(initial = false): MotionMock {
  let matches = initial
  type Listener = (event: MediaQueryListEvent) => void
  const listeners = new Set<Listener>()
  const query = {
    get matches() {
      return matches
    },
    media: '(prefers-reduced-motion: reduce)',
    onchange: null,
    addEventListener: (_type: string, listener: Listener) => listeners.add(listener),
    removeEventListener: (_type: string, listener: Listener) => listeners.delete(listener),
    addListener: (listener: Listener | null) => {
      if (listener) listeners.add(listener)
    },
    removeListener: (listener: Listener | null) => {
      if (listener) listeners.delete(listener)
    },
    dispatchEvent: () => true,
  } as unknown as MediaQueryList
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn(() => query),
  })
  return {
    setMatches(next) {
      matches = next
      for (const listener of listeners) listener({ matches } as MediaQueryListEvent)
    },
  }
}

function requestResponse(status: RequestResponse['status'] = 'pending'): RequestResponse {
  return {
    id: 91,
    tmdb_id: MOVIE.tmdb_id,
    media_type: MOVIE.media_type,
    title: MOVIE.title,
    status,
    is_anime: false,
    keep_forever: false,
  }
}

function setupMutation(overrides: Record<string, unknown> = {}) {
  const mutation = {
    mutateAsync: vi.fn().mockResolvedValue(requestResponse()),
    isPending: false,
    ...overrides,
  }
  ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation)
  return mutation
}

function renderSpotlight(
  overrides: Partial<{
    items: DiscoverResult[]
    stateFor: (item: DiscoverResult) => StatusPresentation | null
    canQuickRequest: (item: DiscoverResult) => boolean
    onOpen: (item: DiscoverResult) => void
    stateRevision: number
    paused: boolean
  }> = {},
) {
  const props = {
    items: [MOVIE, SHOW, THIRD],
    stateFor: () => null,
    canQuickRequest: () => true,
    onOpen: vi.fn(),
    paused: false,
    ...overrides,
  }
  return { ...render(<Spotlight {...props} />), props }
}

function activeTitle(): HTMLHeadingElement {
  return screen.getByRole('heading', { level: 1 })
}

beforeEach(() => {
  vi.clearAllMocks()
  mockMotion(false)
  Object.defineProperty(document, 'hidden', { configurable: true, value: false })
  setupMutation()
  ;(useToast as unknown as Mock).mockReturnValue({ toast: vi.fn() })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('Spotlight — rotation and transition lifecycle', () => {
  it('advances after 6.5 seconds and removes the outgoing slide after the 300ms fade', () => {
    vi.useFakeTimers()
    renderSpotlight()

    act(() => vi.advanceTimersByTime(6_499))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)

    act(() => vi.advanceTimersByTime(1))
    expect(activeTitle()).toHaveTextContent(SHOW.title)
    expect(screen.getByText(MOVIE.title).closest('[aria-hidden="true"]')).not.toBeNull()

    act(() => vi.advanceTimersByTime(299))
    expect(screen.getByText(MOVIE.title)).toBeInTheDocument()
    act(() => vi.advanceTimersByTime(1))
    expect(screen.queryByText(MOVIE.title)).not.toBeInTheDocument()
  })

  it('selects native pagination dots, marks the current dot, and starts a fresh dwell', () => {
    vi.useFakeTimers()
    renderSpotlight()
    const second = screen.getByRole('button', { name: `Show 2 of 3: ${SHOW.title}` })

    expect(screen.getByRole('button', { name: `Show 1 of 3: ${MOVIE.title}` })).toHaveAttribute(
      'aria-current',
      'true',
    )
    expect(second.className).toContain('focus-visible:ring-2')
    fireEvent.click(second)

    expect(activeTitle()).toHaveTextContent(SHOW.title)
    expect(second).toHaveAttribute('aria-current', 'true')
    act(() => vi.advanceTimersByTime(6_499))
    expect(activeTitle()).toHaveTextContent(SHOW.title)
    act(() => vi.advanceTimersByTime(1))
    expect(activeTitle()).toHaveTextContent(THIRD.title)
  })

  it('pauses for an external dialog and resumes with a fresh full delay', () => {
    vi.useFakeTimers()
    const view = renderSpotlight({ paused: true })

    act(() => vi.advanceTimersByTime(20_000))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)

    view.rerender(<Spotlight {...view.props} paused={false} />)
    act(() => vi.advanceTimersByTime(6_499))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)
    act(() => vi.advanceTimersByTime(1))
    expect(activeTitle()).toHaveTextContent(SHOW.title)
  })

  it('pauses while hovered and while focus is within, with a fresh delay after each leave', () => {
    vi.useFakeTimers()
    renderSpotlight()
    const carousel = screen.getByRole('region', { name: 'Featured titles' })

    fireEvent.pointerEnter(carousel)
    act(() => vi.advanceTimersByTime(7_000))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)
    fireEvent.pointerLeave(carousel)
    act(() => vi.advanceTimersByTime(6_500))
    expect(activeTitle()).toHaveTextContent(SHOW.title)

    act(() => vi.advanceTimersByTime(300))
    const details = screen.getByRole('button', { name: 'Details' })
    fireEvent.focus(details)
    act(() => vi.advanceTimersByTime(7_000))
    expect(activeTitle()).toHaveTextContent(SHOW.title)
    fireEvent.blur(details, { relatedTarget: document.body })
    act(() => vi.advanceTimersByTime(6_500))
    expect(activeTitle()).toHaveTextContent(THIRD.title)
  })

  it('pauses while the document is hidden and resumes from a fresh delay', () => {
    vi.useFakeTimers()
    renderSpotlight()
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    fireEvent(document, new Event('visibilitychange'))

    act(() => vi.advanceTimersByTime(9_000))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)

    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    fireEvent(document, new Event('visibilitychange'))
    act(() => vi.advanceTimersByTime(6_499))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)
    act(() => vi.advanceTimersByTime(1))
    expect(activeTitle()).toHaveTextContent(SHOW.title)
  })

  it('pauses while a request mutation is pending and resumes from a fresh delay', () => {
    vi.useFakeTimers()
    setupMutation({ isPending: true })
    const view = renderSpotlight()
    act(() => vi.advanceTimersByTime(8_000))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)

    setupMutation({ isPending: false })
    view.rerender(<Spotlight {...view.props} />)
    act(() => vi.advanceTimersByTime(6_500))
    expect(activeTitle()).toHaveTextContent(SHOW.title)
  })

  it('disables auto-rotation and fading for reduced motion but keeps manual dots', () => {
    vi.useFakeTimers()
    mockMotion(true)
    renderSpotlight()

    act(() => vi.advanceTimersByTime(20_000))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)
    expect(screen.queryByRole('button', { name: /spotlight rotation/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: `Show 2 of 3: ${SHOW.title}` }))
    expect(activeTitle()).toHaveTextContent(SHOW.title)
    expect(screen.queryByText(MOVIE.title)).not.toBeInTheDocument()
    expect(activeTitle().closest('.spotlight-fade-in')).toBeNull()
  })

  it('preserves the active media key across refreshed and reordered item objects', () => {
    vi.useFakeTimers()
    const view = renderSpotlight()
    fireEvent.click(screen.getByRole('button', { name: `Show 2 of 3: ${SHOW.title}` }))
    act(() => vi.advanceTimersByTime(300))

    const refreshedShow = { ...SHOW, title: 'Second Feature — refreshed' }
    view.rerender(<Spotlight {...view.props} items={[THIRD, refreshedShow, MOVIE]} />)

    expect(activeTitle()).toHaveTextContent(refreshedShow.title)
    expect(
      screen.getByRole('button', { name: `Show 2 of 3: ${refreshedShow.title}` }),
    ).toHaveAttribute('aria-current', 'true')
  })

  it('offers a persistent Pause/Play control and clears every timer on unmount', () => {
    vi.useFakeTimers()
    const view = renderSpotlight()
    const pause = screen.getByRole('button', { name: 'Pause spotlight rotation' })

    fireEvent.click(pause)
    expect(screen.getByRole('button', { name: 'Play spotlight rotation' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    act(() => vi.advanceTimersByTime(8_000))
    expect(activeTitle()).toHaveTextContent(MOVIE.title)

    fireEvent.click(screen.getByRole('button', { name: 'Play spotlight rotation' }))
    act(() => vi.advanceTimersByTime(6_500))
    expect(activeTitle()).toHaveTextContent(SHOW.title)
    expect(vi.getTimerCount()).toBeGreaterThan(0)

    view.unmount()
    expect(vi.getTimerCount()).toBe(0)
  })
})

describe('Spotlight — honest CTA behavior', () => {
  it('posts the exact safe payload, blocks a double-submit, and swaps immediately to returned status', async () => {
    let resolveRequest: ((value: RequestResponse) => void) | undefined
    const pending = new Promise<RequestResponse>((resolve) => {
      resolveRequest = resolve
    })
    const mutation = setupMutation({ mutateAsync: vi.fn(() => pending), isPending: false })
    const { props, rerender } = renderSpotlight({ items: [MOVIE] })

    fireEvent.click(screen.getByRole('button', { name: '+ Request' }))
    expect(mutation.mutateAsync).toHaveBeenCalledWith({ tmdb_id: 1, media_type: 'movie' })

    mutation.isPending = true
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation)
    rerender(<Spotlight {...props} />)
    const loading = screen.getByRole('button', { name: '+ Request' })
    expect(loading).toBeDisabled()
    fireEvent.click(loading)
    expect(mutation.mutateAsync).toHaveBeenCalledTimes(1)

    await act(async () => resolveRequest?.(requestResponse('searching')))
    expect(screen.getByText('Searching')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '+ Request' })).not.toBeInTheDocument()
    expect((useToast as unknown as Mock).mock.results[0]?.value.toast).toHaveBeenCalledWith({
      title: `Requested ${MOVIE.title}`,
      intent: 'success',
    })
  })

  it('opens Details instead of posting when a TV shortcut is scope-unsafe', () => {
    const mutation = setupMutation()
    const onOpen = vi.fn()
    renderSpotlight({ items: [SHOW], canQuickRequest: () => false, onOpen })

    fireEvent.click(screen.getByRole('button', { name: '+ Request' }))

    expect(mutation.mutateAsync).not.toHaveBeenCalled()
    expect(onOpen).toHaveBeenCalledWith(SHOW)
  })

  it('retires the returned-status bridge after a settled refetch even when state is null', async () => {
    setupMutation({
      mutateAsync: vi.fn().mockResolvedValue(requestResponse('searching')),
    })
    const view = renderSpotlight({ items: [MOVIE], stateRevision: 100 })

    fireEvent.click(screen.getByRole('button', { name: '+ Request' }))
    await waitFor(() => expect(screen.getByText('Searching')).toBeInTheDocument())

    view.rerender(
      <Spotlight
        {...view.props}
        stateRevision={200}
        stateFor={() => null}
        canQuickRequest={() => false}
      />,
    )
    expect(screen.getByRole('button', { name: '+ Request' })).toBeInTheDocument()
  })

  it('toasts an honest request failure and leaves the CTA retryable', async () => {
    const error = { message: 'TMDB could not resolve that title.' }
    setupMutation({ mutateAsync: vi.fn().mockRejectedValue(error) })
    renderSpotlight({ items: [MOVIE] })

    fireEvent.click(screen.getByRole('button', { name: '+ Request' }))

    await waitFor(() => {
      expect((useToast as unknown as Mock).mock.results[0]?.value.toast).toHaveBeenCalledWith({
        title: 'Request failed',
        description: error.message,
        intent: 'error',
      })
    })
    expect(screen.getByRole('button', { name: '+ Request' })).toBeEnabled()
  })

  it.each([
    requestStatus('pending'),
    requestStatus('searching'),
    requestStatus('downloading'),
    requestStatus('no_acceptable_release'),
    requestStatus('waiting_for_air_date'),
    requestStatus('import_blocked'),
    requestStatus('completed'),
    { label: 'Unknown pipeline state', intent: 'neutral' } satisfies StatusPresentation,
  ])('renders non-available state "$label" as a status chip, never a dead button', (state) => {
    renderSpotlight({ items: [MOVIE], stateFor: () => state })

    expect(screen.getByText(state.label)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: state.label })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Details' })).toBeInTheDocument()
  })

  it.each([requestStatus('available'), requestStatus('partially_available')])(
    'renders "$label" as the safe hosted Plex link plus Details',
    (state) => {
      renderSpotlight({ items: [MOVIE], stateFor: () => state })

      const plex = screen.getByRole('link', { name: /Open in Plex.*opens in a new tab/ })
      expect(plex).toHaveAttribute('href', 'https://app.plex.tv/desktop/')
      expect(plex).toHaveAttribute('target', '_blank')
      expect(plex).toHaveAttribute('rel', 'noopener noreferrer')
      expect(screen.getByRole('button', { name: 'Details' })).toBeInTheDocument()
    },
  )

  it('always opens Details for the active item', () => {
    const onOpen = vi.fn()
    renderSpotlight({ items: [SHOW], onOpen })

    fireEvent.click(screen.getByRole('button', { name: 'Details' }))
    expect(onOpen).toHaveBeenCalledWith(SHOW)
  })

  it('keeps a legible gradient fallback when artwork is missing and returns null for no items', () => {
    const missingArt = { ...MOVIE, backdrop_url: null }
    const view = renderSpotlight({ items: [missingArt] })
    expect(screen.getByTestId('spotlight-art-fallback')).toBeInTheDocument()

    view.rerender(<Spotlight {...view.props} items={[]} />)
    expect(screen.queryByRole('region', { name: 'Featured titles' })).not.toBeInTheDocument()
  })
})
