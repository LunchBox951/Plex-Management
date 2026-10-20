import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useNotifyAdmin } from '../../api/hooks'
import type { RequestResponse } from '../../api/types'
import { ProblemCard } from './ProblemCard'

vi.mock('../../api/hooks', () => ({ useNotifyAdmin: vi.fn() }))

const mutate = vi.fn()

function notifyState(overrides: Record<string, unknown> = {}) {
  vi.mocked(useNotifyAdmin).mockReturnValue({
    mutate,
    isPending: false,
    isSuccess: false,
    isError: false,
    error: null,
    ...overrides,
  } as never)
}

function blocked(overrides: Partial<RequestResponse> = {}): RequestResponse {
  return {
    id: 7,
    tmdb_id: 7,
    media_type: 'movie',
    title: 'The Pitt',
    status: 'import_blocked',
    is_anime: false,
    keep_forever: false,
    can_mutate: true,
    is_owner: true,
    can_withdraw: false,
    has_other_participants: false,
    ...overrides,
  }
}

describe('ProblemCard', () => {
  beforeEach(() => {
    mutate.mockReset()
    notifyState()
  })

  it('gives the requester plain words and Let the admin know, never fixes', () => {
    render(<ProblemCard request={blocked()} audience="family" />)
    expect(screen.getByRole('heading', { name: 'Something went wrong with the download' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Let the admin know' }))
    expect(mutate).toHaveBeenCalledWith({ requestId: 7 })
  })

  it('switches to "The admin knows" once sent', () => {
    notifyState({ isSuccess: true })
    render(<ProblemCard request={blocked()} audience="family" />)
    expect(screen.getByRole('heading', { name: 'The admin knows' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Let the admin know' })).not.toBeInTheDocument()
  })

  it('says honestly when Let the admin know is not available yet (501)', () => {
    notifyState({
      isError: true,
      error: { code: 'not_implemented', message: 'x', status: 501 },
    })
    render(<ProblemCard request={blocked()} audience="family" />)
    expect(screen.getByRole('status')).toHaveTextContent("Letting the admin know isn't available yet.")
    expect(screen.queryByRole('button', { name: 'Let the admin know' })).not.toBeInTheDocument()
  })

  it('shows the admin the reason and release, with the supplied fixes and no Let the admin know', () => {
    render(
      <ProblemCard
        request={blocked()}
        audience="admin"
        download={{
          id: 3,
          torrent_hash: 'abc',
          status: 'import_blocked',
          progress: 1,
          seed_ratio: 0,
          scopes: [],
          failed_reason: 'Files look like Season 1.',
          release_title: 'The.Pitt.S02.1080p.WEB-DL',
        }}
        actions={<button type="button">Find a different copy</button>}
      />,
    )
    expect(screen.getByText(/Files look like Season 1\./)).toBeInTheDocument()
    expect(screen.getByText('The.Pitt.S02.1080p.WEB-DL')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Find a different copy' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Let the admin know' })).not.toBeInTheDocument()
  })
})
