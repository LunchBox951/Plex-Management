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
    const createMutation = mutation(created)
    const previewMutation = mutation(PREVIEW)
    ;(useCreateRequest as unknown as Mock).mockReturnValue(createMutation)
    ;(useSearchPreview as unknown as Mock).mockReturnValue(previewMutation)
    ;(useGrab as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useMarkFailed as unknown as Mock).mockReturnValue(mutation(undefined))
    ;(useImportDownload as unknown as Mock).mockReturnValue(mutation(undefined))
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
    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))
    await waitFor(() => {
      expect(createMutation.mutateAsync).toHaveBeenCalled()
    })
    expect(screen.getByText(/searching/i)).toBeInTheDocument()
    expect(previewMutation.mutateAsync).not.toHaveBeenCalled()
    // Terminal create -> not grabbable -> no release list / Grab button is generated.
    expect(screen.queryByRole('button', { name: /grab/i })).not.toBeInTheDocument()
  })

  it('arms Grab when POST /requests returns a non-terminal row (pending)', async () => {
    setup('pending')
    fireEvent.click(screen.getByRole('button', { name: /^request$/i }))
    const grab = await screen.findByRole('button', { name: /grab/i })
    expect(grab).toBeEnabled()
  })
})

describe('TitleDetailModal deferred media types', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(useCreateRequest as unknown as Mock).mockReturnValue(idle())
    ;(useSearchPreview as unknown as Mock).mockReturnValue(idle())
    ;(useGrab as unknown as Mock).mockReturnValue(idle())
    ;(useMarkFailed as unknown as Mock).mockReturnValue(idle())
    ;(useImportDownload as unknown as Mock).mockReturnValue(idle())
    ;(useRequests as unknown as Mock).mockReturnValue({ data: { requests: [] } })
    ;(useQueue as unknown as Mock).mockReturnValue({ data: { queue: [] } })
  })

  it('does not offer request or preview actions for TV titles while TV import is deferred', () => {
    render(
      <TitleDetailModal
        title={{ ...TITLE, media_type: 'tv', title: 'Test Show' }}
        open
        onOpenChange={() => {}}
      />,
    )

    expect(screen.getByText(/TV requests are deferred/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^request$/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /preview releases/i })).not.toBeInTheDocument()
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
    expect(screen.getByRole('progressbar', { name: /download progress/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /report a problem/i })).toBeInTheDocument()
  })
})
