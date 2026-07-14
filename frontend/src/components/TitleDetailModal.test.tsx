import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import {
  StrictMode,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type ReactNode,
} from 'react'
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
  useWithdrawSubscription,
} from '../api/hooks'
import type {
  DiscoverResult,
  DownloadStateValue,
  GrabRequest,
  QueueItem,
  RequestResponse,
  RequestStatusValue,
  SearchPreviewResponse,
} from '../api/types'
import { TitleDetailModal } from './TitleDetailModal'

// The caller's auth context, read by the modal to gate admin-only verbs. A
// hoisted mutable holder (not mockReturnValue) so the per-role tests can flip it
// and the top-level beforeEach below restores the admin default for every other
// (admin-flow) describe block — vi.clearAllMocks clears calls, not return values.
const authState = vi.hoisted(() => {
  const admin = {
    data: { authenticated: true, auth_method: 'api_key', is_admin: true, user: null },
    isLoading: false,
  }
  return { admin, current: admin as typeof admin | { data: unknown; isLoading: boolean } }
})

// No network and no Radix portals: the hooks and the Dialog/toast shells are replaced
// with controllable stand-ins so the tests exercise only the modal's grab-gating (G3)
// and report-gating (G6) logic.
vi.mock('../api/hooks', () => ({
  useAuthMe: vi.fn(() => authState.current),
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
  useWithdrawSubscription: vi.fn(() => ({ mutateAsync: vi.fn(), isPending: false })),
}))

const toastState = vi.hoisted(() => ({ toast: vi.fn() }))

beforeEach(() => {
  authState.current = authState.admin
  toastState.toast.mockClear()
})

vi.mock('./ui/toast', () => ({ useToast: () => ({ toast: toastState.toast }) }))

vi.mock('./ui/Dialog', () => ({
  Dialog: ({
    title,
    description,
    children,
    customChrome = false,
  }: {
    title: string
    description?: string
    children: ReactNode
    customChrome?: boolean
  }) => (
    <div role="dialog">
      {customChrome ? null : <h2>{title}</h2>}
      {/* Faithful to the real Dialog (Dialog.tsx): `description` is rendered
          sr-only, NOT visibly. A prior mock that rendered it as a plain <p>
          masked issue #335 -- a destructive warning passed ONLY as
          `description` was invisible to sighted users yet still found by
          getByText. Keeping the sr-only class here lets a test assert the
          warning is rendered VISIBLY (in `children`), not just accessibly. */}
      {description ? <p className="sr-only">{description}</p> : null}
      {children}
    </div>
  ),
  DialogTitle: ({ children, ...props }: HTMLAttributes<HTMLHeadingElement>) => (
    <h2 {...props}>{children}</h2>
  ),
  DialogClose: ({ children, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button type="button" {...props}>
      {children}
    </button>
  ),
}))

const TITLE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 42,
  title: 'Test Movie',
  year: 2021,
  library_state: 'none',
}

function mutation(resolved: unknown) {
  return { mutateAsync: vi.fn().mockResolvedValue(resolved), isPending: false }
}

function idle() {
  return { mutateAsync: vi.fn(), isPending: false }
}

/**
 * Assert `pattern` is rendered VISIBLY (issue #335 / Codex Finding 2), not only
 * as the Dialog's sr-only `description`. The Dialog mock renders `description`
 * inside a `.sr-only` node just like the real component (Dialog.tsx), so a
 * warning passed ONLY via `description` would match `getByText` yet fail this
 * check -- every match would be inside an sr-only ancestor. Passing requires at
 * least one occurrence outside `.sr-only`, i.e. actually seen by sighted users.
 */
function expectWarningVisible(pattern: RegExp): void {
  const matches = screen.getAllByText(pattern)
  expect(matches.some((el) => el.closest('.sr-only') === null)).toBe(true)
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
        covered_seasons: [],
        target_seasons: [],
        upgrade_seasons: [],
        waste_seasons: [],
        ignored_seasons: [],
        skipped_seasons: [],
      },
    ],
    rejected: [],
    no_acceptable_release: false,
  }

  function setup(createdStatus: RequestStatusValue) {
    const created: RequestResponse = {
      id: 7,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: createdStatus,
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      year: 2021,
    }
    const createMutation = mutation(created)
    const previewMutation = mutation(PREVIEW)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createMutation)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(previewMutation)
    ;(useGrab as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useMarkFailed as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useImportDownload as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    // liveRequest stays null: the /requests poll has NOT yet reflected the new row,
    // which is exactly the window where the bug enabled Grab.
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    return { createMutation, previewMutation }
  }

  beforeEach(() => vi.clearAllMocks())

  it('skips preview when POST /requests returns a terminal row (available)', async () => {
    const { createMutation, previewMutation } = setup('available')
    fireEvent.click(screen.getByRole('button', { name: /^\+ request$/i }))
    await waitFor(() => {
      expect(createMutation.mutateAsync).toHaveBeenCalled()
    })
    expect(
      screen.getByText('Your request is queued and will be searched automatically.'),
    ).toBeInTheDocument()
    expect(previewMutation.mutateAsync).not.toHaveBeenCalled()
    // Terminal create -> not grabbable -> no release list / Grab button is generated.
    expect(screen.queryByRole('button', { name: /grab/i })).not.toBeInTheDocument()
  })

  it('arms Grab when POST /requests returns a non-terminal row (pending)', async () => {
    setup('pending')
    fireEvent.click(screen.getByRole('button', { name: /^\+ request$/i }))
    const grab = await screen.findByRole('button', { name: /grab/i })
    expect(grab).toBeEnabled()
  })
})

describe('TitleDetailModal TV request actions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
  })

  it('offers request and preview actions for TV titles', () => {
    render(
      <TitleDetailModal
        title={{ ...TITLE, media_type: 'tv', title: 'Test Show' }}
        open
        onOpenChange={() => {}}
      />,
    )

    expect(screen.queryByText(/TV requests are deferred/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^\+ request$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /search releases/i })).toBeInTheDocument()
  })
})

