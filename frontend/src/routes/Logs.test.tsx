import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi, type Mock } from 'vitest'
import { useExportLogs, useLogs, useLogsTail } from '../api/hooks'
import type { LogsResponse, LogsTailResponse } from '../api/types'
import { downloadTextFile } from '../lib/download'
import { Logs } from './Logs'

// No network: the ops log hooks are replaced with controllable stand-ins so the
// test exercises only the Logs page's own filtering/toggling/export wiring.
vi.mock('../api/hooks', () => ({
  useLogs: vi.fn(),
  useLogsTail: vi.fn(),
  useExportLogs: vi.fn(),
}))

vi.mock('../lib/download', () => ({ downloadTextFile: vi.fn() }))

const toastMock = vi.fn()
vi.mock('../components/ui/toast', () => ({ useToast: () => ({ toast: toastMock }) }))

function durablePage(overrides: Partial<LogsResponse> = {}): LogsResponse {
  return {
    total: 1,
    events: [
      {
        id: 1,
        created_at: '2026-01-01T00:00:00Z',
        level: 'ERROR',
        logger: 'plex_manager.services.eviction_service',
        message: 'evicted title X: watched, past grace period',
        context: { request_id: 42 },
      },
    ],
    ...overrides,
  }
}

function tailPage(overrides: Partial<LogsTailResponse> = {}): LogsTailResponse {
  return {
    dropped_count: 0,
    events: [
      {
        created_at: '2026-01-01T00:00:01Z',
        level: 'DEBUG',
        logger: 'plex_manager.web.app',
        message: 'tick',
        context: null,
      },
    ],
    ...overrides,
  }
}

