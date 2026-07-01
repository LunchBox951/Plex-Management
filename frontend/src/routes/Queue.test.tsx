import { render, screen } from '@testing-library/react'
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

describe('Queue — tv season/episode badge', () => {
  beforeEach(() => {
    h.queue = []
    h.markFailed.mockReset()
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

    expect(screen.getByRole('button', { name: /^mark failed$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /blocklist & fail/i })).toBeInTheDocument()
  })
})
