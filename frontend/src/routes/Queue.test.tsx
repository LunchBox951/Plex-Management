import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useMarkFailed, useQueue } from '../api/hooks'
import type { QueueItem } from '../api/types'
import { Queue } from './Queue'

// No network: the hooks are replaced with controllable stand-ins so the test
// exercises only the tv season/episode badge rendering.
vi.mock('../api/hooks', () => ({
  useMarkFailed: vi.fn(),
  useQueue: vi.fn(),
}))

vi.mock('../components/ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }))

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
