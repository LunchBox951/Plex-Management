import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useCreateRequest } from '../api/hooks'
import type { CreateRequestBody, DiscoverResult } from '../api/types'
import type { StatusPresentation } from '../lib/status'
import { Row } from './Row'

// QuickRequestButton (rendered per-tile below `state === null`) is the only thing
// in this tree that touches the network or the toast provider — stand both in
// with controllable mocks, same pattern as TitleDetailModal.test.tsx.
vi.mock('../api/hooks', () => ({ useCreateRequest: vi.fn() }))

const toastSpy = vi.fn()
vi.mock('./ui/toast', () => ({ useToast: () => ({ toast: toastSpy }) }))

const REQUESTED: StatusPresentation = { label: 'Requested', intent: 'neutral' }

const MOVIE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 1,
  title: 'Unbadged Movie',
  year: 2020,
  library_state: 'none',
}

const SHOW: DiscoverResult = {
  media_type: 'tv',
  tmdb_id: 2,
  title: 'Already Requested Show',
  year: 2019,
  library_state: 'requested',
}

const SET_WIDTH = 3_320
const TRACK_SCROLL_WIDTH = SET_WIDTH * 3 - 16
const TRACK_CLIENT_WIDTH = 1_000

let measuredSetWidth = SET_WIDTH
let reducedMotion = false
let scrollBySpy: Mock
let resizeObservers: ResizeObserverMock[] = []

class ResizeObserverMock {
  readonly observe = vi.fn()
  readonly unobserve = vi.fn()
  readonly disconnect = vi.fn()

  constructor(private readonly callback: ResizeObserverCallback) {
    resizeObservers.push(this)
  }

  trigger() {
    this.callback([], this as unknown as ResizeObserver)
  }
}

function installGeometryMocks() {
  measuredSetWidth = SET_WIDTH
  reducedMotion = false
  resizeObservers = []
  scrollBySpy = vi.fn()

  Object.defineProperties(HTMLElement.prototype, {
    scrollLeft: {
      configurable: true,
      writable: true,
      value: 0,
    },
    offsetLeft: {
      configurable: true,
      get(this: HTMLElement) {
        const copy = this.getAttribute('data-loop-copy-start')
        return copy === null ? 0 : Number(copy) * measuredSetWidth
      },
    },
    scrollWidth: {
      configurable: true,
      get(this: HTMLElement) {
        return this.hasAttribute('data-row-track') ? TRACK_SCROLL_WIDTH : 0
      },
    },
    clientWidth: {
      configurable: true,
      get(this: HTMLElement) {
        return this.hasAttribute('data-row-track') ? TRACK_CLIENT_WIDTH : 0
      },
    },
    scrollBy: {
      configurable: true,
      writable: true,
      value: scrollBySpy,
    },
  })
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn(
      (query: string) =>
        ({
          matches: reducedMotion,
          media: query,
        }) as MediaQueryList,
    ),
  })
  Object.defineProperty(globalThis, 'ResizeObserver', {
    configurable: true,
    writable: true,
    value: ResizeObserverMock,
  })
}

function track(): HTMLDivElement {
  const element = document.querySelector<HTMLDivElement>('[data-row-track]')
  if (!element) throw new Error('Row track was not rendered')
  return element
}

function loopTile(copyIndex: number, slotIndex: number): HTMLElement {
  const element = document.querySelector<HTMLElement>(
    `[data-loop-copy="${copyIndex}"][data-loop-slot="${slotIndex}"]`,
  )
  if (!element) throw new Error(`Loop tile ${copyIndex}:${slotIndex} was not rendered`)
  return element
}

function item(index: number): DiscoverResult {
  return {
    media_type: index % 2 === 0 ? 'movie' : 'tv',
    tmdb_id: 100 + index,
    title: `Title ${index}`,
    year: 2000 + index,
    library_state: 'none',
  }
}

function mutation(resolved: unknown = { id: 99 }) {
  return { mutateAsync: vi.fn().mockResolvedValue(resolved), isPending: false }
}

function rejecting(error: unknown) {
  return { mutateAsync: vi.fn().mockRejectedValue(error), isPending: false }
}

