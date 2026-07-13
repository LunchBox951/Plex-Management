import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  hasData: true,
  isLoading: false,
  isError: false,
  error: new Error('queue unavailable'),
  refetch: vi.fn(),
  markFailed: vi.fn(),
  importDownload: vi.fn(),
  relocateDownload: vi.fn(),
  toast: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useQueue: () => ({
    data: h.hasData ? { queue: h.queue } : undefined,
    isLoading: h.isLoading,
    isError: h.isError,
    error: h.error,
    refetch: h.refetch,
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

beforeEach(() => {
  h.queue = []
  h.hasData = true
  h.isLoading = false
  h.isError = false
  h.error = new Error('queue unavailable')
  h.refetch.mockReset()
  h.markFailed.mockReset()
  h.importDownload.mockReset()
  h.relocateDownload.mockReset()
  h.toast.mockReset()
})

describe('Queue — admin header and query states', () => {
  it('renders the shared header, resolved active count, description, and healthy poll hint', () => {
    h.queue = [
      queueItem({ id: 1, title: 'Moving', status: 'downloading' }),
      queueItem({ id: 2, title: 'Blocked', status: 'import_blocked' }),
    ]

    render(<Queue />)

    expect(screen.getByRole('heading', { name: 'Queue' })).toBeInTheDocument()
    expect(screen.getByText('1 active')).toBeInTheDocument()
    expect(
      screen.getByText('Everything the download client is holding, including blocked imports.'),
    ).toBeInTheDocument()
    const pollStatus = screen.getByRole('status')
    expect(pollStatus).toHaveTextContent('updating every 2s')
    expect(pollStatus.querySelector('[aria-hidden="true"]')).toHaveClass(
      'motion-safe:animate-pulse',
      'bg-downloading',
    )
  })

  it('keeps loading ahead of a first-load error', () => {
    h.hasData = false
    h.isLoading = true
    h.isError = true

    render(<Queue />)

    expect(screen.getByText('Loading queue')).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load the queue")).not.toBeInTheDocument()
    expect(screen.queryByText(/active$/)).not.toBeInTheDocument()
  })

  it('renders an actionable first-load error when no queue data exists', () => {
    h.hasData = false
    h.isError = true
    h.error = new Error('download client unavailable')

    render(<Queue />)

    expect(screen.getByText("Couldn't load the queue")).toBeInTheDocument()
    expect(screen.getByText('download client unavailable')).toBeInTheDocument()
    const pollStatus = screen.getByRole('status')
    expect(pollStatus).toHaveTextContent('reconnecting…')
    expect(pollStatus.querySelector('[aria-hidden="true"]')).toHaveClass('bg-error')
    expect(pollStatus.querySelector('[aria-hidden="true"]')).not.toHaveClass(
      'motion-safe:animate-pulse',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(h.refetch).toHaveBeenCalledOnce()
  })

  it('retains cached rows during a failed background poll', () => {
    h.queue = [queueItem({ title: 'Cached title' })]
    h.isError = true

    render(<Queue />)

    expect(screen.getByText('Cached title')).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load the queue")).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('reconnecting…')
  })

  it('retains a cached empty result during a failed background poll', () => {
    h.isError = true

    render(<Queue />)

    expect(screen.getByText('0 active')).toBeInTheDocument()
    expect(screen.getByText('Nothing downloading')).toBeInTheDocument()
    expect(
      screen.getByText("Grab a release from a title's detail to see it here."),
    ).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load the queue")).not.toBeInTheDocument()
    expect(screen.getAllByRole('status')[0]).toHaveTextContent('reconnecting…')
  })
})

describe('Queue — truthful transfer progress', () => {
  it('renders one named progressbar and one matching rounded percentage for downloading', () => {
    h.queue = [queueItem({ title: 'Fractional title', progress: 0.456 })]

    render(<Queue />)

    const progress = screen.getByRole('progressbar', {
      name: 'Fractional title download progress',
    })
    expect(progress).toHaveAttribute('aria-valuenow', '46')
    expect(progress).toHaveClass('h-[5px]', 'max-w-[420px]')
    expect(screen.getByText('46%')).toHaveClass('tabular-nums')
  })

  it.each([
    { value: -0.5, expected: 0 },
    { value: 1.5, expected: 100 },
  ])('clamps a $value transfer fraction to $expected%', ({ value, expected }) => {
    h.queue = [queueItem({ title: 'Clamp title', progress: value })]

    render(<Queue />)

    expect(
      screen.getByRole('progressbar', { name: 'Clamp title download progress' }),
    ).toHaveAttribute('aria-valuenow', String(expected))
    expect(screen.getByText(`${expected}%`)).toBeInTheDocument()
  })

  it.each([
    { status: 'metadata_fetching', label: 'Fetching metadata', activeCount: 1 },
    { status: 'import_pending', label: 'Import pending' },
    { status: 'importing', label: 'Importing' },
    { status: 'import_blocked', label: 'Import blocked', activeCount: 0 },
    { status: 'client_missing', label: 'Client missing', activeCount: 0 },
    { status: 'failed', label: 'Failed', activeCount: 0 },
  ])(
    'shows the $label badge without claiming transfer progress',
    ({ status, label, activeCount = 1 }) => {
      h.queue = [queueItem({ title: 'Import phase', status, progress: 1 })]

      render(<Queue />)

      expect(screen.getByText(label)).toBeInTheDocument()
      expect(screen.getByText(`${activeCount} active`)).toBeInTheDocument()
      expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
      expect(screen.queryByText('100%')).not.toBeInTheDocument()
    },
  )
})

describe('Queue — tv season/episode badge', () => {
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
    expect(screen.getByText('Some.Movie.2020.1080p.WEB-DL.x264-GROUP')).toHaveClass('font-mono')
  })

  it('renders the exact short-hash and seed-ratio metadata without implying a seeder count', () => {
    h.queue = [
      queueItem({
        title: 'Some Movie',
        torrent_hash: 'abc123def4567890',
        seed_ratio: 1.237,
      }),
    ]

    render(<Queue />)

    const shortHash = screen.getByTitle('abc123def4567890')
    expect(shortHash).toHaveTextContent('abc123def456')
    expect(shortHash.parentElement).toHaveTextContent('abc123def456 · seed 1.24')
  })

  it('renders queue cards as a dense semantic list', () => {
    h.queue = [queueItem({ title: 'Some Movie' })]

    render(<Queue />)

    expect(screen.getByRole('list')).toBeInTheDocument()
    expect(screen.getByRole('listitem')).toHaveClass('px-[14px]', 'py-[11px]', 'rounded-[10px]')
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
    expect(img).toHaveAttribute('alt', '')
    expect(img).toHaveClass('w-[38px]')
  })

  it('falls back to the poster placeholder when an image fails to load', () => {
    h.queue = [
      queueItem({ title: 'Some Movie', poster_url: 'https://image.tmdb.org/missing.jpg' }),
    ]

    const { container } = render(<Queue />)

    const img = container.querySelector('img')
    expect(img).not.toBeNull()
    fireEvent.error(img!)
    expect(container.querySelector('img')).not.toBeInTheDocument()
    expect(container.querySelector('[aria-hidden="true"].bg-poster')).toHaveClass(
      'w-[38px]',
      'aspect-[2/3]',
    )
  })

  it('renders a placeholder (no img) when poster_url is absent', () => {
    h.queue = [queueItem({ title: 'Some Movie', poster_url: null })]

    const { container } = render(<Queue />)

    expect(container.querySelector('img')).not.toBeInTheDocument()
    expect(container.querySelector('[aria-hidden="true"].bg-poster')).toBeInTheDocument()
  })

  it('surfaces a long failure reason verbatim in an overflow-safe detail line', () => {
    const reason =
      'download path not visible inside the container /an/extremely/long/container/path/that/must/not/be/shortened/movie.mkv'
    h.queue = [queueItem({ title: 'Blocked title', status: 'import_blocked', failed_reason: reason })]

    render(<Queue />)

    expect(screen.getByText(reason)).toHaveClass('[overflow-wrap:anywhere]')
  })

  // A live poll replaces every row object with a fresh one for the same id. The
  // card is keyed by id, so its per-row state (a poster that already 404'd) must
  // survive the re-render — the row must not flash back to a broken <img>, and
  // progress must track the new value without remounting the card.
  it('preserves a failed-poster fallback across a poll that only advances progress', () => {
    h.queue = [
      queueItem({
        id: 1,
        title: 'Some Movie',
        progress: 0.4,
        poster_url: 'https://image.tmdb.org/missing.jpg',
      }),
    ]

    const { container, rerender } = render(<Queue />)
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')).not.toBeInTheDocument()

    // Same id, same (still-broken) URL, new progress — the poll's fresh object.
    h.queue = [
      queueItem({
        id: 1,
        title: 'Some Movie',
        progress: 0.7,
        poster_url: 'https://image.tmdb.org/missing.jpg',
      }),
    ]
    rerender(<Queue />)

    // The fallback held (state survived the re-render); no broken <img> reappeared.
    expect(container.querySelector('img')).not.toBeInTheDocument()
    expect(container.querySelector('[aria-hidden="true"].bg-poster')).toBeInTheDocument()
    expect(
      screen.getByRole('progressbar', { name: 'Some Movie download progress' }),
    ).toHaveAttribute('aria-valuenow', '70')
  })
})

describe('Queue actions', () => {
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

  it.each([
    {
      action: 'Mark failed',
      title: 'Mark this download failed?',
      blocklist: false,
    },
    {
      action: 'Blocklist & fail',
      title: "Blocklist this release and mark failed? It won't be grabbed again.",
      blocklist: true,
    },
  ])('confirms $action with the current row identity', async ({ action, title, blocklist }) => {
    h.queue = [queueItem({ id: 27, title: 'Action title', status: 'downloading' })]
    h.markFailed.mockResolvedValue(queueItem({ id: 27, status: 'failed' }))

    render(<Queue />)

    fireEvent.click(screen.getByRole('button', { name: action }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: title })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeInTheDocument()

    fireEvent.click(within(dialog).getByRole('button', { name: action }))

    await waitFor(() => {
      expect(h.markFailed).toHaveBeenCalledWith({ downloadId: 27, blocklist })
    })
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

  it('closes a pending fail dialog without mutating when polling removes the row', async () => {
    h.queue = [queueItem({ id: 19, status: 'downloading' })]
    const view = render(<Queue />)

    fireEvent.click(screen.getByRole('button', { name: /^mark failed$/i }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    h.queue = []
    view.rerender(<Queue />)

    await waitFor(() => {
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    })
    expect(h.markFailed).not.toHaveBeenCalled()
  })

  it('keeps the mark-failed error visible through the existing error toast', async () => {
    const apiError: ApiError = {
      code: 'invalid_state_transition',
      message: 'download already importing',
      status: 409,
    }
    h.queue = [queueItem({ id: 31, status: 'downloading' })]
    h.markFailed.mockRejectedValue(apiError)

    render(<Queue />)

    fireEvent.click(screen.getByRole('button', { name: /^mark failed$/i }))
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Mark failed' }))

    await waitFor(() =>
      expect(h.toast).toHaveBeenCalledWith({
        title: 'Action failed',
        description: 'download already importing',
        intent: 'error',
      }),
    )
  })

  // Issue #205: MARK_FAILABLE is a positive allowlist (the states that legally
  // reach FailedPending, per domain/state_machine.py's TRANSITIONS, plus the
  // failed_pending "adopt" path) rather than a terminal denylist that only
  // excluded `importing`. `searching` has no edge to FailedPending (the backend
  // would 409 an operator's mark-failed there), so the OLD denylist's fail-open
  // behavior was itself a latent bug this allowlist fixes.
  it('hides fail actions for a status with no legal path to FailedPending (searching)', () => {
    h.queue = [queueItem({ status: 'searching' })]

    render(<Queue />)

    expect(screen.queryByRole('button', { name: /^mark failed$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /blocklist & fail/i })).not.toBeInTheDocument()
  })

  it('shows fail actions for failed_pending (the backend adopts an operator retry on it)', () => {
    h.queue = [queueItem({ status: 'failed_pending' })]

    render(<Queue />)

    expect(screen.getByRole('button', { name: /^mark failed$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /blocklist & fail/i })).toBeInTheDocument()
  })

  it.each(['metadata_fetching', 'import_pending', 'import_blocked', 'client_missing'] as const)(
    'shows fail actions for the legal pre-FailedPending state %s',
    (status) => {
      h.queue = [queueItem({ status })]

      render(<Queue />)

      expect(screen.getByRole('button', { name: /^mark failed$/i })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /blocklist & fail/i })).toBeInTheDocument()
    },
  )

  it('hides fail actions for a status this bundle does not recognize (fails closed, not open)', () => {
    // A future backend state this bundle predates, or corrupt/legacy data --
    // reachable only via a cast, exactly like the real untyped-JSON boundary.
    h.queue = [queueItem({ status: 'a_future_backend_state' as QueueItem['status'] })]

    render(<Queue />)

    expect(screen.queryByRole('button', { name: /^mark failed$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /blocklist & fail/i })).not.toBeInTheDocument()
  })
})

describe('Queue — Retry import (import_blocked only)', () => {
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
    const buttons = screen.getAllByRole('button')
    expect(buttons.map((button) => button.textContent)).toEqual([
      'Relocate & retry',
      'Retry import',
      'Mark failed',
      'Blocklist & fail',
    ])
    expect(buttons[0]?.parentElement).toHaveClass(
      'col-span-2',
      'flex-wrap',
      'justify-end',
      'lg:col-start-3',
    )
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

  it('does not loosely match the path-not-visible prefix later in a failure reason', () => {
    h.queue = [
      queueItem({
        status: 'import_blocked',
        failed_reason:
          'import failed because download path not visible inside the container /downloads/movie',
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