describe('Logs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
      configurable: true,
    })
  })

  function setup(opts?: { logs?: LogsResponse; tail?: LogsTailResponse; liveTailEnabled?: boolean }) {
    ;(useLogs as unknown as Mock).mockReturnValue({
      data: opts?.logs ?? durablePage(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })
    ;(useLogsTail as unknown as Mock).mockReturnValue({
      data: opts?.tail ?? tailPage(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })
    ;(useExportLogs as unknown as Mock).mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue('log line 1\nlog line 2\n'),
      isPending: false,
    })
  }

  it('renders the durable store by default (level, logger, message)', () => {
    setup()
    render(<Logs />)
    const list = screen.getByRole('list')
    expect(within(list).getByText('ERROR')).toBeInTheDocument()
    expect(within(list).getByText('plex_manager.services.eviction_service')).toBeInTheDocument()
    expect(within(list).getByText(/evicted title x/i)).toBeInTheDocument()
  })

  it('switches to the live ring-buffer tail when the toggle is on', () => {
    setup()
    render(<Logs />)
    fireEvent.click(screen.getByRole('checkbox', { name: /live tail/i }))
    expect(screen.getByText('tick')).toBeInTheDocument()
    expect(screen.queryByText(/evicted title x/i)).not.toBeInTheDocument()
  })

  it('disables the level filter during live tail (the tail is always all-levels)', () => {
    setup()
    render(<Logs />)
    fireEvent.click(screen.getByRole('checkbox', { name: /live tail/i }))
    expect(screen.getByLabelText('Level')).toBeDisabled()
  })

  it('sends a purely-numeric search term as an exact correlation_id filter', () => {
    setup()
    render(<Logs />)
    fireEvent.change(screen.getByPlaceholderText(/request\/download\/tmdb id/i), {
      target: { value: '42' },
    })
    const lastCall = (useLogs as unknown as Mock).mock.calls.at(-1)
    expect(lastCall?.[0]).toEqual(expect.objectContaining({ correlationId: '42' }))
  })

  it('filters loaded events client-side by a non-numeric search term', () => {
    setup({
      logs: durablePage({
        total: 2,
        events: [
          ...durablePage().events,
          {
            id: 2,
            created_at: '2026-01-01T00:00:02Z',
            level: 'INFO',
            logger: 'plex_manager.services.health_service',
            message: 'plex reachable',
            context: null,
          },
        ],
      }),
    })
    render(<Logs />)
    fireEvent.change(screen.getByPlaceholderText(/request\/download\/tmdb id/i), {
      target: { value: 'reachable' },
    })
    expect(screen.getByText('plex reachable')).toBeInTheDocument()
    expect(screen.queryByText(/evicted title x/i)).not.toBeInTheDocument()
  })

  it('copies the export text to the clipboard on "Copy for diagnosis"', async () => {
    setup()
    render(<Logs />)
    fireEvent.click(screen.getByRole('button', { name: /copy for diagnosis/i }))
    await waitFor(() =>
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('log line 1\nlog line 2\n'),
    )
    expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ intent: 'success' }))
  })

  it('falls back to a download when the Clipboard API is unavailable (e.g. plain http://)', async () => {
    setup()
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    render(<Logs />)
    fireEvent.click(screen.getByRole('button', { name: /copy for diagnosis/i }))
    await waitFor(() => expect(downloadTextFile).toHaveBeenCalled())
    const [content] = (downloadTextFile as unknown as Mock).mock.calls[0] as [string, string]
    expect(content).toBe('log line 1\nlog line 2\n')
    // Never "Export failed" — the export itself succeeded; only copying locally
    // isn't available.
    expect(toastMock).not.toHaveBeenCalledWith(expect.objectContaining({ title: 'Export failed' }))
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringMatching(/clipboard unavailable/i) }),
    )
  })

  it('falls back to a download when the clipboard write itself is rejected', async () => {
    setup()
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
      configurable: true,
    })
    render(<Logs />)
    fireEvent.click(screen.getByRole('button', { name: /copy for diagnosis/i }))
    await waitFor(() => expect(downloadTextFile).toHaveBeenCalled())
    // Honest: the export succeeded, only the clipboard write was refused —
    // never the misleading "Export failed".
    expect(toastMock).not.toHaveBeenCalledWith(expect.objectContaining({ title: 'Export failed' }))
    expect(toastMock).toHaveBeenCalledWith(
      expect.objectContaining({ title: expect.stringMatching(/copy failed.*downloaded/i) }),
    )
  })

  it('triggers a file download on "Download"', async () => {
    setup()
    render(<Logs />)
    fireEvent.click(screen.getByRole('button', { name: /^download$/i }))
    await waitFor(() => expect(downloadTextFile).toHaveBeenCalled())
    const [content, filename] = (downloadTextFile as unknown as Mock).mock.calls[0] as [string, string]
    expect(content).toBe('log line 1\nlog line 2\n')
    expect(filename).toMatch(/^plex-manager-logs-.*\.txt$/)
  })

  it('surfaces an honest error with a retry action when the durable read fails', () => {
    const refetch = vi.fn()
    ;(useLogs as unknown as Mock).mockReturnValue({
      data: undefined,
      isLoading: false,
      isError: true,
      error: { code: 'unknown_error', message: 'Network down', status: 0 },
      refetch,
    })
    ;(useLogsTail as unknown as Mock).mockReturnValue({
      data: tailPage(),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })
    ;(useExportLogs as unknown as Mock).mockReturnValue({ mutateAsync: vi.fn(), isPending: false })

    render(<Logs />)
    expect(screen.getByText(/couldn't load logs/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    expect(refetch).toHaveBeenCalled()
  })

  it('paginates the durable store with Prev/Next', () => {
    setup({ logs: durablePage({ total: 250 }) })
    render(<Logs />)
    expect(screen.getByText(/1–1 of 250/)).toBeInTheDocument()
    const next = screen.getByRole('button', { name: /^next$/i })
    expect(screen.getByRole('button', { name: /^prev$/i })).toBeDisabled()
    fireEvent.click(next)
    const lastCall = (useLogs as unknown as Mock).mock.calls.at(-1)
    expect(lastCall?.[0]).toEqual(expect.objectContaining({ offset: 100 }))
  })
})