beforeEach(() => {
  vi.clearAllMocks()
  installGeometryMocks()
  ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
})

describe('Row quick-request action (issue #42)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // The action's accessible name carries the title (`aria-label={`Request ${title}`}`)
  // so each tile's button is distinct for assistive tech.
  const REQUEST_MOVIE = 'Request Unbadged Movie'

  it('renders the Request action only for a tile whose state is null', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(
      <Row
        title="Home row"
        items={[MOVIE, SHOW]}
        onSelect={() => {}}
        tileState={(item) => (item.tmdb_id === MOVIE.tmdb_id ? null : REQUESTED)}
      />,
    )

    expect(within(loopTile(1, 0)).getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
    // SHOW is badged, so it gets no Request action of its own.
    expect(
      within(loopTile(1, 1)).queryByRole('button', { name: 'Request Already Requested Show' }),
    ).not.toBeInTheDocument()
    // The badge on SHOW is now a TileStatusGlyph icon (issue #135), not a text
    // pill — it still carries the same label via an accessible `role="img"` name.
    expect(within(loopTile(1, 1)).getByRole('img', { name: 'Requested' })).toBeInTheDocument()
  })

  it('suppresses the Request action when quickRequestable vetoes, even for a null-state tile', () => {
    // The gate the Codex P2 fixes add: a `state === null` tile only offers the
    // one-click Request when Discover's quickRequestable approves — the /requests
    // data is fresh AND (for tv) the title has no request rows at all. A false here
    // models the stale window (query invalidated / not yet succeeded) or a tv title
    // with settled season-scoped history, where a click would POST a seasons-less
    // (whole-series) body.
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(
      <Row
        title="Home row"
        items={[MOVIE]}
        onSelect={() => {}}
        tileState={() => null}
        quickRequestable={() => false}
      />,
    )
    expect(
      within(loopTile(1, 0)).queryByRole('button', { name: REQUEST_MOVIE }),
    ).not.toBeInTheDocument()
  })

  it('renders the Request action for a null-state tile that quickRequestable approves', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(
      <Row
        title="Home row"
        items={[MOVIE]}
        onSelect={() => {}}
        tileState={() => null}
        quickRequestable={(item) => item.tmdb_id === MOVIE.tmdb_id}
      />,
    )
    expect(within(loopTile(1, 0)).getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
  })

  it('hides the action once the tile becomes badged', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    const { rerender } = render(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />,
    )
    const movieTile = loopTile(1, 0)
    expect(within(movieTile).getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()

    rerender(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />,
    )
    expect(within(movieTile).queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
  })

  it('hides the button and returns focus to the card on a successful request', async () => {
    const createMutation = mutation({ id: 99 })
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createMutation)
    render(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />,
    )
    const movieTile = loopTile(1, 0)

    fireEvent.click(within(movieTile).getByRole('button', { name: REQUEST_MOVIE }))

    await waitFor(() => {
      expect(createMutation.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: MOVIE.tmdb_id,
        media_type: MOVIE.media_type,
      } satisfies CreateRequestBody)
    })
    await waitFor(() => {
      expect(within(movieTile).queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
    })
    // Focus was handed to the card's details trigger so a keyboard user isn't
    // dumped to <body> when the action unmounts.
    expect(
      within(movieTile).getByRole('button', { name: 'View details for Unbadged Movie (2020)' }),
    ).toHaveFocus()
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({ intent: 'success', title: expect.stringContaining('Requested') }),
    )
  })

  it('does not open the card when the Request action is activated by keyboard', () => {
    const onSelect = vi.fn()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(<Row title="Home row" items={[MOVIE]} onSelect={onSelect} tileState={() => null} />)

    const action = within(loopTile(1, 0)).getByRole('button', { name: REQUEST_MOVIE })
    // The action is a DOM sibling of the card's details trigger, not nested inside
    // it, so there's no shared handler for a keyboard activation to reach —
    // activating the action must not open details.
    fireEvent.keyDown(action, { key: 'Enter' })
    fireEvent.keyDown(action, { key: ' ' })

    expect(onSelect).not.toHaveBeenCalled()
  })

  it('does not open the card and shows an error toast when the request fails', async () => {
    const onSelect = vi.fn()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(
      rejecting({ code: 'upstream_error', message: 'An upstream service failed. Try again shortly.' }),
    )
    render(<Row title="Home row" items={[MOVIE]} onSelect={onSelect} tileState={() => null} />)
    const movieTile = loopTile(1, 0)

    fireEvent.click(within(movieTile).getByRole('button', { name: REQUEST_MOVIE }))

    await waitFor(() => {
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({
          intent: 'error',
          description: 'An upstream service failed. Try again shortly.',
        }),
      )
    })
    // The button survives a failed request (no local "just requested" flip), and
    // clicking it — a DOM sibling of the card's details trigger, not nested inside
    // it — never fires the card's own onSelect.
    expect(within(movieTile).getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
    expect(onSelect).not.toHaveBeenCalled()
  })
})

