import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import {
  useCancelRequest,
  useCreateRequest,
  useGrab,
  useImportDownload,
  useMarkFailed,
  useQueue,
  useReportIssue,
  useRequests,
  useSearchPreview,
  useSetKeepForever,
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
  useSetKeepForever: vi.fn(),
  // ADR-0014 correction hooks: default to an idle mutation so every render path
  // works without each setup wiring them (individual tests can still override).
  useReportIssue: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
  useCancelRequest: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
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
      keep_forever: false,
      year: 2021,
    }
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation(created))
    ;(useSearchPreview as unknown as Mock).mockReturnValue(mutation(PREVIEW))
    ;(useGrab as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useMarkFailed as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useImportDownload as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
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
      keep_forever: false,
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
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
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
      keep_forever: false,
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
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
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
      keep_forever: false,
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
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
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
      keep_forever: false,
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
      keep_forever: false,
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
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
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
      keep_forever: false,
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
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
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
      keep_forever: false,
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
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
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

describe('TitleDetailModal — keep-forever pin + evicted status (ADR-0012)', () => {
  function movieRequest(overrides: Partial<RequestResponse> = {}): RequestResponse {
    return {
      id: 7,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'available',
      is_anime: false,
      keep_forever: false,
      ...overrides,
    }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
  })

  it('shows no keep-forever control before any request exists', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.queryByText(/keep forever/i)).not.toBeInTheDocument()
  })

  it("reflects the live request's unpinned state and pins it on click", async () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ keep_forever: false })] },
    })
    const setKeepForeverMock = mutation(undefined)
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(setKeepForeverMock)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    const checkbox = screen.getByRole('checkbox', { name: /keep forever/i })
    expect(checkbox).not.toBeChecked()

    fireEvent.click(checkbox)
    await waitFor(() =>
      expect(setKeepForeverMock.mutateAsync).toHaveBeenCalledWith({
        requestId: 7,
        keepForever: true,
      }),
    )
  })

  it('shows the checkbox pre-checked when the request is already pinned', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ keep_forever: true })] },
    })
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('checkbox', { name: /keep forever/i })).toBeChecked()
  })

  it('renders the evicted status honestly with a "Request again" affordance, never Grab', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'evicted' })] },
    })
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByText('Evicted')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /request again/i })).toBeInTheDocument()
    // A settled (evicted) request is not grabbable — no stray Grab button.
    expect(screen.queryByRole('button', { name: /^grab/i })).not.toBeInTheDocument()
  })

  it('pins the NEW request after "Request again", never the stale settled one it replaced', async () => {
    // R4-5: the OLD request (id 7) is evicted AND was left pinned; it is what
    // /requests still returns -- the poll has NOT yet caught up to the fresh
    // re-request (mirrors G3's create-then-poll gap above, applied to the pin
    // action instead of Grab). Before the fix, `pinRequestId` preferred
    // `liveRequest?.id` unconditionally, so an immediate "Keep forever" toggle
    // right after "Request again" would have pinned the OLD, now-off-disk
    // request -- leaving the freshly re-grabbed copy unpinned (auto-evictable)
    // despite the success toast.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ id: 7, status: 'evicted', keep_forever: true })] },
    })
    const created = movieRequest({ id: 9, status: 'pending', keep_forever: false })
    ;(useCreateRequest as unknown as Mock).mockReturnValue(mutation(created))
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    const setKeepForeverMock = mutation(undefined)
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(setKeepForeverMock)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    // Before "Request again": the checkbox reflects the OLD (pinned) request.
    expect(screen.getByRole('checkbox', { name: /keep forever/i })).toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: /request again/i }))

    // The create resolves, requestId updates to 9 -- the pin target must follow
    // it immediately, NOT wait for /requests to catch up: a fresh request
    // always starts unpinned, so the checkbox flips to unchecked right away.
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: /keep forever/i })).not.toBeChecked(),
    )

    fireEvent.click(screen.getByRole('checkbox', { name: /keep forever/i }))
    await waitFor(() =>
      expect(setKeepForeverMock.mutateAsync).toHaveBeenCalledWith({
        requestId: 9,
        keepForever: true,
      }),
    )
    // Never targeted the stale, now-evicted request the operator just replaced.
    expect(setKeepForeverMock.mutateAsync).not.toHaveBeenCalledWith(
      expect.objectContaining({ requestId: 7 }),
    )
  })

  it('does not let a stale evicted row shadow a fresh re-request for the same title', () => {
    // Both an old evicted request AND a fresh one exist for this tmdb_id — the
    // fresh (non-settled) one must win, never the older evicted row (mirrors the
    // backend's own `_SETTLED_REQUEST_STATUSES` dedup exclusion).
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ id: 7, status: 'evicted' }),
          movieRequest({ id: 8, status: 'pending' }),
        ],
      },
    })
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByText(/searching/i)).toBeInTheDocument()
    expect(screen.queryByText(/^evicted$/i)).not.toBeInTheDocument()
  })
})

