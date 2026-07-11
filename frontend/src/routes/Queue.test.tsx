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
  relocateDownload: vi.fn(),
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
  useRelocateDownload: () => ({ mutateAsync: h.relocateDownload, isPending: false }),
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
    h.relocateDownload.mockReset()
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

  it('does not collapse non-contiguous episodes into a range', () => {
    h.queue = [queueItem({ season: 2, episodes: [6, 4] })]

    render(<Queue />)

    expect(screen.getByText('S02E04E06')).toBeInTheDocument()
  })

  it('shows "S02 pack" for a whole-season grab (no episodes named)', () => {
    h.queue = [queueItem({ season: 2, episodes: null })]

    render(<Queue />)

    expect(screen.getByText('S02 pack')).toBeInTheDocument()
  })

  it('shows every attached scope for a shared tv torrent', () => {
    h.queue = [
      queueItem({
        season: 1,
        episodes: null,
        scopes: [
          { media_request_id: 7, season: 1, episodes: null, status: 'active' },
          { media_request_id: 7, season: 2, episodes: [4, 5], status: 'active' },
        ],
      }),
    ]

    render(<Queue />)

    expect(screen.getByText('S01 pack')).toBeInTheDocument()
    expect(screen.getByText('S02E04-E05')).toBeInTheDocument()
  })

  it('labels non-active attached scope statuses', () => {
    h.queue = [
      queueItem({
        season: 1,
        episodes: null,
        scopes: [
          { media_request_id: 7, season: 1, episodes: null, status: 'imported' },
          { media_request_id: 7, season: 2, episodes: [4, 5], status: 'import_blocked' },
        ],
      }),
    ]

    render(<Queue />)

    expect(screen.getByText('S01 pack · Imported')).toBeInTheDocument()
    expect(screen.getByText('S02E04-E05 · Import blocked')).toBeInTheDocument()
  })
})

describe('Queue — human-legible identity (issue #134)', () => {
  beforeEach(() => {
    h.queue = []
    h.markFailed.mockReset()
    h.importDownload.mockReset()
    h.relocateDownload.mockReset()
    h.toast.mockReset()
  })

  it('shows the media title as the heading, with release_title as a secondary line', () => {
    h.queue = [
      queueItem({
        title: 'Some Movie',
        release_title: 'Some.Movie.2020.1080p.WEB-DL.x264-GROUP',
        poster_url: null,
      }),
    ]

    render(<Queue />)

    expect(screen.getByText('Some Movie')).toBeInTheDocument()
    expect(screen.getByText('Some.Movie.2020.1080p.WEB-DL.x264-GROUP')).toBeInTheDocument()
  })

  it('falls back to release_title as the heading when title is absent, without repeating it', () => {
    h.queue = [
      queueItem({
        title: null,
        release_title: 'Some.Movie.2020.1080p.WEB-DL.x264-GROUP',
        poster_url: null,
      }),
    ]

    render(<Queue />)

    expect(
      screen.getAllByText('Some.Movie.2020.1080p.WEB-DL.x264-GROUP'),
    ).toHaveLength(1)
  })

  it('falls back to a short hash heading when title and release_title are both absent (orphan row)', () => {
    h.queue = [
      queueItem({
        title: null,
        release_title: null,
        poster_url: null,
        torrent_hash: 'abc123def4567890',
      }),
    ]

    const { container } = render(<Queue />)

    // Still renders — honesty over silence — with the short hash as the heading.
    const heading = container.querySelector('p.font-display')
    expect(heading).toHaveTextContent('abc123def456')
  })

  it('renders the poster image when poster_url is present', () => {
    h.queue = [
      queueItem({ title: 'Some Movie', poster_url: 'https://image.tmdb.org/poster.jpg' }),
    ]

    const { container } = render(<Queue />)

    const img = container.querySelector('img')
    expect(img).toHaveAttribute('src', 'https://image.tmdb.org/poster.jpg')
  })

  it('renders a placeholder (no img) when poster_url is absent', () => {
    h.queue = [queueItem({ title: 'Some Movie', poster_url: null })]

    const { container } = render(<Queue />)

    expect(container.querySelector('img')).not.toBeInTheDocument()
  })
})

describe('Queue actions', () => {
  beforeEach(() => {
    h.queue = []
    h.markFailed.mockReset()
    h.importDownload.mockReset()
    h.relocateDownload.mockReset()
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
    h.relocateDownload.mockReset()
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

describe('Queue — Relocate & retry (path-not-visible import_blocked rows, issues #133/#157)', () => {
  beforeEach(() => {
    h.queue = []
    h.markFailed.mockReset()
    h.importDownload.mockReset()
    h.relocateDownload.mockReset()
    h.toast.mockReset()
  })

  it('shows Relocate & retry for an import_blocked row with the path-not-visible reason, and calls it', () => {
    h.relocateDownload.mockResolvedValue(queueItem({ id: 11, status: 'import_blocked' }))
    h.queue = [
      queueItem({
        id: 11,
        status: 'import_blocked',
        failed_reason: 'download path not visible inside the container /downloads/movie',
      }),
    ]

    render(<Queue />)

    fireEvent.click(screen.getByRole('button', { name: /relocate & retry/i }))
    expect(h.relocateDownload).toHaveBeenCalledWith(11)
    // The operator still needs to retry the import once qBittorrent settles.
    expect(screen.getByRole('button', { name: /retry import/i })).toBeInTheDocument()
  })

  it('does not show Relocate & retry for an import_blocked row with a different reason', () => {
    h.queue = [
      queueItem({
        status: 'import_blocked',
        failed_reason: 'no video file found in the completed torrent',
      }),
    ]

    render(<Queue />)

    expect(screen.queryByRole('button', { name: /relocate & retry/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry import/i })).toBeInTheDocument()
  })

  it('does not show Relocate & retry for a non-import_blocked row', () => {
    h.queue = [
      queueItem({
        status: 'downloading',
        failed_reason: null,
      }),
    ]

    render(<Queue />)

    expect(screen.queryByRole('button', { name: /relocate & retry/i })).not.toBeInTheDocument()
  })

  it('surfaces the newer reason honestly on a 409 relocation_superseded', async () => {
    const apiError: ApiError = {
      code: 'relocation_superseded',
      message:
        'The move was requested, but this row was already re-blocked with a different reason — refresh to see the current status.',
      status: 409,
    }
    h.relocateDownload.mockRejectedValue(apiError)
    h.queue = [
      queueItem({
        status: 'import_blocked',
        failed_reason: 'download path not visible inside the container /downloads/movie',
      }),
    ]

    render(<Queue />)

    fireEvent.click(screen.getByRole('button', { name: /relocate & retry/i }))
    await waitFor(() =>
      expect(h.toast).toHaveBeenCalledWith(
        expect.objectContaining({
          intent: 'error',
          description:
            'The move was requested, but this row was already re-blocked with a different reason — refresh to see the current status.',
        }),
      ),
    )
  })
})