describe('Row quick-request hover-reveal affordance (issue #135)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  const REQUEST_MOVIE = 'Request Unbadged Movie'

  it('renders a circular icon button with no visible "Request" text', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(<Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />)

    const button = within(loopTile(1, 0)).getByRole('button', { name: REQUEST_MOVIE })
    // The old text pill ("Request") is gone — the accessible name now lives
    // entirely in aria-label, not in rendered text content.
    expect(button).toHaveTextContent('')
  })

  it('stays in the tab order (not `hidden`/`display:none`) while revealed only via opacity', () => {
    // Solution-1 (issue #135) must not regress keyboard reachability: hiding
    // the action with `hidden` or `display:none` would drop it from the tab
    // order entirely. Revealing it via opacity keeps it focusable at all times.
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(<Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />)

    const button = within(loopTile(1, 0)).getByRole('button', { name: REQUEST_MOVIE })
    expect(button).not.toHaveAttribute('hidden')
    expect(button.tabIndex).toBe(0)
    expect(button).toBeVisible() // jsdom's visibility check: opacity-0 still passes, `hidden` would not
    // The reveal is opacity-driven, gated on hover OR focus-within — never a
    // `display`/`visibility` toggle that would also strip it from the tab order.
    expect(button.className).toContain('opacity-0')
    expect(button.className).toContain('group-hover:opacity-100')
    expect(button.className).toContain('group-focus-within:opacity-100')
    // While invisible the button must not be hit-testable: opacity alone
    // leaves it catching taps on touch devices (no hover reveal) — an
    // invisible tap target that silently submits a request. Pointer events
    // come back with the reveal; keyboard focus/activation ignore
    // pointer-events, so the tab-order assertions above still hold.
    expect(button.className).toContain('pointer-events-none')
    expect(button.className).toContain('group-hover:pointer-events-auto')
    expect(button.className).toContain('group-focus-within:pointer-events-auto')
  })

  it('can still be focused directly (keyboard users can tab to the hidden-by-default action)', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(<Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />)

    const button = within(loopTile(1, 0)).getByRole('button', { name: REQUEST_MOVIE })
    button.focus()
    expect(button).toHaveFocus()
  })
})

