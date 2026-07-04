import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ApiError } from '../lib/errors'
import type { QueueItem } from '../api/types'
import { Queue } from './Queue'

// No network: the hooks are replaced with controllable stand-ins. vi.mock
// factories are hoisted above imports, so every spy/fixture they capture is
// created inside a vi.hoisted block — a plain top-level const would be out of
// scope by the time the factory runs.
const h = vi.hoisted(() => ({
  queue: [] as QueueItem[],
  markFailed: vi.fn(),
  importDownload: vi.fn(),
  toast: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useQueue: () => ({
    data: { queue: h.queue },
    isLoading: false,
    isError: false,
    error: null,
    refetch: vi.fn(),
  }),
  useMarkFailed: () => ({ mutateAsync: h.markFailed, isPending: false }),
  useImportDownload: () => ({ mutateAsync: h.importDownload, isPending: false }),
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: h.toast }),
}))

function queueItem(overrides: Partial<QueueItem> = {}): QueueItem {
  return {
    id: 1,
    media_request_id: 7,
    progress: 0.4,
    seed_ratio: 0,
    status: 'downloading',
    torrent_hash: 'abc123def4567890',
    ...overrides,
  }
}

describe('Queue — tv season/episode badge', () => {
  beforeEach(() => {
    h.queue = []
    h.markFailed.mockReset()
    h.importDownload.mockReset()
    h.toast.mockReset()
  })

  it('renders no season badge for a movie download (season is null)', () => {
    h.queue = [queueItem({ season: null, episodes: null })]

    render(<Queue />)

    expect(screen.queryByText(/^S\d{2}/)).not.toBeInTheDocument()
  })

  it('shows "S02E05" for a single-episode tv download', () => {
    h.queue = [queueItem({ season: 2, episodes: [5] })]

    render(<Queue />)

    expect(screen.getByText('S02E05')).toBeInTheDocument()
  })

  it('shows a multi-episode range for a multi-episode file', () => {
    h.queue = [queueItem({ season: 2, episodes: [5, 6] })]

    render(<Queue />)

    expect(screen.getByText('S02E05-E06')).toBeInTheDocument()
  })

  it('shows "S02 pack" for a whole-season grab (no episodes named)', () => {
    h.queue = [queueItem({ season: 2, episodes: null })]

    render(<Queue />)

    expect(screen.getByText('S02 pack')).toBeInTheDocument()
  })
})

describe('Queue actions', () => {
  beforeEach(() => {
    h.queue = []
    h.markFailed.mockReset()
    h.importDownload.mockReset()
    h.toast.mockReset()
  })

  it('hides fail actions while a download is importing', () => {
    h.queue = [queueItem({ status: 'importing' })]

    render(<Queue />)

    expect(screen.queryByRole('button', { name: /^mark failed$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /blocklist & fail/i })).not.toBeInTheDocument()
  })

  it('shows fail actions while a download is still downloading', () => {
    h.queue = [queueItem({ status: 'downloading' })]

    render(<Queue />)

    expect(screen.getByRole('progressbar', { name: /download progress/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^mark failed$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /blocklist & fail/i })).toBeInTheDocument()
  })

  it('closes a pending fail dialog when polling makes the download non-actionable', async () => {
    h.queue = [queueItem({ status: 'downloading' })]
    const view = render(<Queue />)

    fireEvent.click(screen.getByRole('button', { name: /blocklist & fail/i }))
    expect(screen.getByText(/Blocklist this release/i)).toBeInTheDocument()

    h.queue = [queueItem({ status: 'importing' })]
    view.rerender(<Queue />)

    await waitFor(() => {
      expect(screen.queryByText(/Blocklist this release/i)).not.toBeInTheDocument()
    })
    expect(h.markFailed).not.toHaveBeenCalled()
  })
})

describe('Queue — Retry import (import_blocked only)', () => {
  beforeEach(() => {
    h.queue = []
    h.markFailed.mockReset()
    h.importDownload.mockReset()
    h.toast.mockReset()
  })

  it('renders a Retry import button for an import_blocked download and retries it', () => {
    h.importDownload.mockResolvedValue(queueItem({ status: 'import_blocked' }))
    h.queue = [queueItem({ id: 7, status: 'import_blocked' })]

    render(<Queue />)

    fireEvent.click(screen.getByRole('button', { name: /retry import/i }))
    expect(h.importDownload).toHaveBeenCalledWith(7)
  })

  it('renders no Retry import button for a download in any other status', () => {
    h.queue = [queueItem({ status: 'downloading' })]

    render(<Queue />)

    expect(screen.queryByText(/retry import/i)).not.toBeInTheDocument()
  })

  it('toasts an error and does not swallow a failed retry', async () => {
    const apiError: ApiError = {
      code: 'invalid_state_transition',
      message: 'still locked',
      status: 409,
    }
    h.importDownload.mockRejectedValue(apiError)
    h.queue = [queueItem({ status: 'import_blocked' })]

    render(<Queue />)

    fireEvent.click(screen.getByRole('button', { name: /retry import/i }))
    await waitFor(() =>
      expect(h.toast).toHaveBeenCalledWith(
        expect.objectContaining({ intent: 'error', description: 'still locked' }),
      ),
    )
  })
})
