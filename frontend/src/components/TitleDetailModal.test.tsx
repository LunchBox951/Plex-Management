import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useCreateRequest,
  useGrab,
  useImportDownload,
  useMarkFailed,
  useQueue,
  useRequests,
  useSearchPreview,
} from '../api/hooks'
import type {
  DiscoverResult,
  GrabRequest,
  QueueItem,
  RequestResponse,
  SearchPreviewResponse,
} from '../api/types'
import { TitleDetailModal } from './TitleDetailModal'

// No network and no Radix portals: the hooks and the Dialog/toast shells are replaced
// with controllable stand-ins so the tests exercise only the modal's grab-gating (G3)
// and report-gating (G6) logic.
vi.mock('../api/hooks', () => ({
  useCreateRequest: vi.fn(),
  useSearchPreview: vi.fn(),
  useGrab: vi.fn(),
  useMarkFailed: vi.fn(),
  useImportDownload: vi.fn(),
  useRequests: vi.fn(),
  useQueue: vi.fn(),
}))

vi.mock('./ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }))

vi.mock('./ui/Dialog', () => ({
  Dialog: ({ title, children }: { title: string; children: ReactNode }) => (
    <div>
      <h2>{title}</h2>
      {children}
    </div>
  ),
}))

const TITLE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 42,
  title: 'Test Movie',
  year: 2021,
}

function mutation(resolved: unknown) {
  return { mutateAsync: vi.fn().mockResolvedValue(resolved), isPending: false }
}

function idle() {
  return { mutateAsync: vi.fn(), isPending: false }
}

describe('TitleDetailModal grab gating on the create path (G3)', () => {
  const PREVIEW: SearchPreviewResponse = {
    accepted: [
      {
        guid: 'g1',
        indexer: 'Indexer A',
        quality_name: 'WEBDL-1080p',
        resolution: '1080p',
        score: 1000,
        source: 'WEBDL',
        title: 'Test.Movie.1080p.WEB-DL',
        seeders: 10,
        info_hash: 'hash1',
      },
    ],
    rejected: [],
    no_acceptable_release: false,
  }

  function setup(createdStatus: string) {
    const created: RequestResponse = {
      id: 7,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: createdStatus,
      is_anime: false,
      year: 2021,
    }
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation(created))
    ;(useSearchPreview as unknown as Mock).mockReturnValue(mutation(PREVIEW))
    ;(useGrab as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useMarkFailed as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useImportDownload as unknown as Mock).mockReturnValue(mutation(undefined))
    // liveRequest stays null: the /requests poll has NOT yet reflected the new row,
    // which is exactly the window where the bug enabled Grab.
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
  }

  beforeEach(() => vi.clearAllMocks())

  it('keeps Grab disabled when POST /requests returns a terminal row (available)', async () => {
    setup('available')
    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))
    const grab = await screen.findByRole('button', { name: /grab/i })
    // Terminal create -> not grabbable -> Grab stays disabled, so no grab can hit the
    // backend request_not_active guard. Fails before the fix (Grab enabled).
    expect(grab).toBeDisabled()
  })

  it('arms Grab when POST /requests returns a non-terminal row (pending)', async () => {
    setup('pending')
    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))
    const grab = await screen.findByRole('button', { name: /grab/i })
    expect(grab).toBeEnabled()
  })
})

describe('TitleDetailModal report-a-problem gating (G6)', () => {
  function request(overrides: Partial<RequestResponse> = {}): RequestResponse {
    return {
      id: 7,
      is_anime: false,
      media_type: 'movie',
      status: 'downloading',
      title: 'Test Movie',
      tmdb_id: 42,
      ...overrides,
    }
  }

  function queueItem(overrides: Partial<QueueItem> = {}): QueueItem {
    return {
      id: 11,
      media_request_id: 7,
      progress: 1,
      seed_ratio: 0,
      status: 'importing',
      torrent_hash: 'hash-1',
      ...overrides,
    }
  }

  // Request always 'downloading' (the lagging status); only the download status moves.
  function setDownloadStatus(downloadStatus: string): void {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [request({ status: 'downloading' })] },
    })
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [queueItem({ status: downloadStatus })] },
    })
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
  })

  it('hides "Report a problem" while the download is importing (mark-failed would 409)', () => {
    setDownloadStatus('importing')
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('button', { name: /report a problem/i })).not.toBeInTheDocument()
  })

  it('still offers "Report a problem" while genuinely downloading', () => {
    setDownloadStatus('downloading')
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /report a problem/i })).toBeInTheDocument()
  })
})