describe('TitleDetailModal report-a-problem gating (G6)', () => {
  function request(overrides: Partial<RequestResponse> = {}): RequestResponse {
    return {
      id: 7,
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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
  function setDownloadStatus(downloadStatus: DownloadStateValue): void {
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
    expect(screen.getByRole('progressbar', { name: /download progress/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /report a problem/i })).toBeInTheDocument()
  })

  it('closes an open report dialog when polling makes the download non-actionable', async () => {
    setDownloadStatus('downloading')
    const view = render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /report a problem/i }))
    expect(screen.getByText(/Blocklist this release/i)).toBeInTheDocument()

    setDownloadStatus('importing')
    view.rerender(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    await waitFor(() => {
      expect(screen.queryByText(/Blocklist this release/i)).not.toBeInTheDocument()
    })
  })

  // Issue #205 review follow-up: "Report a problem" drives the SAME `mark_failed`
  // mutation as Queue.tsx's Mark failed/Blocklist buttons, so it must be gated on
  // the identical positive allowlist (`isMarkFailableStatus`), not a denylist that
  // only excluded 'importing'. `searching` has no edge to FailedPending (the
  // backend would 409 an operator's mark-failed there), and a status this bundle
  // doesn't recognize at all (a future backend state, or corrupt/legacy data)
  // must fail CLOSED rather than exposing a control that can't succeed.
  it('hides "Report a problem" for a download state with no legal path to FailedPending (searching)', () => {
    setDownloadStatus('searching')
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('button', { name: /report a problem/i })).not.toBeInTheDocument()
  })

  it('hides "Report a problem" for a status this bundle does not recognize (fails closed, not open)', () => {
    setDownloadStatus('a_future_backend_state' as DownloadStateValue)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('button', { name: /report a problem/i })).not.toBeInTheDocument()
  })

  it.each(['metadata_fetching', 'import_pending', 'import_blocked', 'client_missing', 'failed_pending'] as const)(
    'still offers "Report a problem" for the legal mark-failable state %s',
    (status) => {
      setDownloadStatus(status)
      render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
      expect(screen.getByRole('button', { name: /report a problem/i })).toBeInTheDocument()
    },
  )
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
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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

    fireEvent.click(screen.getByRole('button', { name: /^\+ request$/i }))

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
    library_state: 'none',
  }

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
  })

  it('threads the chosen season into CreateRequestBody.seasons and SearchPreviewRequest.season', async () => {
    const created: RequestResponse = {
      id: 9,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'pending',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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
    fireEvent.click(screen.getByRole('button', { name: /^\+ request$/i }))

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
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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
      covered_seasons: [],
      target_seasons: [],
      upgrade_seasons: [],
      waste_seasons: [],
      ignored_seasons: [],
      skipped_seasons: [],
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
    fireEvent.click(screen.getByRole('button', { name: /^\+ request$/i }))

    const grab = await screen.findByRole('button', { name: /grab/i })
    expect(grab).toBeEnabled()
  })

  it('keeps a failed TV season grabbable when the parent reads completed only because a sibling is finalizing (issue #265/#287)', async () => {
    // S1 completed (finalizing, still awaiting Plex's confirmation) always wins the
    // parent rollup outright (season_rollup's precedence), so the show reads
    // 'completed' -- terminal-looking, but NOT because S2 settled. S2 is genuinely
    // 'failed' and must stay retryable exactly like it would under any other
    // non-terminal parent, matching what the backend (grab_service.grab) allows.
    const created: RequestResponse = {
      id: 16,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'completed',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      seasons: [
        { season_number: 1, status: 'completed' },
        { season_number: 2, status: 'failed' },
      ],
    }
    const release = {
      guid: 'g4',
      indexer: 'Indexer A',
      quality_name: 'WEBDL-1080p',
      resolution: '1080p',
      score: 1000,
      source: 'WEBDL',
      title: 'Test.Show.S02.1080p.WEB-DL',
      seeders: 10,
      info_hash: 'hash4',
      covered_seasons: [],
      target_seasons: [],
      upgrade_seasons: [],
      waste_seasons: [],
      ignored_seasons: [],
      skipped_seasons: [],
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
    fireEvent.click(screen.getByRole('button', { name: /^\+ request$/i }))

    const grab = await screen.findByRole('button', { name: /grab/i })
    expect(grab).toBeEnabled()
  })

  it('defaults the season picker to a failed season over a finalizing sibling (issue #287)', () => {
    // Neither 'completed' (S1) nor 'failed' (S2) is grabbable in isolation, but S2
    // is the season that actually needs attention -- the picker must not silently
    // default to the finalizing S1 and hide the retry action behind an extra click.
    const request: RequestResponse = {
      id: 17,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'completed',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      seasons: [
        { season_number: 1, status: 'completed' },
        { season_number: 2, status: 'failed' },
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

    // Defaults to season 2 (the failed, actually-actionable season) -- never
    // season 1, the finalizing sibling that merely happens to be listed first.
    const select = screen.getByLabelText('Season') as HTMLSelectElement
    expect(select.value).toBe('2')
    expect(
      screen.getByText('The request failed. Request it again to restart.'),
    ).toBeInTheDocument()
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
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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
      covered_seasons: [],
      target_seasons: [],
      upgrade_seasons: [],
      waste_seasons: [],
      ignored_seasons: [],
      skipped_seasons: [],
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
    fireEvent.click(screen.getByRole('button', { name: /^\+ request$/i }))

    await waitFor(() =>
      expect(createRequestMock.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 100,
        media_type: 'tv',
      }),
    )

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
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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
    expect(
      screen.getByText('Your request is queued and will be searched automatically.'),
    ).toBeInTheDocument()

    // Switching to season 1 reveals ITS real state — already in the library —
    // rather than the show's 'partially_available' rollup leaking through.
    fireEvent.change(screen.getByLabelText('Season'), { target: { value: '1' } })
    expect(
      await screen.findByText('This season is imported and visible in Plex.'),
    ).toBeInTheDocument()
  })

  it('matches queue rows by attached scope when the legacy season differs', () => {
    const request: RequestResponse = {
      id: 21,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'downloading',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      seasons: [
        { season_number: 1, status: 'available' },
        { season_number: 2, status: 'downloading' },
      ],
    }
    const sharedPack: QueueItem = {
      id: 31,
      media_request_id: 21,
      tmdb_id: 100,
      season: 1,
      episodes: null,
      progress: 0.63,
      seed_ratio: 0,
      status: 'downloading',
      torrent_hash: 'hash-shared-pack',
      scopes: [
        { media_request_id: 21, season: 1, episodes: null, status: 'active' },
        { media_request_id: 21, season: 2, episodes: null, status: 'active' },
      ],
    }
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useReportIssue as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [request] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [sharedPack] } })

    render(<TitleDetailModal title={TV_TITLE} open onOpenChange={() => {}} />)

    expect(screen.getByText('63%')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /report a problem/i })).toBeInTheDocument()
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
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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

  it('pins the NEW request once available, never the stale settled one it replaced', async () => {
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
    const createRequestMock = mutation(created)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createRequestMock)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    const setKeepForeverMock = mutation(undefined)
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(setKeepForeverMock)
    const view = render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    // Pins are offered only for a watchable selected scope, never for evicted or
    // in-flight content.
    expect(screen.queryByRole('checkbox', { name: /keep forever/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /request again/i }))

    await waitFor(() => expect(createRequestMock.mutateAsync).toHaveBeenCalled())
    expect(screen.queryByRole('checkbox', { name: /keep forever/i })).not.toBeInTheDocument()

    // Once polling confirms the fresh request is available, the checkbox must
    // target id 9 rather than the stale evicted id 7 that preceded it.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ id: 7, status: 'evicted', keep_forever: true }),
          movieRequest({ id: 9, status: 'available', keep_forever: false }),
        ],
      },
    })
    view.rerender(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    const freshPin = await screen.findByRole('checkbox', { name: /keep forever/i })
    expect(freshPin).not.toBeChecked()
    fireEvent.click(freshPin)
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
    expect(
      screen.getByText('Your request is queued and will be searched automatically.'),
    ).toBeInTheDocument()
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
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
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
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } });
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

  it('offers Cancel for a TV request waiting for its air date', async () => {
    const tvTitle: DiscoverResult = {
      media_type: 'tv',
      tmdb_id: 77,
      title: 'Future Show',
      year: 2026,
      library_state: 'none',
    }
    const waiting: RequestResponse = {
      id: 21,
      tmdb_id: 77,
      media_type: 'tv',
      title: 'Future Show',
      status: 'waiting_for_air_date',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      seasons: [{ season_number: 3, status: 'waiting_for_air_date' }],
    }
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [waiting] } })
    const cancelMock = mutation({ ...waiting, status: 'cancelled' })
    ;(useCancelRequest as unknown as Mock).mockReturnValue(cancelMock)
    render(<TitleDetailModal title={tvTitle} open onOpenChange={() => {}} />)

    expect(
      screen.getByText(
        "This season hasn't aired yet. It will be searched automatically after its air date.",
      ),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /cancel request/i }))
    const confirms = screen.getAllByRole('button', { name: /cancel request/i })
    fireEvent.click(confirms[confirms.length - 1]!)

    await waitFor(() => expect(cancelMock.mutateAsync).toHaveBeenCalledWith(21))
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
      library_state: 'none',
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
            can_mutate: true,
            is_owner: false,
            can_withdraw: false,
            has_other_participants: false,
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

