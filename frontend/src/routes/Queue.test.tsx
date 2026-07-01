import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { QueueItem } from '../api/types'
import { Queue } from './Queue'

const h = vi.hoisted(() => ({
  queue: [] as QueueItem[],
  markFailed: vi.fn(),
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
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: vi.fn() }),
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

describe('Queue actions', () => {
  beforeEach(() => {
    h.queue = []
    h.markFailed.mockReset()
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
