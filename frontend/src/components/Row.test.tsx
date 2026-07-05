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
  // so each tile's button is distinct for assistive tech. An exact-string match
  // selects only the button — the enclosing role="button" card folds this label
  // into its own name-from-content, so a loose /request/ regex would be ambiguous.
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
    expect(screen.getByText('Requested')).toBeInTheDocument() // the badge on SHOW
  })

  it('suppresses the Request action when requestsSettled is false, even for a null-state tile', () => {
    // The gate the Codex P2 fix adds: a `state === null` tile is only requestable
    // once the shared /requests query is fresh. `requestsSettled={false}` models the
    // stale window (query invalidated / not yet fetched) where the derived null is
    // untrustworthy — the button must not render, or a click could POST a
    // seasons-less (whole-series) tv request.
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(
      <Row
        title="Home row"
        items={[MOVIE]}
        onSelect={() => {}}
        tileState={() => null}
        requestsSettled={false}
      />,
    )
    expect(screen.queryByRole('button', { name: REQUEST_MOVIE })).not.toBeInTheDocument()
  })

  it('renders the Request action for a null-state tile once requestsSettled is true', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(
      <Row
        title="Home row"
        items={[MOVIE]}
        onSelect={() => {}}
        tileState={() => null}
        requestsSettled={true}
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
    // Focus was handed to the enclosing card (the only role="button" left in the
    // tile once the action unmounts) so a keyboard user isn't dumped to <body>.
    expect(screen.getByRole('button', { name: /Unbadged Movie/ })).toHaveFocus()
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({ intent: 'success', title: expect.stringContaining('Requested') }),
    )
  })

  it('does not open the card when the Request action is activated by keyboard', () => {
    const onSelect = vi.fn()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    render(<Row title="Home row" items={[MOVIE]} onSelect={onSelect} tileState={() => null} />)

    const action = screen.getByRole('button', { name: REQUEST_MOVIE })
    // The action lives inside PosterCard's role="button" div, whose own onKeyDown
    // opens the modal on Enter/Space. QuickRequestButton must stop that keydown from
    // bubbling; delete its guard and this keydown reaches the card and fires onSelect.
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
    // The button survives a failed request (no local "just requested" flip) and the
    // click never bubbled to the card's own onSelect.
    expect(screen.getByRole('button', { name: REQUEST_MOVIE })).toBeInTheDocument()
    expect(onSelect).not.toHaveBeenCalled()
  })
})
