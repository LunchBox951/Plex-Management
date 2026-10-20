import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useCreateRequest } from '../api/hooks'
import type { DiscoverResult, RequestResponse } from '../api/types'
import { QuickRequestButton } from './QuickRequestButton'
import type { TvRequestPickerProps } from './TvRequestPicker'

vi.mock('../api/hooks', () => ({ useCreateRequest: vi.fn() }))

const toastState = vi.hoisted(() => ({ toast: vi.fn() }))
vi.mock('./ui/toast', () => ({ useToast: () => ({ toast: toastState.toast }) }))

// The sheet itself is covered by TvRequestPicker.test.tsx; here it is a stand-in
// that reports how it was opened and can finish a request like the real one.
vi.mock('./TvRequestPicker', () => ({
  TvRequestPicker: (props: TvRequestPickerProps) =>
    props.open ? (
      <div data-testid="tv-request-picker" data-tmdb-id={props.tmdbId} data-title={props.title}>
        <button
          type="button"
          onClick={() => {
            props.onOpenChange(false)
            props.onRequested?.(CREATED, { tmdb_id: props.tmdbId, media_type: 'tv' })
          }}
        >
          Picker: request
        </button>
      </div>
    ) : null,
}))

const CREATED: RequestResponse = {
  id: 1,
  tmdb_id: 7,
  media_type: 'tv',
  title: 'Show',
  status: 'pending',
  is_anime: false,
  keep_forever: false,
  can_mutate: true,
  is_owner: true,
  can_withdraw: false,
  has_other_participants: false,
}

const MOVIE: DiscoverResult = { media_type: 'movie', tmdb_id: 5, title: 'Film', library_state: 'none' }
const SHOW: DiscoverResult = { media_type: 'tv', tmdb_id: 7, title: 'Show', library_state: 'none' }

let createMock: { mutateAsync: Mock; isPending: boolean }

beforeEach(() => {
  vi.clearAllMocks()
  createMock = { mutateAsync: vi.fn().mockResolvedValue(CREATED), isPending: false }
  ;(useCreateRequest as unknown as Mock).mockReturnValue(createMock)
})

describe('QuickRequestButton', () => {
  it('requests a movie at once', async () => {
    render(<QuickRequestButton item={MOVIE} />)
    fireEvent.click(screen.getByRole('button', { name: 'Request Film' }))

    await waitFor(() =>
      expect(createMock.mutateAsync).toHaveBeenCalledWith({ tmdb_id: 5, media_type: 'movie' }),
    )
    expect(screen.queryByTestId('tv-request-picker')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(toastState.toast).toHaveBeenCalledWith({ title: 'Requested Film', intent: 'success' }),
    )
  })

  it('opens the TV request sheet for a show instead of requesting it', () => {
    render(<QuickRequestButton item={SHOW} />)
    fireEvent.click(screen.getByRole('button', { name: 'Request Show' }))

    const picker = screen.getByTestId('tv-request-picker')
    expect(picker).toHaveAttribute('data-tmdb-id', '7')
    expect(picker).toHaveAttribute('data-title', 'Show')
    expect(createMock.mutateAsync).not.toHaveBeenCalled()
  })

  it('hides itself once the sheet has requested the show', () => {
    render(<QuickRequestButton item={SHOW} />)
    fireEvent.click(screen.getByRole('button', { name: 'Request Show' }))
    fireEvent.click(screen.getByRole('button', { name: 'Picker: request' }))

    expect(screen.queryByRole('button', { name: 'Request Show' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('tv-request-picker')).not.toBeInTheDocument()
  })
})
