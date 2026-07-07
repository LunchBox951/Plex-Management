import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

function mutation(resolved: unknown = { id: 99 }) {
  return { mutateAsync: vi.fn().mockResolvedValue(resolved), isPending: false }
}

function rejecting(error: unknown) {
  return { mutateAsync: vi.fn().mockRejectedValue(error), isPending: false }
}

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

    expect(screen.getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
    // SHOW is badged, so it gets no Request action of its own.
    expect(screen.queryByRole('button', { name: 'Request Already Requested Show' })).not.toBeInTheDocument()
    // The badge on SHOW is now a TileStatusGlyph icon (issue #135), not a text
    // pill — it still carries the same label via an accessible `role="img"` name.
    expect(screen.getByRole('img', { name: 'Requested' })).toBeInTheDocument()
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
    expect(screen.queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
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
    expect(screen.getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
  })

  it('hides the action once the tile becomes badged', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    const { rerender } = render(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />,
    )
    expect(screen.getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()

    rerender(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />,
    )
    expect(screen.queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
  })

  it('hides the button and returns focus to the card on a successful request', async () => {
    const createMutation = mutation({ id: 99 })
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createMutation)
    render(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />,
    )

    fireEvent.click(screen.getByRole('button', { name: REQUEST_MOVIE }))

    await waitFor(() => {
      expect(createMutation.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: MOVIE.tmdb_id,
        media_type: MOVIE.media_type,
      } satisfies CreateRequestBody)
    })
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
    })
    // Focus was handed to the card's details trigger so a keyboard user isn't
    // dumped to <body> when the action unmounts.
    expect(
      screen.getByRole('button', { name: 'View details for Unbadged Movie (2020)' }),
    ).toHaveFocus()
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({ intent: 'success', title: expect.stringContaining('Requested') }),
    )
  })

  it('does not open the card when the Request action is activated by keyboard', () => {
    const onSelect = vi.fn()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(<Row title="Home row" items={[MOVIE]} onSelect={onSelect} tileState={() => null} />)

    const action = screen.getByRole('button', { name: REQUEST_MOVIE })
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

    fireEvent.click(screen.getByRole('button', { name: REQUEST_MOVIE }))

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
    expect(screen.getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
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

    const button = screen.getByRole('button', { name: REQUEST_MOVIE })
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

    const button = screen.getByRole('button', { name: REQUEST_MOVIE })
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

    const button = screen.getByRole('button', { name: REQUEST_MOVIE })
    button.focus()
    expect(button).toHaveFocus()
  })
})
