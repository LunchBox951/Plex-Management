import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useCancelRequest, useGrab, useQueue, useRequests } from '../../api/hooks'
import type { QueueItem, RequestResponse } from '../../api/types'
import { ManageRequests } from './ManageRequests'

const h = vi.hoisted(() => ({
  grab: vi.fn(),
  cancelAsync: vi.fn(),
  markFailed: vi.fn(),
  toast: vi.fn(),
}))

vi.mock('../../api/hooks', () => ({
  useRequests: vi.fn(),
  useQueue: vi.fn(),
  useGrab: vi.fn(() => ({ mutate: h.grab, isPending: false })),
  useCancelRequest: vi.fn(() => ({ mutateAsync: h.cancelAsync, isPending: false })),
  useMarkFailed: vi.fn(() => ({ mutate: h.markFailed, isPending: false })),
  useForceImport: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useReportIssue: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  useSetKeepForever: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
}))

vi.mock('../../components/ui/toast', () => ({ useToast: () => ({ toast: h.toast }) }))

// The title dialog has its own exhaustive suite; here it only has to prove which
// request and which action it was opened with.
vi.mock('../../components/TitleDetailModal', () => ({
  TitleDetailModal: ({
    title,
    open,
    action,
  }: {
    title: { title: string }
    open: boolean
    action: { kind: string } | null
  }) =>
    open ? (
      <div role="dialog" aria-label={`Details: ${title.title}`}>
        {action ? `action:${action.kind}` : 'no action'}
      </div>
    ) : null,
}))

function row(overrides: Partial<RequestResponse>): RequestResponse {
  return {
    id: 1,
    tmdb_id: 1,
    media_type: 'movie',
    title: 'Untitled',
    status: 'pending',
    is_anime: false,
    keep_forever: false,
    can_mutate: true,
    is_owner: true,
    can_withdraw: false,
    has_other_participants: false,
    ...overrides,
  }
}

const REQUESTS = [
  row({ id: 1, title: 'Past Lives', status: 'no_acceptable_release', year: 2023 }),
  row({ id: 2, title: 'The Pitt', status: 'import_blocked', media_type: 'tv' }),
  row({ id: 3, title: 'Survivor', status: 'downloading', media_type: 'tv', download_progress: 0.62 }),
  row({ id: 4, title: 'Dune: Part Two', status: 'available', is_owner: false }),
]

const BLOCKED_DOWNLOAD: QueueItem = {
  id: 30,
  torrent_hash: 'abc',
  status: 'import_blocked',
  progress: 1,
  seed_ratio: 0,
  scopes: [],
  media_request_id: 2,
  release_title: 'The.Pitt.S02.1080p.WEB-DL',
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ManageRequests />
    </MemoryRouter>,
  )
}

function openMenu(title: string) {
  const trigger = screen.getByRole('button', { name: `Actions for ${title}` })
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
  return screen.findByRole('menu')
}

describe('Manage › Requests', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(useRequests).mockReturnValue({
      data: { requests: REQUESTS },
      isLoading: false,
      isError: false,
    } as never)
    vi.mocked(useQueue).mockReturnValue({ data: { queue: [BLOCKED_DOWNLOAD] } } as never)
    vi.mocked(useGrab).mockReturnValue({ mutate: h.grab, isPending: false } as never)
    vi.mocked(useCancelRequest).mockReturnValue({ mutateAsync: h.cancelAsync, isPending: false } as never)
  })

  it('lists every request with what was asked for and who asked', () => {
    renderPage()
    expect(screen.getByRole('button', { name: 'Past Lives' })).toBeInTheDocument()
    expect(screen.getByText('Movie · 2023')).toBeInTheDocument()
    expect(screen.getAllByText('Someone else')).toHaveLength(1)
  })

  it('filters by chip and by search text', () => {
    renderPage()
    fireEvent.click(screen.getByRole('tab', { name: /Needs attention/ }))
    expect(screen.getByRole('button', { name: 'Past Lives' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'The Pitt' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Survivor' })).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search requests' }), {
      target: { value: 'pitt' },
    })
    expect(screen.queryByRole('button', { name: 'Past Lives' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'The Pitt' })).toBeInTheDocument()
  })

  it('offers only the actions legal for the row, danger last', async () => {
    renderPage()
    const menu = await openMenu('Past Lives')
    const labels = within(menu)
      .getAllByRole('menuitem')
      .map((item) => item.firstChild?.textContent)
    expect(labels).toEqual([
      'Review the problem',
      'Search again',
      'Pick a release myself',
      'Keep forever',
      'Cancel request…',
    ])
  })

  it('offers Import anyway and Mark as bad on a blocked import with a download', async () => {
    renderPage()
    const menu = await openMenu('The Pitt')
    expect(within(menu).getByRole('menuitem', { name: /Import anyway…/ })).toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: /Mark download as bad…/ })).toBeInTheDocument()
    expect(within(menu).queryByRole('menuitem', { name: /Cancel request/ })).not.toBeInTheDocument()
  })

  it('Search again starts a grab immediately (no confirm)', async () => {
    renderPage()
    const menu = await openMenu('Past Lives')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Search again' }))
    expect(h.grab).toHaveBeenCalledWith({ request_id: 1, season: null }, expect.anything())
  })

  it('Pick a release myself opens the title on its release list', async () => {
    renderPage()
    const menu = await openMenu('Past Lives')
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Pick a release myself' }))
    expect(await screen.findByRole('dialog', { name: 'Details: Past Lives' })).toHaveTextContent(
      'action:re-search',
    )
  })

  it('clicking a row opens its detail; ticking its box only selects it', () => {
    renderPage()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Survivor' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Bulk actions' })).toHaveTextContent('1 selected')

    fireEvent.click(screen.getByRole('button', { name: 'Survivor' }))
    expect(screen.getByRole('dialog', { name: 'Details: Survivor' })).toHaveTextContent('no action')
  })

  it('bulk cancel confirms once with the count, then cancels each', async () => {
    h.cancelAsync.mockResolvedValue({})
    renderPage()
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Past Lives' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select Survivor' }))
    fireEvent.click(
      within(screen.getByRole('region', { name: 'Bulk actions' })).getByRole('button', {
        name: 'Cancel requests',
      }),
    )
    const dialog = screen.getByRole('dialog', { name: 'Cancel 2 requests?' })
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel 2 requests' }))
    })
    await waitFor(() => expect(h.cancelAsync).toHaveBeenCalledTimes(2))
    expect(h.cancelAsync).toHaveBeenCalledWith(1)
    expect(h.cancelAsync).toHaveBeenCalledWith(3)
  })
})