describe('TitleDetailModal — movie path is unchanged by the tv season selector', () => {
  it('renders no season UI and sends no season/seasons fields for a movie', async () => {
    const created: RequestResponse = {
      id: 55,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'pending',
      is_anime: false,
    }
    const createRequestMock = mutation(created)
    const searchPreviewMock = mutation({
      accepted: [],
      rejected: [],
      no_acceptable_release: true,
    } satisfies SearchPreviewResponse)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createRequestMock)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(searchPreviewMock)
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    // No season/whole-series controls exist at all for a movie.
    expect(screen.queryByText(/whole series/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Season')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/season to search/i)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))

    // The exact payloads below prove no `season`/`seasons` field snuck in.
    await waitFor(() =>
      expect(createRequestMock.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 42,
        media_type: 'movie',
      }),
    )
    await waitFor(() =>
      expect(searchPreviewMock.mutateAsync).toHaveBeenCalledWith({ request_id: 55 }),
    )
  })
})

describe('TitleDetailModal — tv season selector', () => {
  const TV_TITLE: DiscoverResult = {
    media_type: 'tv',
    tmdb_id: 100,
    title: 'Test Show',
    year: 2022,
  }

  beforeEach(() => vi.clearAllMocks())

  it('threads the chosen season into CreateRequestBody.seasons and SearchPreviewRequest.season', async () => {
    const created: RequestResponse = {
      id: 9,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'pending',
      is_anime: false,
      seasons: [{ season_number: 2, status: 'pending' }],
    }
    const createRequestMock = mutation(created)
    const searchPreviewMock = mutation({
      accepted: [],
      rejected: [],
      no_acceptable_release: true,
    } satisfies SearchPreviewResponse)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createRequestMock)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(searchPreviewMock)
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    render(<TitleDetailModal title={TV_TITLE} open onOpenChange={() => {}} />)

    // Uncheck "whole series" and pick season 2 before requesting.
    fireEvent.click(screen.getByRole('checkbox', { name: /whole series/i }))
    fireEvent.change(screen.getByLabelText(/season to search/i), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))

    await waitFor(() =>
      expect(createRequestMock.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 100,
        media_type: 'tv',
        seasons: [2],
      }),
    )
    await waitFor(() =>
      expect(searchPreviewMock.mutateAsync).toHaveBeenCalledWith({ request_id: 9, season: 2 }),
    )
  })

  it('keeps a failed TV season grabbable under an active (partially_available) show', async () => {
    // S1 available + S2 failed rolls up to partially_available (non-terminal), so the
    // backend would accept a re-grab of S2. The modal must ARM Grab for the failed
    // season, not dead-end into "Request again" (which dedups back to the same failed
    // season on an active show). Before the fix, failed -> Grab disabled.
    const created: RequestResponse = {
      id: 15,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'partially_available',
      is_anime: false,
      seasons: [
        { season_number: 1, status: 'available' },
        { season_number: 2, status: 'failed' },
      ],
    }
    const release = {
      guid: 'g3',
      indexer: 'Indexer A',
      quality_name: 'WEBDL-1080p',
      resolution: '1080p',
      score: 1000,
      source: 'WEBDL',
      title: 'Test.Show.S02.1080p.WEB-DL',
      seeders: 10,
      info_hash: 'hash3',
    }
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation(created))
    ;(useSearchPreview as unknown as Mock).mockReturnValue(
      mutation({
        accepted: [release],
        rejected: [],
        no_acceptable_release: false,
      } satisfies SearchPreviewResponse),
    )
    ;(useGrab as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    render(<TitleDetailModal title={TV_TITLE} open onOpenChange={() => {}} />)
    fireEvent.click(screen.getByRole('checkbox', { name: /whole series/i }))
    fireEvent.change(screen.getByLabelText(/season to search/i), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))

    const grab = await screen.findByRole('button', { name: /grab/i })
    expect(grab).toBeEnabled()
  })

  it('previews and arms Grab against the season the create RESOLVED to, not the click-time default (whole-series request, season 1 already in the library)', async () => {
    // "Whole series" stays checked (the default) — no season exists to pick before
    // the request is created, so the click-time default is season 1. The create
    // comes back tracking season 1 as already available (terminal) and season 2 as
    // the real actionable one — exactly the shape that exposed the bug.
    const created: RequestResponse = {
      id: 12,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'partially_available',
      is_anime: false,
      seasons: [
        { season_number: 1, status: 'available' },
        { season_number: 2, status: 'pending' },
      ],
    }
    const release = {
      guid: 'g2',
      indexer: 'Indexer A',
      quality_name: 'WEBDL-1080p',
      resolution: '1080p',
      score: 1000,
      source: 'WEBDL',
      title: 'Test.Show.S02.1080p.WEB-DL',
      seeders: 10,
      info_hash: 'hash2',
    }
    const createRequestMock = mutation(created)
    const searchPreviewMock = mutation({
      accepted: [release],
      rejected: [],
      no_acceptable_release: false,
    } satisfies SearchPreviewResponse)
    const grabMock = mutation(undefined)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createRequestMock)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(searchPreviewMock)
    ;(useGrab as unknown as Mock).mockReturnValue(grabMock)
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    render(<TitleDetailModal title={TV_TITLE} open onOpenChange={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))

    // The preview must search season 2 (the season the create resolved to) —
    // NEVER season 1, the stale click-time default.
    await waitFor(() =>
      expect(searchPreviewMock.mutateAsync).toHaveBeenCalledWith({ request_id: 12, season: 2 }),
    )

    // The selector settles on season 2 too, so the release list and the selector
    // agree (both season 2), rather than a season-2 selector over season-1 releases.
    const select = (await screen.findByLabelText('Season')) as HTMLSelectElement
    expect(select.value).toBe('2')

    // Season 2 is 'pending' (grabbable) — Grab must be armed, not disabled by
    // having been judged against season 1's terminal ('available') status.
    const grabButton = await screen.findByRole('button', { name: /grab/i })
    expect(grabButton).toBeEnabled()

    // And the grab itself must be scoped to season 2 — the season actually shown —
    // never silently recorded against season 1.
    fireEvent.click(grabButton)
    await waitFor(() =>
      expect(grabMock.mutateAsync).toHaveBeenCalledWith({
        request_id: 12,
        guid: 'g2',
        season: 2,
      } satisfies GrabRequest),
    )
  })

  it('enumerates every tracked season in the picker, with its own status label', () => {
    const request: RequestResponse = {
      id: 5,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'partially_available',
      is_anime: false,
      seasons: [
        { season_number: 1, status: 'available' },
        { season_number: 2, status: 'pending' },
      ],
    }
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [request] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    render(<TitleDetailModal title={TV_TITLE} open onOpenChange={() => {}} />)

    expect(screen.getByRole('option', { name: /season 1.*in library/i })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /season 2.*requested/i })).toBeInTheDocument()
  })

  it('derives the action zone from the SELECTED season, not the show-level rollup', async () => {
    // The show-level rollup is 'partially_available' — a value that never appears
    // on an individual SeasonRequest and, if it leaked into the per-season check,
    // would fall through to the generic 'unknown' UI for every season instead of
    // each season's own honest state.
    const request: RequestResponse = {
      id: 5,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'partially_available',
      is_anime: false,
      seasons: [
        { season_number: 1, status: 'available' },
        { season_number: 2, status: 'pending' },
      ],
    }
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [request] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    render(<TitleDetailModal title={TV_TITLE} open onOpenChange={() => {}} />)

    // Defaults to the first ACTIONABLE tracked season (season 2, still pending) —
    // never season 1 (already terminal/available).
    expect(screen.getByText(/searching/i)).toBeInTheDocument()

    // Switching to season 1 reveals ITS real state — already in the library —
    // rather than the show's 'partially_available' rollup leaking through.
    fireEvent.change(screen.getByLabelText('Season'), { target: { value: '1' } })
    expect(await screen.findByText(/in your library/i)).toBeInTheDocument()
  })
})