describe('Row looping track (issue #190)', () => {
  it('pads a short source to 20 logical slots and preserves longer sources before tripling', () => {
    const shortItems = [MOVIE, SHOW, item(2)]
    const { rerender } = render(
      <Row title="Home row" items={shortItems} onSelect={() => {}} tileState={() => REQUESTED} />,
    )

    expect(document.querySelectorAll('[data-loop-copy]')).toHaveLength(60)
    for (const copyIndex of [0, 1, 2]) {
      expect(document.querySelectorAll(`[data-loop-copy="${copyIndex}"]`)).toHaveLength(20)
    }
    // Slot 4 is a padding REPEAT of SHOW (4 % 3 === 1) — its poster art is present
    // for the loop runway, but as an inert clone it exposes no interactive control.
    const paddingRepeat = loopTile(1, 4)
    expect(paddingRepeat).toHaveTextContent('Already Requested Show')
    expect(paddingRepeat).toHaveAttribute('inert')
    expect(within(paddingRepeat).queryByRole('button')).not.toBeInTheDocument()
    // The single real SHOW tile (first, un-padded occurrence) keeps its control.
    expect(
      within(loopTile(1, 1)).getByRole('button', {
        name: 'View details for Already Requested Show (2019)',
      }),
    ).toBeInTheDocument()

    const longerItems = Array.from({ length: 21 }, (_, index) => item(index))
    rerender(
      <Row title="Home row" items={longerItems} onSelect={() => {}} tileState={() => REQUESTED} />,
    )

    expect(document.querySelectorAll('[data-loop-copy]')).toHaveLength(63)
    for (const copyIndex of [0, 1, 2]) {
      expect(document.querySelectorAll(`[data-loop-copy="${copyIndex}"]`)).toHaveLength(21)
    }
    expect(
      within(loopTile(1, 20)).getByRole('button', { name: 'View details for Title 20 (2020)' }),
    ).toBeInTheDocument()
  })

  it('keeps stable keys on an ordinary rerender and callbacks receive the current source item', () => {
    const onSelect = vi.fn()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { rerender } = render(
      <Row title="Home row" items={[MOVIE, SHOW]} onSelect={onSelect} tileState={() => REQUESTED} />,
    )
    const rowTrack = track()
    rowTrack.scrollLeft = SET_WIDTH + 740

    const updatedMovie = { ...MOVIE, title: 'Updated Movie' }
    const updatedShow = { ...SHOW, title: 'Updated Show' }
    rerender(
      <Row
        title="Home row"
        items={[updatedMovie, updatedShow]}
        onSelect={onSelect}
        tileState={() => REQUESTED}
      />,
    )

    expect(rowTrack.scrollLeft).toBe(SET_WIDTH + 740)
    fireEvent.click(
      within(loopTile(1, 1)).getByRole('button', {
        name: 'View details for Updated Show (2019)',
      }),
    )
    expect(onSelect).toHaveBeenCalledWith(updatedShow)
    const keyWarnings = consoleError.mock.calls.filter(([message]) =>
      String(message).includes('unique "key"'),
    )
    expect(keyWarnings).toHaveLength(0)
    consoleError.mockRestore()
  })

  it('initializes at the marker-measured start of the middle copy and resets for new identities', () => {
    const { rerender } = render(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />,
    )
    const rowTrack = track()

    expect(rowTrack.scrollLeft).toBe(SET_WIDTH)
    expect(resizeObservers).toHaveLength(1)
    expect(resizeObservers[0]?.observe).toHaveBeenCalledWith(rowTrack)

    rowTrack.scrollLeft = SET_WIDTH + 300
    rerender(
      <Row title="Home row" items={[SHOW]} onSelect={() => {}} tileState={() => REQUESTED} />,
    )
    expect(rowTrack.scrollLeft).toBe(SET_WIDTH)
    expect(resizeObservers).toHaveLength(2)
    expect(resizeObservers[0]?.disconnect).toHaveBeenCalledOnce()
  })

  it('wraps native scrolling at either physical edge but leaves interior scrolling alone', () => {
    render(<Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />)
    const rowTrack = track()
    const maxScroll = TRACK_SCROLL_WIDTH - TRACK_CLIENT_WIDTH
    scrollBySpy.mockClear()

    rowTrack.scrollLeft = SET_WIDTH + 500
    fireEvent.scroll(rowTrack)
    expect(rowTrack.scrollLeft).toBe(SET_WIDTH + 500)

    rowTrack.scrollLeft = 4
    fireEvent.scroll(rowTrack)
    expect(rowTrack.scrollLeft).toBe(SET_WIDTH + 4)
    // A browser may emit a follow-up event while touch/trackpad momentum is
    // still settling. The content-identical jump must already be outside the
    // threshold so that event cannot translate a second time.
    fireEvent.scroll(rowTrack)
    expect(rowTrack.scrollLeft).toBe(SET_WIDTH + 4)

    rowTrack.scrollLeft = maxScroll - 4
    fireEvent.scroll(rowTrack)
    expect(rowTrack.scrollLeft).toBe(maxScroll - 4 - SET_WIDTH)
    fireEvent.scroll(rowTrack)
    expect(rowTrack.scrollLeft).toBe(maxScroll - 4 - SET_WIDTH)
    expect(scrollBySpy).not.toHaveBeenCalled()
    expect(rowTrack.className).not.toContain('scroll-smooth')
    expect(document.querySelector('[data-row-end-fade]')).toBeInTheDocument()
  })

  it('keeps both chevrons enabled and creates runway before each 600px scroll', () => {
    render(<Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />)
    const rowTrack = track()
    const left = screen.getByRole('button', { name: 'Scroll left' })
    const right = screen.getByRole('button', { name: 'Scroll right' })
    const maxScroll = TRACK_SCROLL_WIDTH - TRACK_CLIENT_WIDTH

    expect(left).toBeEnabled()
    expect(right).toBeEnabled()

    rowTrack.scrollLeft = 604
    fireEvent.click(left)
    expect(rowTrack.scrollLeft).toBe(604 + SET_WIDTH)
    expect(scrollBySpy).toHaveBeenLastCalledWith({ left: -600, behavior: 'smooth' })

    scrollBySpy.mockClear()
    rowTrack.scrollLeft = maxScroll - 604
    fireEvent.click(right)
    expect(rowTrack.scrollLeft).toBe(maxScroll - 604 - SET_WIDTH)
    expect(scrollBySpy).toHaveBeenLastCalledWith({ left: 600, behavior: 'smooth' })
  })

  it('uses smooth chevron motion normally and auto behavior for reduced motion', () => {
    render(<Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />)
    const rowTrack = track()
    rowTrack.scrollLeft = SET_WIDTH

    fireEvent.click(screen.getByRole('button', { name: 'Scroll right' }))
    expect(scrollBySpy).toHaveBeenLastCalledWith({ left: 600, behavior: 'smooth' })

    reducedMotion = true
    fireEvent.click(screen.getByRole('button', { name: 'Scroll left' }))
    expect(scrollBySpy).toHaveBeenLastCalledWith({ left: -600, behavior: 'auto' })
  })

  it('preserves proportional position when markers resize, then normalizes away from an edge', () => {
    render(<Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />)
    const rowTrack = track()
    const observer = resizeObservers[0]
    if (!observer) throw new Error('ResizeObserver was not created')

    rowTrack.scrollLeft = SET_WIDTH * 1.5
    measuredSetWidth = 4_000
    observer.trigger()
    expect(rowTrack.scrollLeft).toBe(6_000)

    rowTrack.scrollLeft = 4
    measuredSetWidth = 5_000
    observer.trigger()
    expect(rowTrack.scrollLeft).toBe(5_005)
  })

  it('disconnects its observer and removes the native scroll listener on unmount', () => {
    const { unmount } = render(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />,
    )
    const rowTrack = track()
    const removeEventListener = vi.spyOn(rowTrack, 'removeEventListener')
    const observer = resizeObservers[0]
    if (!observer) throw new Error('ResizeObserver was not created')

    unmount()

    expect(observer.disconnect).toHaveBeenCalledOnce()
    expect(removeEventListener).toHaveBeenCalledWith('scroll', expect.any(Function))
  })

  it('keeps the eight-skeleton loading branch control-free and returns null when empty', () => {
    const { container, rerender } = render(
      <Row title="Home row" items={[]} loading onSelect={() => {}} />,
    )

    expect(container.querySelectorAll('.animate-pulse')).toHaveLength(8)
    expect(screen.queryByRole('button', { name: 'Scroll left' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Scroll right' })).not.toBeInTheDocument()
    expect(document.querySelector('[data-row-end-fade]')).not.toBeInTheDocument()

    rerender(<Row title="Home row" items={[]} onSelect={() => {}} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('Row loop-clone inertness and screen-reader honesty (issue #190)', () => {
  const REQUEST_MOVIE = 'Request Unbadged Movie'

  it('leaves only the middle copy of an already-long source in the tab order and a11y tree', () => {
    // 21 unique items => no padding; each copy is a faithful 21-tile mirror.
    const longItems = Array.from({ length: 21 }, (_, index) => item(index))
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(<Row title="Home row" items={longItems} onSelect={() => {}} tileState={() => null} />)

    // 63 tiles are rendered for the visual loop, but exactly the 21 real ones (the
    // middle copy) are non-inert and in the accessibility tree.
    expect(document.querySelectorAll('[data-loop-copy]')).toHaveLength(63)
    expect(document.querySelectorAll('[data-loop-real]')).toHaveLength(21)
    for (const copyIndex of [0, 2]) {
      for (const el of document.querySelectorAll(`[data-loop-copy="${copyIndex}"]`)) {
        expect(el).toHaveAttribute('inert')
        expect(el).toHaveAttribute('aria-hidden', 'true')
      }
    }
    for (const el of document.querySelectorAll('[data-loop-copy="1"]')) {
      expect(el).not.toHaveAttribute('inert')
      expect(el).not.toHaveAttribute('aria-hidden')
    }

    // Screen-reader honesty: one details button + one request action per real item,
    // never one-per-clone. getAllByRole ignores inert/aria-hidden subtrees.
    expect(screen.getAllByRole('button', { name: /^View details for/ })).toHaveLength(21)
    expect(screen.getAllByRole('button', { name: /^Request / })).toHaveLength(21)
  })

  it('announces a padded short source by its real item count, not the padded/cloned count', () => {
    // 3 unique items padded to 20 logical slots, tripled to 60 tiles — yet a screen
    // reader must hear three titles, once each.
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(
      <Row
        title="Home row"
        items={[MOVIE, SHOW, item(2)]}
        onSelect={() => {}}
        tileState={() => null}
      />,
    )

    expect(document.querySelectorAll('[data-loop-copy]')).toHaveLength(60)
    expect(document.querySelectorAll('[data-loop-real]')).toHaveLength(3)
    // Exactly the three unique titles are reachable as details triggers.
    const detailNames = screen
      .getAllByRole('button', { name: /^View details for/ })
      .map((el) => el.getAttribute('aria-label'))
    expect(detailNames).toHaveLength(3)
    expect(new Set(detailNames).size).toBe(3)
    // And exactly the three request actions, one per real unbadged tile.
    expect(screen.getAllByRole('button', { name: /^Request / })).toHaveLength(3)
  })

  it('offers the request action only from a real tile, and it targets the real source item', () => {
    const createMutation = mutation({ id: 1 })
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createMutation)
    render(<Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />)

    // The padded clone at copy 1, slot 1 carries no request control at all.
    expect(within(loopTile(1, 1)).queryByRole('button')).not.toBeInTheDocument()
    expect(loopTile(1, 1)).toHaveAttribute('inert')

    // The lone reachable request action belongs to the real tile and POSTs the
    // real item's identity — clones can never source a request.
    const action = screen.getByRole('button', { name: REQUEST_MOVIE })
    expect(action.closest('[data-loop-copy]')).toBe(loopTile(1, 0))
    fireEvent.click(action)
    expect(createMutation.mutateAsync).toHaveBeenCalledWith({
      tmdb_id: MOVIE.tmdb_id,
      media_type: MOVIE.media_type,
    } satisfies CreateRequestBody)
  })

  it('pads deterministically by modular repetition (no randomness, stable across renders)', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    const source = [MOVIE, SHOW, item(2)]
    const readSlotTitles = () =>
      Array.from({ length: 20 }, (_, slotIndex) =>
        loopTile(1, slotIndex).querySelector('.font-display')?.textContent,
      )

    const { rerender } = render(
      <Row title="Home row" items={source} onSelect={() => {}} tileState={() => REQUESTED} />,
    )
    const firstPass = readSlotTitles()
    // Each padded slot is source[slot % source.length] — pure, index-driven.
    for (let slotIndex = 0; slotIndex < 20; slotIndex += 1) {
      expect(firstPass[slotIndex]).toBe(source[slotIndex % source.length]!.title)
    }

    // Re-rendering the same source yields byte-identical padding (no Math.random).
    rerender(
      <Row title="Home row" items={source} onSelect={() => {}} tileState={() => REQUESTED} />,
    )
    expect(readSlotTitles()).toEqual(firstPass)
  })
})
