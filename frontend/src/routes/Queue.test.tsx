import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useImportDownload, useMarkFailed, useQueue } from '../api/hooks'
import type { ApiError } from '../lib/errors'
import type { QueueItem } from '../api/types'
import { Queue } from './Queue'

// No network: the hooks are replaced with controllable stand-ins so the test
// exercises only the tv season/episode badge rendering.
vi.mock('../api/hooks', () => ({
  useMarkFailed: vi.fn(),
  useImportDownload: vi.fn(),
  useQueue: vi.fn(),
}))

// vi.mock factories are hoisted above imports, so the captured `toast` spy is
// itself created inside a vi.hoisted block — a plain top-level const would be
// out of scope by the time the factory runs.
const { toast } = vi.hoisted(() => ({ toast: vi.fn() }))
vi.mock('../components/ui/toast', () => ({ useToast: () => ({ toast }) }))

function baseItem(overrides: Partial<QueueItem> = {}): QueueItem {
  return {
    id: 1,
    progress: 0,
    seed_ratio: 0,
    status: 'downloading',
    torrent_hash: 'abcdef0123456789',
    ...overrides,
  }
}

describe('Queue — tv season/episode badge', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(useMarkFailed as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
    ;(useImportDownload as unknown as Mock).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    })
  })

  it('renders no season badge for a movie download (season is null)', () => {
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [baseItem({ season: null, episodes: null })] },
      isLoading: false,
      isError: false,
    })
    render(<Queue />)
    expect(screen.queryByText(/^S\d{2}/)).not.toBeInTheDocument()
  })

  it('shows "S02E05" for a single-episode tv download', () => {
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [baseItem({ season: 2, episodes: [5] })] },
      isLoading: false,
      isError: false,
    })
    render(<Queue />)
    expect(screen.getByText('S02E05')).toBeInTheDocument()
  })

  it('shows a multi-episode range for a multi-episode file', () => {
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [baseItem({ season: 2, episodes: [5, 6] })] },
      isLoading: false,
      isError: false,
    })
    render(<Queue />)
    expect(screen.getByText('S02E05-E06')).toBeInTheDocument()
  })

  it('shows "S02 pack" for a whole-season grab (no episodes named)', () => {
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [baseItem({ season: 2, episodes: null })] },
      isLoading: false,
      isError: false,
    })
    render(<Queue />)
    expect(screen.getByText('S02 pack')).toBeInTheDocument()
  })
})

describe('Queue — Retry import (import_blocked only)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(useMarkFailed as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })
  })

  it('renders a Retry import button for an import_blocked download and retries it', () => {
    const mutateAsync = vi.fn().mockResolvedValue(baseItem({ status: 'import_blocked' }))
    ;(useImportDownload as unknown as Mock).mockReturnValue({ mutateAsync, isPending: false })
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [baseItem({ id: 7, status: 'import_blocked' })] },
      isLoading: false,
      isError: false,
    })
    render(<Queue />)
    const button = screen.getByRole('button', { name: /retry import/i })
    fireEvent.click(button)
    expect(mutateAsync).toHaveBeenCalledWith(7)
  })

  it('renders no Retry import button for a download in any other status', () => {
    ;(useImportDownload as unknown as Mock).mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    })
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [baseItem({ status: 'downloading' })] },
      isLoading: false,
      isError: false,
    })
    render(<Queue />)
    expect(screen.queryByText(/retry import/i)).not.toBeInTheDocument()
  })

  it('toasts an error and does not swallow a failed retry', async () => {
    const apiError: ApiError = { code: 'invalid_state_transition', message: 'still locked', status: 409 }
    const mutateAsync = vi.fn().mockRejectedValue(apiError)
    ;(useImportDownload as unknown as Mock).mockReturnValue({ mutateAsync, isPending: false })
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [baseItem({ status: 'import_blocked' })] },
      isLoading: false,
      isError: false,
    })
    render(<Queue />)
    fireEvent.click(screen.getByRole('button', { name: /retry import/i }))
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith(
        expect.objectContaining({ intent: 'error', description: 'still locked' }),
      ),
    )
  })
})