describe('TitleDetailModal — subscriber control: Withdraw vs Cancel (issue #314)', () => {
  function movieRequest(overrides: Partial<RequestResponse> = {}): RequestResponse {
    return {
      id: 7,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'downloading',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      ...overrides,
    }
  }

  function asSharedUser() {
    authState.current = {
      data: {
        authenticated: true,
        auth_method: 'plex_session',
        is_admin: false,
        user: { is_admin: false },
      },
      isLoading: false,
    }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    authState.current = authState.admin
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useReportIssue as unknown as Mock).mockReturnValue(idle())
    ;(useCancelRequest as unknown as Mock).mockReturnValue(idle())
    ;(useWithdrawSubscription as unknown as Mock).mockReturnValue(idle())
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
  })

  it('shows "Cancel request" (never Withdraw) to an admin, even with other participants', () => {
    // authState.current already defaults to admin (see beforeEach above).
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({
            is_owner: false,
            can_withdraw: false,
            has_other_participants: true,
          }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /cancel request/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /withdraw/i })).not.toBeInTheDocument()
  })

  it('shows "Cancel request" to a non-admin sole owner (no other participants)', () => {
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ is_owner: true, can_withdraw: true, has_other_participants: false }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /cancel request/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /withdraw/i })).not.toBeInTheDocument()
  })

  it('shows "Withdraw" (never Cancel) to a non-admin owner WITH other participants', () => {
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ is_owner: true, can_withdraw: true, has_other_participants: true }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /withdraw/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /cancel request/i })).not.toBeInTheDocument()
  })

  it('shows "Withdraw" to a plain (non-owner) subscriber', () => {
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ is_owner: false, can_withdraw: true, has_other_participants: true }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /withdraw/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /cancel request/i })).not.toBeInTheDocument()
  })

  it('shows the plain-subscriber withdraw confirm copy and calls withdraw with the live request id', async () => {
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ id: 9, is_owner: false, can_withdraw: true, has_other_participants: true }),
        ],
      },
    })
    const withdrawMock = mutation(undefined)
    ;(useWithdrawSubscription as unknown as Mock).mockReturnValue(withdrawMock)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /withdraw/i }))
    expect(screen.getByText('Remove from your requests?')).toBeInTheDocument()
    const confirms = screen.getAllByRole('button', { name: /withdraw/i })
    fireEvent.click(confirms[confirms.length - 1]!)

    await waitFor(() => expect(withdrawMock.mutateAsync).toHaveBeenCalledWith(9))
  })

  it('shows the owner-with-others withdraw confirm copy (hand-off wording)', () => {
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({ is_owner: true, can_withdraw: true, has_other_participants: true }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /withdraw/i }))
    expect(screen.getByText('Withdraw and hand off?')).toBeInTheDocument()
  })

  it('shows the destructive cancel warning to a sole NON-OWNER subscriber on an ACTIVE row (issue #335)', () => {
    // Issue #335: on a cancellable/active status the backend's last-participant
    // branch tears down (`cancel_request`: torrent + file) and settles
    // `cancelled` -- REGARDLESS of ownership -- e.g. a browser user who
    // subscribed to an ownerless/API-key-created request that is still
    // `searching`. Keying the dialog off `isOwner` would show the benign
    // "continues for others" copy here even though withdrawing actually cancels
    // the request and removes the download.
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          // Ownerless request (`can_mutate: false`, matching the real API's
          // `_can_mutate_request`: neither admin nor owner) with the caller as
          // its sole subscriber -- Cancel is unavailable to them, only Withdraw.
          // A cancellable/active status (`searching`) is what makes the
          // last-participant withdrawal genuinely destructive.
          movieRequest({
            status: 'searching',
            can_mutate: false,
            is_owner: false,
            can_withdraw: true,
            has_other_participants: false,
          }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    expect(screen.queryByRole('button', { name: /cancel request/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /withdraw/i }))
    expect(screen.getByText('Withdraw and cancel request?')).toBeInTheDocument()
    expectWarningVisible(/withdrawing will cancel it and remove the download/i)
  })

  it('shows the MERE-REMOVAL copy (never the destructive warning) to a non-admin SOLE owner of a SETTLED row (issue #335)', () => {
    // Codex #333, Finding 2: a settled row has no Cancel (CANCELLABLE_STATUSES
    // excludes it), so gating Withdraw on `(!isOwner || hasOtherParticipants)`
    // wrongly left a sole owner with NO self-removal path. Gating on `!canCancel`
    // fixes it: Cancel is absent here, so Withdraw appears. Issue #335 (Codex
    // round): the confirm must NOT warn about a teardown here -- the backend's
    // last-participant branch on an ALREADY-SETTLED status (`available`) is a
    // MERE subscription removal (`withdraw_participant` reuses `cancel_request`
    // ONLY on a cancellable status), so the destructive copy would lie.
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({
            status: 'available',
            is_owner: true,
            can_withdraw: true,
            has_other_participants: false,
          }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /withdraw/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /cancel request/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /withdraw/i }))
    expect(screen.getByText('Remove from your requests?')).toBeInTheDocument()
    expectWarningVisible(/nothing is torn down/i)
    // The destructive teardown warning must be ABSENT on a settled row.
    expect(
      screen.queryByText(/withdrawing will cancel it and remove the download/i),
    ).not.toBeInTheDocument()
  })

  it('does NOT warn destructively for a sole participant on a TV row with an imported season under a cancellable rollup (issue #335, Codex round 2)', () => {
    // season_rollup precedence rolls {available, downloading} up to `downloading`
    // (in CANCELLABLE_STATUSES), but the backend `cancel_request` -- which
    // `withdraw_participant`'s last-participant branch reuses -- refuses the
    // whole request (not_cancellable, per-season guard) because S1 is imported,
    // so NO teardown happens. `willCancel` must fold in the same
    // `anySeasonImported` exclusion `canCancel` already uses; keying it off the
    // rollup status alone would show "cancel it and remove the download" for a
    // withdrawal the backend deterministically refuses to tear down.
    asSharedUser()
    const tvTitle: DiscoverResult = {
      media_type: 'tv',
      tmdb_id: 77,
      title: 'Mixed Show',
      year: 2022,
      library_state: 'none',
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
            can_mutate: true,
            is_owner: true,
            can_withdraw: true,
            has_other_participants: false,
            seasons: [
              { season_number: 1, status: 'available' },
              { season_number: 2, status: 'downloading' },
            ],
          } satisfies RequestResponse,
        ],
      },
    })
    render(<TitleDetailModal title={tvTitle} open onOpenChange={() => {}} />)

    // The imported season suppresses Cancel (existing per-season mirror), so
    // this sole owner's verb is Withdraw.
    expect(screen.queryByRole('button', { name: /cancel request/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /withdraw/i }))

    // #349/#351: the backend deterministically REFUSES this teardown
    // (`not_cancellable`, per-season guard) and removes nothing, so the copy is
    // the honest refusal variant -- neither the destructive warning (#335) nor the
    // benign "nothing is torn down", which would have over-promised a mere removal
    // the backend will 409.
    expect(screen.getByText("Can't withdraw yet")).toBeInTheDocument()
    expectWarningVisible(/still active and can't be withdrawn/i)
    expect(screen.queryByText('Remove from your requests?')).not.toBeInTheDocument()
    expect(screen.queryByText('Withdraw and cancel request?')).not.toBeInTheDocument()
    expect(
      screen.queryByText(/withdrawing will cancel it and remove the download/i),
    ).not.toBeInTheDocument()
  })

  it.each(['available', 'completed', 'failed', 'cancelled', 'evicted', 'import_blocked'] as const)(
    'offers Withdraw to a participant on a settled/blocked row (%s)',
    (status) => {
      // Codex #333, Finding 2: these switch arms previously omitted the Withdraw
      // button entirely, stranding a participant of a settled/blocked row with no
      // self-removal affordance.
      asSharedUser()
      ;(useRequests as unknown as Mock).mockReturnValue({
        data: {
          requests: [
            movieRequest({
              status,
              is_owner: false,
              can_withdraw: true,
              has_other_participants: true,
            }),
          ],
        },
      })
      render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
      expect(screen.getByRole('button', { name: /withdraw/i })).toBeInTheDocument()
    },
  )

  it('still shows Withdraw to a sole participant on import_blocked (backend owns the 409)', () => {
    // Codex #333, Findings 1+2: we deliberately do NOT re-derive the backend's
    // active-non-cancellable status set in the client. The button shows; a
    // last-participant withdrawal from import_blocked / partially_available 409s
    // `withdrawal_blocked_active_request`, which `runWithdraw` surfaces as a toast.
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({
            status: 'import_blocked',
            is_owner: true,
            can_withdraw: true,
            has_other_participants: false,
          }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /withdraw/i })).toBeInTheDocument()
  })

  it('shows the refusal-specific confirm copy for a sole participant on import_blocked (#349)', () => {
    // A sole participant on an ACTIVE non-cancellable row (import_blocked): the
    // backend 409s `withdrawal_blocked_active_request` and removes nothing. The
    // dialog must NOT fall into the benign "nothing is torn down" mere-removal
    // copy (which over-promises a removal that will be refused) nor the
    // destructive teardown copy -- it uses the honest refusal variant.
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({
            status: 'import_blocked',
            is_owner: true,
            can_withdraw: true,
            has_other_participants: false,
          }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: /withdraw/i }))
    expect(screen.getByText("Can't withdraw yet")).toBeInTheDocument()
    expectWarningVisible(/still active and can't be withdrawn/i)
    expect(screen.queryByText('Remove from your requests?')).not.toBeInTheDocument()
    expect(screen.queryByText('Withdraw and cancel request?')).not.toBeInTheDocument()
  })

  it('keys the success toast off the server outcome, not the click-time snapshot (#351)', async () => {
    // Snapshot at click time shows a co-participant remaining (benign "continues
    // for others" copy). Between snapshot and confirm that participant withdrew, so
    // the backend made THIS caller the sole participant on a cancellable row and
    // tore it down -- returning `settled: true`. The success toast must reflect the
    // REAL teardown, not the now-stale benign snapshot (the #351 lie).
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({
            id: 31,
            status: 'downloading',
            is_owner: false,
            can_withdraw: true,
            has_other_participants: true,
          }),
        ],
      },
    })
    const withdrawMock = mutation({ settled: true })
    ;(useWithdrawSubscription as unknown as Mock).mockReturnValue(withdrawMock)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /withdraw/i }))
    expectWarningVisible(/the download continues for others who requested it/i)
    const confirms = screen.getAllByRole('button', { name: /withdraw/i })
    fireEvent.click(confirms[confirms.length - 1]!)

    await waitFor(() => expect(withdrawMock.mutateAsync).toHaveBeenCalledWith(31))
    expect(toastState.toast).toHaveBeenCalledWith(
      expect.objectContaining({
        title: 'Request cancelled and download removed',
        intent: 'success',
      }),
    )
  })

  it('does NOT claim a teardown when the server merely removed the subscription (#351)', async () => {
    // The reverse stale-snapshot direction: the click-time snapshot looks
    // destructive (sole participant on a cancellable row), but the row settled
    // between snapshot and confirm so the backend did a MERE removal
    // (`settled: false`). The toast must not falsely claim a cancel + download
    // removal that did not happen.
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({
            id: 32,
            status: 'downloading',
            can_mutate: false,
            is_owner: false,
            can_withdraw: true,
            has_other_participants: false,
          }),
        ],
      },
    })
    const withdrawMock = mutation({ settled: false })
    ;(useWithdrawSubscription as unknown as Mock).mockReturnValue(withdrawMock)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: /withdraw/i }))
    // Destructive snapshot copy -- what the caller was shown pre-action.
    expect(screen.getByText('Withdraw and cancel request?')).toBeInTheDocument()
    const confirms = screen.getAllByRole('button', { name: /withdraw/i })
    fireEvent.click(confirms[confirms.length - 1]!)

    await waitFor(() => expect(withdrawMock.mutateAsync).toHaveBeenCalledWith(32))
    expect(toastState.toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: 'Removed from your requests', intent: 'success' }),
    )
  })

  it('shows "Withdraw" to an ADMIN who is ALSO a participant of a settled row', () => {
    // Codex #333 round 2, Finding C: gating on the API's `can_withdraw` (not a
    // blanket `!isAdmin`) lets an admin-participant remove THEMSELVES from a
    // settled row instead of being forced to hard-cancel it for everyone. The
    // admin auth default from beforeEach stands; `can_withdraw: true` marks them
    // a participant of this row.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({
            status: 'available',
            is_owner: false,
            can_withdraw: true,
            has_other_participants: true,
          }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /withdraw/i })).toBeInTheDocument()
  })

  it('hides "Withdraw" from a NON-participant admin on a settled row', () => {
    // Finding C boundary: an admin who does NOT subscribe to the row
    // (`can_withdraw: false`) still sees no Withdraw -- withdrawal is a
    // participant capability, and the API drives that with `can_withdraw`.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          movieRequest({
            status: 'available',
            is_owner: false,
            can_withdraw: false,
            has_other_participants: true,
          }),
        ],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('button', { name: /withdraw/i })).not.toBeInTheDocument()
  })
})