describe('TitleDetailModal — correction verbs report-issue + cancel (ADR-0014)', () => {
  function movieRequest(overrides: Partial<RequestResponse> = {}): RequestResponse {
    return {
      id: 7,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'available',
      is_anime: false,
      keep_forever: false,
      ...overrides,
    }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useReportIssue as unknown as Mock).mockReturnValue(idle())
    ;(useCancelRequest as unknown as Mock).mockReturnValue(idle())
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
  })

  it('reports an available title via the report-issue endpoint with the chosen reason', async () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'available' })] },
    })
    const reportMock = mutation(movieRequest({ status: 'searching' }))
    ;(useReportIssue as unknown as Mock).mockReturnValue(reportMock)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /report a problem/i }))
    fireEvent.change(screen.getByLabelText(/reason/i), { target: { value: 'wrong_media' } })
    fireEvent.click(screen.getByRole('button', { name: /blocklist & redo/i }))

    await waitFor(() =>
      expect(reportMock.mutateAsync).toHaveBeenCalledWith({
        requestId: 7,
        reason: 'wrong_media',
        season: null,
      }),
    )
  })

  it('offers Cancel for a searching request and calls the cancel endpoint', async () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'searching' })] },
    })
    const cancelMock = mutation(movieRequest({ status: 'cancelled' }))
    ;(useCancelRequest as unknown as Mock).mockReturnValue(cancelMock)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /cancel request/i }))
    // The confirm dialog's own "Cancel request" button (the second one) fires it.
    const confirms = screen.getAllByRole('button', { name: /cancel request/i })
    fireEvent.click(confirms[confirms.length - 1]!)

    await waitFor(() => expect(cancelMock.mutateAsync).toHaveBeenCalledWith(7))
  })

  it('does not offer Cancel for an already-imported (available) request', () => {
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'available' })] },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('button', { name: /cancel request/i })).not.toBeInTheDocument()
  })

  it('does not let a stale cancelled row shadow a fresh active re-request', async () => {
    // ADR-0014: after cancelling then re-requesting the same title, the older
    // `cancelled` row must not shadow the newer active one — the modal must target the
    // fresh id, not the settled cancelled one. (liveRequest treats cancelled as settled.)
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ id: 7, status: 'cancelled' }),
          movieRequest({ id: 8, status: 'searching' }),
        ],
      },
    })
    const cancelMock = mutation(movieRequest({ id: 8, status: 'cancelled' }))
    ;(useCancelRequest as unknown as Mock).mockReturnValue(cancelMock)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    // The Cancel action is offered (liveRequest resolved to the active `searching` row,
    // not the cancelled one — a cancelled liveRequest is not cancellable), and targets id 8.
    fireEvent.click(screen.getByRole('button', { name: /cancel request/i }))
    const confirms = screen.getAllByRole('button', { name: /cancel request/i })
    fireEvent.click(confirms[confirms.length - 1]!)
    await waitFor(() => expect(cancelMock.mutateAsync).toHaveBeenCalledWith(8))
  })

  it('hides Cancel when a TV season is already imported even if the rollup is cancellable', () => {
    // season_rollup precedence rolls {available, downloading} up to `downloading` (in
    // CANCELLABLE_STATUSES), but the backend cancel_request refuses the whole request
    // because S1 is available. The modal must mirror that per-season guard and NOT offer
    // a Cancel button that would deterministically 409.
    const tvTitle: DiscoverResult = {
      media_type: 'tv',
      tmdb_id: 77,
      title: 'Mixed Show',
      year: 2022,
    }
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          {
            id: 20,
            tmdb_id: 77,
            media_type: 'tv',
            title: 'Mixed Show',
            status: 'downloading',
            is_anime: false,
            keep_forever: false,
            seasons: [
              { season_number: 1, status: 'available' },
              { season_number: 2, status: 'downloading' },
            ],
          } satisfies RequestResponse,
        ],
      },
    })
    render(<TitleDetailModal title={tvTitle} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('button', { name: /cancel request/i })).not.toBeInTheDocument()
  })
})
