import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { BlocklistEntry, BlocklistResponse } from '../api/types'
import { Blocklist } from './Blocklist'

const h = vi.hoisted(() => ({
  data: null as BlocklistResponse | null,
  loading: false,
  error: null as Error | null,
  refetch: vi.fn(),
  deleteMutateAsync: vi.fn(),
  deletePending: false,
  toast: vi.fn(),
}))

vi.mock('../api/hooks', () => ({
  useBlocklist: () => ({
    data: h.data,
    isLoading: h.loading,
    error: h.error,
    refetch: h.refetch,
  }),
  useDeleteBlocklistEntry: () => ({
    mutateAsync: h.deleteMutateAsync,
    isPending: h.deletePending,
  }),
}))

vi.mock('../components/ui/toast', () => ({
  useToast: () => ({ toast: h.toast }),
}))

const FULL_ENTRY: BlocklistEntry = {
  id: 41,
  source_title: 'Mortal.Kombat.II.2026.HDTS.x264-QRips',
  reason: 'quality blocked: TS',
  indexer: 'TorrentLeech',
  added_at: '2026-07-11T19:02:00Z',
}

const NULLABLE_ENTRY: BlocklistEntry = {
  id: 42,
  source_title: 'Release.With.Nullable.Metadata',
  reason: 'manual blocklist',
  indexer: null,
  added_at: null,
}

describe('Blocklist', () => {
  beforeEach(() => {
    h.data = { entries: [FULL_ENTRY, NULLABLE_ENTRY] }
    h.loading = false
    h.error = null
    h.refetch.mockReset()
    h.deleteMutateAsync.mockReset()
    h.deleteMutateAsync.mockResolvedValue(undefined)
    h.deletePending = false
    h.toast.mockReset()
  })

  it('renders the admin count and dense semantic rows', () => {
    render(<Blocklist />)

    const heading = screen.getByRole('heading', { level: 1, name: 'Blocklist' })
    expect(within(heading.parentElement!).getByText('2')).toBeInTheDocument()
    const list = screen.getByRole('list')
    const rows = within(list).getAllByRole('listitem')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveClass(
      'px-[14px]',
      'py-[11px]',
      'rounded-[10px]',
      'border',
      'border-hairline',
      'bg-surface',
    )
    expect(within(rows[0]!).getByText(FULL_ENTRY.source_title)).toHaveAttribute(
      'title',
      FULL_ENTRY.source_title,
    )
  })

  it('joins only present nullable metadata and lets the mono line wrap', () => {
    render(<Blocklist />)

    const rows = screen.getAllByRole('listitem')
    const expectedTimestamp = new Date(FULL_ENTRY.added_at!).toLocaleString()
    expect(
      within(rows[0]!).getByText(`quality blocked: TS · TorrentLeech · ${expectedTimestamp}`),
    ).toHaveClass('font-mono', 'text-[11px]', 'break-words')
    expect(within(rows[1]!).getByText('manual blocklist')).toBeInTheDocument()
    expect(rows[1]).not.toHaveTextContent('null')
    expect(rows[1]).not.toHaveTextContent('·')
  })

  it('uses the canonical successful empty state', () => {
    h.data = { entries: [] }
    render(<Blocklist />)

    expect(within(screen.getByRole('heading', { name: 'Blocklist' }).parentElement!).getByText('0')).toBeInTheDocument()
    const empty = screen.getByRole('status')
    expect(within(empty).getByText('Nothing blocklisted')).toBeInTheDocument()
    expect(
      within(empty).getByText('Releases you mark failed-with-blocklist will appear here.'),
    ).toBeInTheDocument()
  })

  it('does not delete until the operator confirms, then closes and toasts success', async () => {
    render(<Blocklist />)

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]!)
    expect(h.deleteMutateAsync).not.toHaveBeenCalled()

    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getAllByText(/release becomes eligible to grab again/i)).toHaveLength(2)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove blocklist entry' }))

    await waitFor(() => expect(h.deleteMutateAsync).toHaveBeenCalledWith(41))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(h.toast).toHaveBeenCalledWith({
      title: 'Removed from blocklist',
      intent: 'success',
    })
  })

  it('cancels without deleting', () => {
    render(<Blocklist />)

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]!)
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(h.deleteMutateAsync).not.toHaveBeenCalled()
  })

  it('disables both dialog actions and shows loading while deletion is pending', () => {
    h.deletePending = true
    render(<Blocklist />)

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]!)
    const dialog = screen.getByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: 'Remove blocklist entry' })
    expect(confirm).toBeDisabled()
    expect(confirm.querySelector('[aria-hidden="true"]')).not.toBeNull()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled()
  })

  it('keeps the entry and dialog available for retry after a failed delete', async () => {
    h.deleteMutateAsync.mockRejectedValueOnce(new Error('Database busy'))
    render(<Blocklist />)

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]!)
    const dialog = screen.getByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove blocklist entry' }))

    await waitFor(() =>
      expect(h.toast).toHaveBeenCalledWith({
        title: 'Remove failed',
        description: 'Database busy',
        intent: 'error',
      }),
    )
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText(FULL_ENTRY.source_title)).toBeInTheDocument()
  })

  it('does not expose a stale zero while loading', () => {
    h.data = null
    h.loading = true
    render(<Blocklist />)

    const heading = screen.getByRole('heading', { level: 1, name: 'Blocklist' })
    expect(heading.parentElement).toHaveTextContent('Blocklist')
    expect(heading.parentElement?.children).toHaveLength(1)
    expect(screen.getByRole('status', { name: 'Loading' })).toBeInTheDocument()
  })

  it('keeps errors actionable with Retry', () => {
    h.data = null
    h.error = new Error('Blocklist unavailable')
    render(<Blocklist />)

    expect(screen.getByText("Couldn't load the blocklist")).toBeInTheDocument()
    expect(screen.getByText('Blocklist unavailable')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(h.refetch).toHaveBeenCalledTimes(1)
  })
})