describe('TitleDetailModal — unknown status fails closed, not open (issue #205)', () => {
  function movieRequest(overrides: Partial<RequestResponse> = {}): RequestResponse {
    return {
      id: 7,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'downloading',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      ...overrides,
    }
  }

  // `vi.mocked(...)` (the Layout.test.tsx idiom) rather than this file's older
  // `;(hook as unknown as Mock)` leading-semicolon pattern: CodeQL's
  // js/automatic-semicolon-insertion rule flags the ASI-terminated statement
  // that pattern leaves at the end of each block (code-scanning alert #323).
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(useCreateRequest).mockReturnValue(idle() as never)
    vi.mocked(useSearchPreview).mockReturnValue(idle() as never)
    vi.mocked(useGrab).mockReturnValue(idle() as never)
    vi.mocked(useMarkFailed).mockReturnValue(idle() as never)
    vi.mocked(useImportDownload).mockReturnValue(idle() as never)
    vi.mocked(useSetKeepForever).mockReturnValue(idle() as never)
    vi.mocked(useReportIssue).mockReturnValue(idle() as never)
    vi.mocked(useCancelRequest).mockReturnValue(idle() as never)
    vi.mocked(useQueue).mockReturnValue({ data: { queue: [] } } as never)
  })

  // A status this bundle's `RequestStatusValue` union doesn't recognize can only
  // arrive at runtime (a rolling deploy talking to a newer backend, or a
  // corrupt/legacy row) -- constructing it here needs a cast, exactly like the
  // real boundary where untyped JSON crosses into the typed `RequestResponse`.
  const UNKNOWN_STATUS = 'a_future_backend_status' as RequestStatusValue

  it('renders a neutral badge for an unrecognized request status, never throwing', () => {
    vi.mocked(useRequests).mockReturnValue({
      data: { requests: [movieRequest({ status: UNKNOWN_STATUS })] },
    } as never)
    expect(() =>
      render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />),
    ).not.toThrow()
    expect(screen.getByText('A future backend status')).toBeInTheDocument()
  })

  it('offers no Grab and no Re-search for an unrecognized status (so the release browser, only reachable via Re-search/Preview, can never be opened)', () => {
    vi.mocked(useRequests).mockReturnValue({
      data: { requests: [movieRequest({ status: UNKNOWN_STATUS })] },
    } as never)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('button', { name: /^grab$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /re-search/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /preview releases/i })).not.toBeInTheDocument()
  })

  it('still offers Re-search for a KNOWN non-terminal status (no_acceptable_release)', () => {
    vi.mocked(useRequests).mockReturnValue({
      data: { requests: [movieRequest({ status: 'no_acceptable_release' })] },
    } as never)
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /re-search/i })).toBeInTheDocument()
  })
})

describe('TitleDetailModal — shared (non-admin) users get a request-only modal', () => {
  function movieRequest(overrides: Partial<RequestResponse> = {}): RequestResponse {
    return {
      id: 7,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'available',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      ...overrides,
    }
  }

  function baseMocks() {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
  }

  function asSharedUser() {
    authState.current = {
      data: {
        authenticated: true,
        auth_method: 'plex_session',
        is_admin: false,
        user: { is_admin: false },
      },
      isLoading: false,
    }
  }

  beforeEach(() => {
    vi.clearAllMocks()
    baseMocks()
  })

  it('shows Request but hides Preview releases for a shared user (admin keeps both)', () => {
    asSharedUser()
    const { unmount } = render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /^\+ request$/i })).toBeInTheDocument()
    // Preview drives the admin-only /search-preview: hidden, not a 403 machine.
    expect(screen.queryByRole('button', { name: /search releases/i })).not.toBeInTheDocument()
    unmount()

    // Same render as an admin: both verbs are offered.
    authState.current = authState.admin
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /^\+ request$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /search releases/i })).toBeInTheDocument()
  })

  it('keeps the admin-only queue query disabled for a shared user', () => {
    asSharedUser()
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    // GET /queue is require_admin: the query must be idle, not a 403 loop.
    expect(useQueue).toHaveBeenCalledWith({ poll: true, enabled: false })
  })

  it('ignores admin-only queue data cached before a shared-user role transition', () => {
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'import_blocked' })] },
    })
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: {
        queue: [
          {
            id: 11,
            media_request_id: 7,
            progress: 1,
            seed_ratio: 0,
            status: 'import_blocked',
            torrent_hash: 'cached-admin-row',
            failed_reason: 'Operator-only filesystem detail',
          } satisfies QueueItem,
        ],
      },
    })

    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)

    expect(
      screen.getByText('The download finished, but import needs operator attention.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/operator-only filesystem detail/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /report a problem|retry import/i })).not.toBeInTheDocument()
  })

  it('shows creator mutations to a shared user while keeping operator actions hidden', () => {
    asSharedUser()
    // The API capability, not the account's global role, grants creator actions.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'available', can_mutate: true })] },
    })
    const { unmount } = render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(
      screen.getByText('This title is imported and visible in Plex.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /keep forever/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /report a problem/i })).toBeInTheDocument()
    unmount()

    // Cancel is creator-capable, while re-search remains an admin-only release action.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'searching', can_mutate: true })] },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(
      screen.getByText('Scanning configured indexers for an acceptable release.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /re-search/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /cancel request/i })).toBeInTheDocument()
  })

  it('keeps a shared subscriber read-only when the request denies mutation capability', () => {
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [movieRequest({ status: 'available', can_mutate: false })],
      },
    })
    const { unmount } = render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(
      screen.getByText('This title is imported and visible in Plex.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/keep forever/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /report a problem/i })).not.toBeInTheDocument()
    unmount()

    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [movieRequest({ status: 'searching', can_mutate: false })],
      },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(
      screen.getByText('Scanning configured indexers for an acceptable release.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /re-search/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /cancel request/i })).not.toBeInTheDocument()
  })

  it('still offers "Request again" for a settled title to a shared user', () => {
    // POST /requests is NOT admin-only: re-requesting an evicted/failed title is
    // exactly the shared-user flow (the auto-grab worker does the rest).
    asSharedUser()
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [movieRequest({ status: 'evicted' })] },
    })
    render(<TitleDetailModal title={TITLE} open onOpenChange={() => {}} />)
    expect(screen.getByRole('button', { name: /request again/i })).toBeInTheDocument()
  })
})

