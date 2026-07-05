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

    expect(screen.getByRole('button', { name: /^request$/i })).toBeInTheDocument()
    expect(screen.getByText('Requested')).toBeInTheDocument() // the badge on SHOW
  })

  it('hides the action once the tile becomes badged', () => {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation())
    const { rerender } = render(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />,
    )
    expect(screen.getByRole('button', { name: /^request$/i })).toBeInTheDocument()

    rerender(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => REQUESTED} />,
    )
    expect(screen.queryByRole('button', { name: /^request$/i })).not.toBeInTheDocument()
  })

  it('hides the button immediately on a successful request', async () => {
    const createMutation = mutation({ id: 99 })
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createMutation)
    render(
      <Row title="Home row" items={[MOVIE]} onSelect={() => {}} tileState={() => null} />,
    )

    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))

    await waitFor(() => {
      expect(createMutation.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: MOVIE.tmdb_id,
        media_type: MOVIE.media_type,
      } satisfies CreateRequestBody)
    })
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /^request$/i })).not.toBeInTheDocument()
    })
    expect(toastSpy).toHaveBeenCalledWith(
      expect.objectContaining({ intent: 'success', title: expect.stringContaining('Requested') }),
    )
  })

  it('does not open the card and shows an error toast when the request fails', async () => {
    const onSelect = vi.fn()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(
      rejecting({ code: 'upstream_error', message: 'An upstream service failed. Try again shortly.' }),
    )
    render(<Row title="Home row" items={[MOVIE]} onSelect={onSelect} tileState={() => null} />)

    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))

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
    expect(screen.getByRole('button', { name: /^request$/i })).toBeInTheDocument()
    expect(onSelect).not.toHaveBeenCalled()
  })
})