describe('TitleDetailModal — one-shot release-preview action', () => {
  const EMPTY_PREVIEW: SearchPreviewResponse = {
    accepted: [],
    rejected: [],
    no_acceptable_release: true,
  }

  function baseMocks(requests: RequestResponse[]) {
    const previewMutation = mutation(EMPTY_PREVIEW)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(previewMutation)
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
    return previewMutation
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('consumes a movie re-search token once across rerenders', async () => {
    const request: RequestResponse = {
      id: 71,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'no_acceptable_release',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const previewMutation = baseMocks([request])
    const action = { kind: 're-search' as const, requestId: 71, season: null, token: 9 }
    const view = render(
      <StrictMode>
        <TitleDetailModal title={TITLE} open onOpenChange={() => {}} action={action} />
      </StrictMode>,
    )

    await waitFor(() =>
      expect(previewMutation.mutateAsync).toHaveBeenCalledWith({ request_id: 71 }),
    )
    expect(previewMutation.mutateAsync).toHaveBeenCalledTimes(1)

    view.rerender(
      <StrictMode>
        <TitleDetailModal title={TITLE} open onOpenChange={() => {}} action={action} />
      </StrictMode>,
    )
    await waitFor(() => expect(previewMutation.mutateAsync).toHaveBeenCalledTimes(1))
  })

  it('uses the action-supplied TV season, not the modal season state', async () => {
    const title: DiscoverResult = {
      media_type: 'tv',
      tmdb_id: 100,
      title: 'Test Show',
      year: 2022,
      library_state: 'processing',
    }
    const request: RequestResponse = {
      id: 72,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      status: 'no_acceptable_release',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      seasons: [
        { season_number: 1, status: 'available' },
        { season_number: 2, status: 'no_acceptable_release' },
      ],
    }
    const previewMutation = baseMocks([request])

    render(
      <TitleDetailModal
        title={title}
        open
        onOpenChange={() => {}}
        action={{ kind: 're-search', requestId: 72, season: 2, token: 10 }}
      />,
    )

    await waitFor(() =>
      expect(previewMutation.mutateAsync).toHaveBeenCalledWith({ request_id: 72, season: 2 }),
    )
    expect(previewMutation.mutateAsync).toHaveBeenCalledTimes(1)
  })

  it('ignores a stale season picked on a PREVIOUS title in the same modal instance', async () => {
    // A long-mounted modal is reused across titles. The operator picks season 1
    // on show A; the shortcut then opens show B in the same instance. The action
    // effect fires in the render where `activeSeason` still holds A's pick (the
    // title-reset effect has not applied yet), so the search must use the season
    // the action carries — resolved from B's own fresh request row — never the
    // modal's stale season state.
    const showA: DiscoverResult = {
      media_type: 'tv',
      tmdb_id: 100,
      title: 'Show A',
      year: 2022,
      library_state: 'processing',
    }
    const showB: DiscoverResult = {
      media_type: 'tv',
      tmdb_id: 200,
      title: 'Show B',
      year: 2023,
      library_state: 'processing',
    }
    const requestA: RequestResponse = {
      id: 73,
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Show A',
      status: 'downloading',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      seasons: [
        { season_number: 1, status: 'downloading' },
        { season_number: 2, status: 'pending' },
      ],
    }
    const requestB: RequestResponse = {
      id: 74,
      tmdb_id: 200,
      media_type: 'tv',
      title: 'Show B',
      status: 'no_acceptable_release',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      seasons: [
        { season_number: 1, status: 'available' },
        { season_number: 2, status: 'no_acceptable_release' },
      ],
    }
    const previewMutation = baseMocks([requestA, requestB])

    const view = render(
      <TitleDetailModal title={showA} open onOpenChange={() => {}} action={null} />,
    )
    // Show B also tracks a season 1, so this stale pick stays "valid" for B and
    // would silently win inside `resolveSeason` without the explicit override.
    fireEvent.change(screen.getByLabelText('Season'), { target: { value: '1' } })

    view.rerender(
      <TitleDetailModal
        title={showB}
        open
        onOpenChange={() => {}}
        action={{ kind: 're-search', requestId: 74, season: 2, token: 12 }}
      />,
    )

    await waitFor(() =>
      expect(previewMutation.mutateAsync).toHaveBeenCalledWith({ request_id: 74, season: 2 }),
    )
    expect(previewMutation.mutateAsync).toHaveBeenCalledTimes(1)
  })

  it('fails closed for a shared user even when an action is supplied', async () => {
    authState.current = {
      data: {
        authenticated: true,
        auth_method: 'plex_session',
        is_admin: false,
        user: { is_admin: false },
      },
      isLoading: false,
    }
    const previewMutation = baseMocks([])

    render(
      <TitleDetailModal
        title={TITLE}
        open
        onOpenChange={() => {}}
        action={{ kind: 're-search', requestId: 71, season: null, token: 11 }}
      />,
    )

    await waitFor(() => expect(previewMutation.mutateAsync).not.toHaveBeenCalled())
  })
})

describe('TitleDetailModal — bound request row (duplicate same-title rows)', () => {
  it('presents and acts on the CLICKED row, not the first title match', async () => {
    // An admin's Requests list legitimately shows TWO users' rows for the same
    // title (the display fold keys on user_id). Without an explicit binding the
    // modal's title-based correlation resolves to the FIRST non-settled match —
    // here another user's `downloading` row — so previewing/grabbing from the
    // clicked no-release row would target a different user's request.
    const otherUsersRow: RequestResponse = {
      id: 80,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'downloading',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const clickedRow: RequestResponse = {
      id: 81,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'no_acceptable_release',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const previewMutation = mutation({
      accepted: [],
      rejected: [],
      no_acceptable_release: true,
    })
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(previewMutation)
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [otherUsersRow, clickedRow] },
    })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    render(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={81} />,
    )

    // The action zone reflects the BOUND row's no-release state (Re-search),
    // not the unbound first match's `downloading` state…
    const reSearch = await screen.findByRole('button', { name: /re-search/i })
    fireEvent.click(reSearch)
    // …and the preview runs against the bound row's id.
    await waitFor(() =>
      expect(previewMutation.mutateAsync).toHaveBeenCalledWith({ request_id: 81 }),
    )
  })

  it('rebinds to the fresh row after "Request again" replaces a SETTLED bound row', async () => {
    // Issue #272: the operator opened the modal on a settled (evicted) row via
    // `boundRequestId`. Firing "Request again" creates a brand-new, active
    // request — the modal must track THAT row from then on, never the dead
    // evicted one `boundRequestId` still names, even once a LATER poll brings
    // both rows back (a literal id match on the stale prop would otherwise win
    // forever).
    const settledRow: RequestResponse = {
      id: 81,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'evicted',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const freshRow: RequestResponse = {
      id: 82,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'pending',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const createRequestMock = mutation(freshRow)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createRequestMock)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [settledRow] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    const view = render(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={81} />,
    )

    expect(screen.getByText('Evicted')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /request again/i }))
    await waitFor(() => expect(createRequestMock.mutateAsync).toHaveBeenCalled())

    // The poll catches up: BOTH rows now come back, the old settled one still
    // present right alongside the fresh one. `boundRequestId` (the prop) is
    // still literally 81 — before the fix this would keep resolving to the dead
    // evicted row forever, masking the fresh, genuinely active request.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [settledRow, freshRow] },
    })
    view.rerender(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={81} />,
    )

    await waitFor(() => expect(screen.queryByText('Evicted')).not.toBeInTheDocument())
    expect(
      screen.getByText('Your request is queued and will be searched automatically.'),
    ).toBeInTheDocument()
  })

  it('lets a newly clicked row win over a stale rebind from a previous "Request again" (issue #287)', async () => {
    // Issue #271 (separate, still open): the modal can stay MOUNTED across a
    // close, so `titleKey` never changes between two clicks on the SAME title's
    // duplicate rows. Continuing the scenario above -- the modal already
    // rebound itself to the fresh row (82) after "Request again" fired on the
    // settled bound row (81) -- the operator now opens a DIFFERENT existing
    // Requests row (83, e.g. another user's active request) for the SAME
    // title. That new click must win outright: the stale rebind must never
    // keep misdirecting preview/grab/cancel/pin at row 82 once a genuinely new
    // `boundRequestId` is supplied.
    const settledRow: RequestResponse = {
      id: 81,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'evicted',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const freshRow: RequestResponse = {
      id: 82,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'pending',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const otherUsersRow: RequestResponse = {
      id: 83,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'downloading',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const createRequestMock = mutation(freshRow)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createRequestMock)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [settledRow] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    const view = render(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={81} />,
    )

    // Rebind to the fresh row, exactly like the test above.
    expect(screen.getByText('Evicted')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /request again/i }))
    await waitFor(() => expect(createRequestMock.mutateAsync).toHaveBeenCalled())
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [settledRow, freshRow, otherUsersRow] },
    })
    view.rerender(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={81} />,
    )
    await waitFor(() =>
      expect(
        screen.getByText('Your request is queued and will be searched automatically.'),
      ).toBeInTheDocument(),
    )

    // The operator now opens a DIFFERENT row (83) for the same title — the
    // modal stays mounted (issue #271), so only `boundRequestId` changes.
    view.rerender(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={83} />,
    )

    // The newly clicked row (83, 'downloading') must win — never the stale
    // rebind (82, 'pending').
    await waitFor(() => expect(screen.getByText('Downloading')).toBeInTheDocument())
    expect(
      screen.queryByText('Your request is queued and will be searched automatically.'),
    ).not.toBeInTheDocument()
  })

  it('targets the newly clicked row\'s OWN download, not a stale created-row id, for mark-failed (issue #295)', async () => {
    // Issue #295: the `boundRequestId`-change effect used to reset only
    // `reboundRequestId`, leaving `requestId` (and `createdGrabbable`/
    // `createdSeasons`) pointed at whatever row "Request again" created earlier.
    // `effectiveRequestId` (`requestId ?? liveRequest?.id`) prefers a non-null
    // `requestId` outright, so — even though the status text above already
    // resolves correctly via `liveRequest` — "Report a problem" would still
    // silently target the STALE created row's download (82) instead of the
    // freshly clicked row's own (83), the very misdirection this bundle exists
    // to prevent. Continues the scenario above through the same two steps
    // (rebind via "Request again", then a new row click), but asserts on the
    // mark-failed ACTION TARGET rather than the status text.
    const settledRow: RequestResponse = {
      id: 81,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'evicted',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const freshRow: RequestResponse = {
      id: 82,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'pending',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    const otherUsersRow: RequestResponse = {
      id: 83,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'downloading',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
    }
    // A queue item for EACH request: the stale created row (82, id 501) and the
    // freshly clicked row (83, id 502). Both are mark-failable ('downloading')
    // so the bug can't hide behind a state that would hide the button either way.
    const staleQueueItem: QueueItem = {
      id: 501,
      media_request_id: 82,
      progress: 0.4,
      seed_ratio: 0,
      status: 'downloading',
      torrent_hash: 'stale-hash',
    }
    const freshQueueItem: QueueItem = {
      id: 502,
      media_request_id: 83,
      progress: 0.7,
      seed_ratio: 0,
      status: 'downloading',
      torrent_hash: 'fresh-hash',
    }
    const createRequestMock = mutation(freshRow)
    const markFailedMock = mutation(freshQueueItem)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createRequestMock)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(markFailedMock)
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [settledRow] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })

    const view = render(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={81} />,
    )

    // Rebind to the fresh row (82) via "Request again", exactly like the tests above.
    expect(screen.getByText('Evicted')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /request again/i }))
    await waitFor(() => expect(createRequestMock.mutateAsync).toHaveBeenCalled())
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [settledRow, freshRow, otherUsersRow] },
    })
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [staleQueueItem, freshQueueItem] },
    })
    view.rerender(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={81} />,
    )
    await waitFor(() =>
      expect(
        screen.getByText('Your request is queued and will be searched automatically.'),
      ).toBeInTheDocument(),
    )

    // The operator now opens a DIFFERENT row (83) for the same title — the
    // modal stays mounted (issue #271), so only `boundRequestId` changes.
    view.rerender(
      <TitleDetailModal title={TITLE} open onOpenChange={() => {}} boundRequestId={83} />,
    )
    await waitFor(() => expect(screen.getByText('Downloading')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /report a problem/i }))
    fireEvent.click(screen.getByRole('button', { name: /blocklist & re-search/i }))

    // The FRESH row's own download (502) must be the mark-failed target — never
    // the stale created row's (501), which `effectiveRequestId` would wrongly
    // keep preferring without the widened reset.
    await waitFor(() =>
      expect(markFailedMock.mutateAsync).toHaveBeenCalledWith({
        downloadId: 502,
        blocklist: true,
      }),
    )
  })
})

describe('TitleDetailModal — Re-acquire an owned title (issue #131)', () => {
  const EMPTY_PREVIEW: SearchPreviewResponse = {
    accepted: [],
    rejected: [],
    no_acceptable_release: true,
  }

  function created(overrides: Partial<RequestResponse> = {}): RequestResponse {
    return {
      id: 31,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status: 'pending',
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      ...overrides,
    }
  }

  function baseMocks() {
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(mutation(EMPTY_PREVIEW))
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useSetKeepForever as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
  }

  // Click through the confirm dialog: the opener and the dialog's confirm button
  // share the "Re-acquire" name (same pattern as the cancel-request test above),
  // so the LAST one is the dialog's.
  async function confirmReacquire() {
    fireEvent.click(screen.getByRole('button', { name: /^re-acquire$/i }))
    expect(screen.getByText('Re-acquire this title?')).toBeInTheDocument()
    const buttons = screen.getAllByRole('button', { name: /^re-acquire$/i })
    fireEvent.click(buttons[buttons.length - 1]!)
  }

  beforeEach(() => {
    vi.clearAllMocks()
    baseMocks()
  })

  it('replaces Request with Re-acquire on a presence-only owned movie and force-creates', async () => {
    // Owned per the Plex projection (library_state 'available') but NO tracked
    // request row at all: "Request" would short-circuit straight back to a
    // terminal available row with no grab, so the honest verb is Re-acquire.
    const createMutation = mutation(created())
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createMutation)

    render(
      <TitleDetailModal
        title={{ ...TITLE, library_state: 'available' }}
        open
        onOpenChange={() => {}}
      />,
    )

    expect(screen.getByRole('button', { name: /^re-acquire$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^\+ request$/i })).not.toBeInTheDocument()

    await confirmReacquire()
    await waitFor(() =>
      expect(createMutation.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 42,
        media_type: 'movie',
        force: true,
      }),
    )
  })

  it('offers Re-acquire beside report-issue on an available movie request and force-creates', async () => {
    // A tracked request already sits terminal `available` — the stale phantom row.
    // Re-acquire never re-arms it: the force-create makes a FRESH pending row.
    const createMutation = mutation(created())
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createMutation)
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: { requests: [created({ id: 7, status: 'available' })] },
    })

    render(
      <TitleDetailModal
        title={{ ...TITLE, library_state: 'available' }}
        open
        onOpenChange={() => {}}
      />,
    )

    // The available zone shows both verbs for an admin.
    expect(screen.getByText('This title is imported and visible in Plex.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /report a problem/i })).toBeInTheDocument()

    await confirmReacquire()
    await waitFor(() =>
      expect(createMutation.mutateAsync).toHaveBeenCalledWith({
        tmdb_id: 42,
        media_type: 'movie',
        force: true,
      }),
    )
  })

  it('never offers Re-acquire for a TV title (movie-only; report-issue covers tv)', () => {
    // An available tv season: report-issue is the per-season re-acquisition verb,
    // so the movie-only Re-acquire button must NOT appear.
    ;(useRequests as unknown as Mock).mockReturnValue({
      data: {
        requests: [
          {
            id: 20,
            tmdb_id: 100,
            media_type: 'tv',
            title: 'Test Show',
            status: 'available',
            is_anime: false,
            keep_forever: false,
            can_mutate: true,
            is_owner: false,
            can_withdraw: false,
            has_other_participants: false,
            seasons: [{ season_number: 1, status: 'available' }],
          } satisfies RequestResponse,
        ],
      },
    })

    render(
      <TitleDetailModal
        title={{
          media_type: 'tv',
          tmdb_id: 100,
          title: 'Test Show',
          year: 2022,
          library_state: 'available',
        }}
        open
        onOpenChange={() => {}}
      />,
    )

    expect(screen.getByText('This season is imported and visible in Plex.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^re-acquire$/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /report a problem/i })).toBeInTheDocument()
  })
})

describe('TitleDetailModal — four-zone presentation (issue #197)', () => {
  const MOVIE: DiscoverResult = {
    media_type: 'movie',
    tmdb_id: 42,
    title: 'Test Movie',
    year: 2021,
    library_state: 'none',
  }

  const TV: DiscoverResult = {
    media_type: 'tv',
    tmdb_id: 100,
    title: 'Test Show',
    year: 2022,
    library_state: 'none',
  }

  function request(
    status: RequestStatusValue,
    overrides: Partial<RequestResponse> = {},
  ): RequestResponse {
    return {
      id: 7,
      tmdb_id: 42,
      media_type: 'movie',
      title: 'Test Movie',
      status,
      is_anime: false,
      keep_forever: false,
      can_mutate: true,
      is_owner: false,
      can_withdraw: false,
      has_other_participants: false,
      ...overrides,
    }
  }

  function queueItem(overrides: Partial<QueueItem> = {}): QueueItem {
    return {
      id: 11,
      media_request_id: 7,
      progress: 0.63,
      seed_ratio: 0,
      status: 'downloading',
      torrent_hash: 'hash-11',
      ...overrides,
    }
  }

  function setBaseMocks(
    requests: RequestResponse[] = [],
    queue: QueueItem[] = [],
  ): void {
    // vi.mocked(...) (the Layout.test.tsx idiom) instead of leading-semicolon
    // `;(x as Mock)` statements: no statement starts with `(`, so none relies
    // on automatic semicolon insertion (CodeQL js/automatic-semicolon-insertion).
    vi.mocked(useCreateRequest).mockReturnValue(idle() as never)
    vi.mocked(useSearchPreview).mockReturnValue(
      mutation({ accepted: [], rejected: [], no_acceptable_release: true }) as never,
    )
    vi.mocked(useGrab).mockReturnValue(idle() as never)
    vi.mocked(useMarkFailed).mockReturnValue(idle() as never)
    vi.mocked(useImportDownload).mockReturnValue(idle() as never)
    vi.mocked(useSetKeepForever).mockReturnValue(idle() as never)
    vi.mocked(useReportIssue).mockReturnValue(idle() as never)
    vi.mocked(useCancelRequest).mockReturnValue(idle() as never)
    vi.mocked(useRequests).mockReturnValue({ data: { requests } } as never)
    vi.mocked(useQueue).mockReturnValue({ data: { queue } } as never)
  }

  beforeEach(() => {
    vi.clearAllMocks()
    setBaseMocks()
  })

  it('renders contract-backed identity, decorative artwork, one H2, and labelled close', async () => {
    const title: DiscoverResult = {
      ...MOVIE,
      overview: 'A contract-backed overview.',
      backdrop_url: 'https://image.tmdb.org/backdrop.jpg',
      poster_url: 'https://image.tmdb.org/poster.jpg',
    }
    const { container } = render(
      <TitleDetailModal title={title} open onOpenChange={() => {}} />,
    )

    expect(screen.getAllByRole('heading', { level: 2 })).toHaveLength(1)
    expect(screen.getByRole('heading', { level: 2, name: 'Test Movie' })).toBeInTheDocument()
    expect(screen.getByText('2021 · Movie')).toBeInTheDocument()
    expect(screen.getByText('A contract-backed overview.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Close' })).toHaveClass('focus-visible:ring-2')

    const backdrop = container.querySelector<HTMLImageElement>(
      'img[src="https://image.tmdb.org/backdrop.jpg"]',
    )
    const poster = container.querySelector<HTMLImageElement>(
      'img[src="https://image.tmdb.org/poster.jpg"]',
    )
    for (const artwork of [backdrop, poster]) {
      expect(artwork).toHaveAttribute('alt', '')
      expect(artwork).toHaveAttribute('aria-hidden', 'true')
    }

    fireEvent.error(backdrop!)
    fireEvent.error(poster!)
    await waitFor(() => {
      expect(
        container.querySelector('img[src="https://image.tmdb.org/backdrop.jpg"]'),
      ).not.toBeInTheDocument()
      expect(
        container.querySelector('img[src="https://image.tmdb.org/poster.jpg"]'),
      ).not.toBeInTheDocument()
    })
    expect(screen.getByTestId('title-backdrop')).toHaveClass('bg-poster')
    expect(screen.getByTestId('title-poster')).toHaveClass('bg-gradient-to-b')
    expect(screen.queryByText(/rating|runtime|genres|indexers/i)).not.toBeInTheDocument()
  })

  it('uses quiet artwork fallbacks and omits a missing overview cleanly', () => {
    const { container } = render(
      <TitleDetailModal title={{ ...TV, overview: null }} open onOpenChange={() => {}} />,
    )
    expect(container.querySelectorAll('img')).toHaveLength(0)
    expect(screen.getByText('2022 · TV')).toBeInTheDocument()
    expect(screen.queryByText('A contract-backed overview.')).not.toBeInTheDocument()
    expect(screen.getByTestId('title-backdrop')).toHaveClass('h-[180px]')
  })

  const stateCases: Array<{
    status: RequestStatusValue | null
    mediaType?: 'movie' | 'tv'
    badge: string
    sentence: string
    actions: string[]
    primary: string | null
    queueStatus?: DownloadStateValue
  }> = [
    {
      status: null,
      badge: 'Not requested',
      sentence: 'Not in the library and not requested.',
      actions: ['+ Request'],
      primary: '+ Request',
    },
    {
      status: 'pending',
      badge: 'Requested',
      sentence: 'Your request is queued and will be searched automatically.',
      actions: ['Cancel request'],
      primary: null,
    },
    {
      status: 'searching',
      badge: 'Searching',
      sentence: 'Scanning configured indexers for an acceptable release.',
      actions: ['Cancel request'],
      primary: null,
    },
    {
      status: 'downloading',
      badge: 'Downloading',
      sentence: 'A release was grabbed and is transferring.',
      actions: ['Report a problem', 'Cancel request'],
      primary: null,
      queueStatus: 'downloading',
    },
    {
      status: 'no_acceptable_release',
      badge: 'No release',
      sentence:
        'No acceptable release was found. Nothing was grabbed; automatic retries will continue.',
      actions: ['Re-search now', 'Cancel request'],
      primary: 'Re-search now',
    },
    {
      status: 'waiting_for_air_date',
      mediaType: 'tv',
      badge: 'Waiting for air date',
      sentence:
        "This season hasn't aired yet. It will be searched automatically after its air date.",
      actions: ['Cancel request'],
      primary: null,
    },
    {
      status: 'import_blocked',
      badge: 'Import blocked',
      sentence: 'The download finished, but import needs operator attention.',
      actions: ['Report a problem'],
      primary: null,
      queueStatus: 'import_blocked',
    },
    {
      status: 'completed',
      badge: 'Finalizing',
      sentence: 'Imported and awaiting Plex confirmation.',
      actions: ['Report a problem'],
      primary: null,
    },
    {
      status: 'available',
      badge: 'In library',
      sentence: 'This title is imported and visible in Plex.',
      actions: ['Open in Plex', 'Report a problem', 'Re-acquire'],
      primary: 'Open in Plex',
    },
    {
      status: 'failed',
      badge: 'Failed',
      sentence: 'The request failed. Request it again to restart.',
      // No 'Report a problem' here (issue #205): the failed zone's report button
      // drives the QUEUE mark-failed mutation, and a terminal `failed` download
      // has no legal edge to FailedPending (domain/state_machine.py TRANSITIONS)
      // and no adopt path -- the backend 409s it unconditionally. The pre-#205
      // `!== 'importing'` denylist offered that guaranteed-dead-end button;
      // `isMarkFailableStatus` fails closed. "Request again" IS the correction.
      actions: ['Request again'],
      primary: 'Request again',
      queueStatus: 'failed',
    },
    {
      status: 'evicted',
      badge: 'Evicted',
      sentence:
        'The disk-pressure sweep reclaimed this file. Deliberate space management — request again any time.',
      actions: ['Request again'],
      primary: 'Request again',
    },
    {
      status: 'cancelled',
      badge: 'Cancelled',
      sentence: 'This request was cancelled. Request it again any time.',
      actions: ['Request again'],
      primary: 'Request again',
    },
    {
      // A runtime-unknown status is reachable only via a cast, exactly like the
      // real untyped-JSON boundary (see UNKNOWN_STATUS in the #205 block above).
      status: 'mystery_state' as RequestStatusValue,
      badge: 'Mystery state',
      sentence:
        'Plex Manager reported “Mystery state”; no additional detail is available.',
      actions: [],
      primary: null,
    },
  ]

  it.each(stateCases)(
    'renders exact state copy and legal actions for $status',
    ({ status, mediaType = 'movie', badge, sentence, actions, primary, queueStatus }) => {
      const title = mediaType === 'tv' ? TV : MOVIE
      const seasons =
        mediaType === 'tv' && status !== null
          ? [{ season_number: 2, status }]
          : undefined
      const requests =
        status === null
          ? []
          : [
              request(status, {
                tmdb_id: title.tmdb_id,
                media_type: mediaType,
                title: title.title,
                ...(seasons ? { seasons } : {}),
              }),
            ]
      const queue = queueStatus
        ? [
            queueItem({
              status: queueStatus,
              tmdb_id: title.tmdb_id,
              ...(mediaType === 'tv'
                ? {
                    scopes: [
                      {
                        media_request_id: 7,
                        season: 2,
                        episodes: null,
                        status: 'active',
                      },
                    ],
                  }
                : {}),
            }),
          ]
        : []
      setBaseMocks(requests, queue)

      const { container } = render(
        <TitleDetailModal title={title} open onOpenChange={() => {}} />,
      )
      const stateRegion = screen.getByRole('region', { name: 'State' })
      expect(within(stateRegion).getAllByText(badge, { exact: true })[0]).toBeInTheDocument()
      expect(within(stateRegion).getByText(sentence, { exact: true })).toBeInTheDocument()

      const actionsRegion = screen.queryByRole('region', { name: 'Actions' })
      if (actions.length === 0) {
        expect(actionsRegion).not.toBeInTheDocument()
      } else {
        expect(actionsRegion).toBeInTheDocument()
        expect(actionsRegion!.querySelectorAll('button, a')).toHaveLength(actions.length)
        for (const actionName of actions) {
          expect(
            within(actionsRegion!).getByRole(actionName === 'Open in Plex' ? 'link' : 'button', {
              name: actionName === 'Open in Plex' ? /open in plex/i : actionName,
            }),
          ).toBeInTheDocument()
        }
      }

      const goldControls = container.querySelectorAll('button.bg-gold, a.bg-gold')
      expect(goldControls).toHaveLength(primary ? 1 : 0)
      if (primary) expect(goldControls[0]).toHaveTextContent(primary)
    },
  )

  it('uses TV-specific downloading and availability sentences', () => {
    const tvRequest = request('downloading', {
      tmdb_id: 100,
      media_type: 'tv',
      title: 'Test Show',
      seasons: [{ season_number: 3, status: 'downloading' }],
    })
    setBaseMocks(
      [tvRequest],
      [
        queueItem({
          scopes: [
            { media_request_id: 7, season: 3, episodes: null, status: 'active' },
          ],
        }),
      ],
    )
    const view = render(<TitleDetailModal title={TV} open onOpenChange={() => {}} />)
    expect(screen.getByText('Season 3 is downloading.')).toBeInTheDocument()

    setBaseMocks([
      request('available', {
        tmdb_id: 100,
        media_type: 'tv',
        title: 'Test Show',
        seasons: [{ season_number: 3, status: 'available' }],
      }),
    ])
    view.rerender(<TitleDetailModal title={TV} open onOpenChange={() => {}} />)
    expect(screen.getByText('This season is imported and visible in Plex.')).toBeInTheDocument()
  })

  it.each([
    [
      'import_blocked',
      'import_blocked',
      'codec validation failed',
      'The download finished, but import is blocked: codec validation failed',
    ],
    ['failed', 'failed', 'client removed torrent', 'The request failed: client removed torrent'],
  ] as const)('surfaces the real queue reason for %s', (status, queueStatus, reason, sentence) => {
    setBaseMocks(
      [request(status)],
      [queueItem({ status: queueStatus, failed_reason: reason })],
    )
    render(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)
    expect(screen.getByText(sentence, { exact: true })).toBeInTheDocument()
  })

  it.each([
    [0, '0%'],
    [0.63, '63%'],
  ])('renders real matching movie queue progress %s as %s', (progress, percent) => {
    setBaseMocks(
      [request('downloading')],
      [queueItem({ progress })],
    )
    render(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)
    expect(
      screen.getByRole('progressbar', { name: 'Download progress for Test Movie' }),
    ).toHaveAttribute('aria-valuenow', percent.replace('%', ''))
    expect(screen.getByText(percent, { exact: true })).toBeInTheDocument()
  })

  it('omits progress while the queue row is missing or loading', () => {
    setBaseMocks([request('downloading')])
    ;(useQueue as unknown as Mock).mockReturnValue({ data: undefined, isLoading: true })
    render(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.queryByText('0%', { exact: true })).not.toBeInTheDocument()
  })

  it('omits stale progress when the queue query is errored', () => {
    setBaseMocks([request('downloading')], [queueItem({ progress: 0.63 })])
    ;(useQueue as unknown as Mock).mockReturnValue({
      data: { queue: [queueItem({ progress: 0.63 })] },
      isError: true,
    })
    render(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.queryByText('63%', { exact: true })).not.toBeInTheDocument()
  })

  it('matches TV progress to the selected scope and never a sibling season', () => {
    setBaseMocks(
      [
        request('downloading', {
          tmdb_id: 100,
          media_type: 'tv',
          title: 'Test Show',
          seasons: [
            { season_number: 1, status: 'available' },
            { season_number: 2, status: 'downloading' },
          ],
        }),
      ],
      [
        queueItem({
          id: 10,
          progress: 0.12,
          scopes: [
            { media_request_id: 7, season: 1, episodes: null, status: 'active' },
          ],
        }),
        queueItem({
          id: 12,
          progress: 0.63,
          scopes: [
            { media_request_id: 7, season: 2, episodes: null, status: 'active' },
          ],
        }),
      ],
    )
    render(<TitleDetailModal title={TV} open onOpenChange={() => {}} />)
    expect(
      screen.getByRole('progressbar', {
        name: 'Download progress for Test Show, season 2',
      }),
    ).toHaveAttribute('aria-valuenow', '63')
    expect(screen.getByText('63%', { exact: true })).toBeInTheDocument()
    expect(screen.queryByText('12%', { exact: true })).not.toBeInTheDocument()
  })

  it('shows honest season N/M chips and omits unknown 0/0', () => {
    setBaseMocks([
      request('downloading', {
        tmdb_id: 100,
        media_type: 'tv',
        title: 'Test Show',
        seasons: [
          {
            season_number: 1,
            status: 'searching',
            imported_episode_count: 3,
            target_episode_count: 8,
          },
          {
            season_number: 2,
            status: 'pending',
            imported_episode_count: 0,
            target_episode_count: 0,
          },
          { season_number: 3, status: 'downloading' },
        ],
      }),
    ])
    render(<TitleDetailModal title={TV} open onOpenChange={() => {}} />)
    const list = screen.getByRole('list', { name: 'Season states' })
    expect(within(list).getByText(/S1 3\/8/)).toBeInTheDocument()
    expect(within(list).getByText(/S2$/)).toBeInTheDocument()
    expect(within(list).getByText(/S3$/)).toBeInTheDocument()
    expect(within(list).queryByText(/0\/0/)).not.toBeInTheDocument()
  })

  it('clears stale release rows when the selected TV season changes', async () => {
    const preview: SearchPreviewResponse = {
      accepted: [
        {
          guid: 'season-2-guid',
          indexer: 'Indexer A',
          quality_name: 'WEBDL-1080p',
          resolution: '1080p',
          score: 1000,
          source: 'WEBDL',
          title: 'Test.Show.S02.1080p.WEB-DL',
          seeders: 10,
          info_hash: 'hash-season-2',
          covered_seasons: [],
          target_seasons: [],
          upgrade_seasons: [],
          waste_seasons: [],
          ignored_seasons: [],
          skipped_seasons: [],
        },
      ],
      rejected: [],
      no_acceptable_release: false,
    }
    setBaseMocks([
      request('searching', {
        tmdb_id: 100,
        media_type: 'tv',
        title: 'Test Show',
        seasons: [
          { season_number: 1, status: 'searching' },
          { season_number: 2, status: 'searching' },
        ],
      }),
    ])
    ;(useSearchPreview as unknown as Mock).mockReturnValue(mutation(preview))
    render(<TitleDetailModal title={TV} open onOpenChange={() => {}} />)

    fireEvent.click(screen.getByRole('button', { name: 'Search releases' }))
    expect(await screen.findByText('Test.Show.S02.1080p.WEB-DL')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Season'), { target: { value: '2' } })
    expect(screen.queryByText('Test.Show.S02.1080p.WEB-DL')).not.toBeInTheDocument()
  })

  it('renders only the safe hosted Plex link for available titles', () => {
    setBaseMocks([request('available')])
    const view = render(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)
    const link = screen.getByRole('link', { name: /open in plex.*opens in a new tab/i })
    expect(link).toHaveAttribute('href', 'https://app.plex.tv/desktop/')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link.getAttribute('href')).not.toMatch(/token|ratingKey|#\/server/i)

    setBaseMocks([request('completed')])
    view.rerender(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)
    expect(screen.queryByRole('link', { name: /open in plex/i })).not.toBeInTheDocument()
  })

  it('hides the entire Admin zone and byte progress from a shared user', () => {
    authState.current = {
      data: {
        authenticated: true,
        auth_method: 'plex_session',
        is_admin: false,
        user: { is_admin: false },
      },
      isLoading: false,
    }
    setBaseMocks(
      [request('downloading')],
      [queueItem({ release_title: 'Secret.Release.Name' })],
    )
    render(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)

    expect(screen.queryByRole('region', { name: /admin.*releases/i })).not.toBeInTheDocument()
    expect(screen.queryByText('Secret.Release.Name')).not.toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(useQueue).toHaveBeenCalledWith({ poll: true, enabled: false })
    expect(screen.getByText('A release was grabbed and is transferring.')).toBeInTheDocument()
  })

  it('never says a presence-only owned movie is "not in the library"', () => {
    // Owned per Plex with no request row (the Re-acquire path, issue #131):
    // the State sentence must agree with the Re-acquire action the modal
    // simultaneously offers, not claim the title is absent from the library.
    setBaseMocks()
    render(
      <TitleDetailModal
        title={{ ...MOVIE, library_state: 'available' }}
        open
        onOpenChange={() => {}}
      />,
    )
    const stateRegion = screen.getByRole('region', { name: 'State' })
    expect(
      within(stateRegion).getByText(
        'In the library, but not tracked by a request. Re-acquire it if its file is missing or was replaced.',
      ),
    ).toBeInTheDocument()
    expect(screen.queryByText('Not in the library and not requested.')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Re-acquire' })).toBeInTheDocument()
  })

  it('describes partial presence honestly for an untracked TV title', () => {
    // No Re-acquire mention for tv (the verb is movie-only); presence is still
    // stated truthfully, and "+ Request" remains the offered action.
    setBaseMocks()
    render(
      <TitleDetailModal
        title={{ ...TV, library_state: 'partially_available' }}
        open
        onOpenChange={() => {}}
      />,
    )
    expect(
      screen.getByText('Partly in the library, but not tracked by a request.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/not in the library and not requested/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '+ Request' })).toBeInTheDocument()
  })

  it('never claims "no search run yet" while release search is closed', () => {
    // Downloading: a search DID run (its grab is why we're downloading) and the
    // browser is deliberately shut — the copy must say that, not "no search yet".
    setBaseMocks([request('downloading')], [queueItem()])
    const view = render(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)
    const admin = screen.getByRole('region', { name: 'ADMIN · RELEASES' })
    expect(
      within(admin).getByText("Release search isn't available in this state."),
    ).toBeInTheDocument()
    expect(within(admin).queryByText(/no release search run yet/i)).not.toBeInTheDocument()

    // Searchable state with no preview yet: the original placeholder is the truth.
    setBaseMocks([request('searching')])
    view.rerender(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)
    expect(screen.getByText(/no release search run yet for this title\./i)).toBeInTheDocument()
    expect(
      screen.queryByText("Release search isn't available in this state."),
    ).not.toBeInTheDocument()
  })

  it('keeps Search releases and Retry import in the Admin header', async () => {
    setBaseMocks([request('import_blocked')], [queueItem({ status: 'import_blocked' })])
    const importMutation = mutation(undefined)
    ;(useImportDownload as unknown as Mock).mockReturnValue(importMutation)
    render(<TitleDetailModal title={MOVIE} open onOpenChange={() => {}} />)

    const admin = screen.getByRole('region', { name: 'ADMIN · RELEASES' })
    const retry = within(admin).getByRole('button', { name: 'Retry import' })
    expect(screen.queryByRole('button', { name: /search releases/i })).not.toBeInTheDocument()
    fireEvent.click(retry)
    await waitFor(() => expect(importMutation.mutateAsync).toHaveBeenCalledWith(11))
  })
})
