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

  it('uses the shared header and keeps all three filters in one accessible compact row', () => {
    setup()
    render(<Logs />)

    const heading = screen.getByRole('heading', { level: 1, name: 'Logs' })
    const header = heading.closest('header')
    expect(header).not.toBeNull()
    expect(header?.parentElement).toHaveClass(
      'max-w-[1160px]',
      'px-5',
      'sm:px-8',
      'lg:px-11',
    )
    const headerActions = within(header as HTMLElement).getAllByRole('button')
    expect(headerActions.map((button) => button.textContent)).toEqual([
      'Copy for diagnosis',
      'Download',
    ])
    for (const action of headerActions) expect(action).toHaveClass('h-8')

    const level = screen.getByRole('combobox', { name: 'Level' })
    const search = screen.getByRole('textbox', { name: 'Search' })
    const liveTail = screen.getByRole('checkbox', { name: 'Live tail' })
    const controlRow = level.parentElement
    expect(controlRow).toBe(search.parentElement)
    expect(controlRow).toBe(liveTail.closest('label')?.parentElement)
    expect(controlRow).toHaveClass(
      'grid',
      'grid-cols-[auto_minmax(0,1fr)_auto]',
      'min-w-0',
    )
    expect(level).toHaveClass('bg-surface-deep')
    expect(search).toHaveClass('min-w-0', 'bg-surface-deep')
    expect(level).toHaveAttribute('aria-describedby', 'logs-mode-description')
    expect(search).toHaveAttribute('aria-describedby', 'logs-mode-description')
    expect(liveTail).toHaveAttribute('aria-describedby', 'logs-mode-description')
    expect(level).toBeEnabled()
    expect(search).toBeEnabled()

    expect(within(level).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'All levels',
      'DEBUG',
      'INFO',
      'WARNING',
      'ERROR',
      'CRITICAL',
    ])

    fireEvent.click(liveTail)
    expect(level).toBeDisabled()
    expect(search).toBeEnabled()
  })

  it('renders the durable store by default (level, logger, message)', () => {
    setup()
    render(<Logs />)
    const list = screen.getByRole('list')
    expect(within(list).getByText('ERROR')).toBeInTheDocument()
    expect(within(list).getByText('plex_manager.services.eviction_service')).toBeInTheDocument()
    expect(within(list).getByText(/evicted title x/i)).toBeInTheDocument()
  })

  it('renders aligned, dense log rows with time, context, and the level color hierarchy', () => {
    const levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
    setup({
      logs: durablePage({
        total: levels.length,
        events: levels.map((level, index) => ({
          id: index + 1,
          created_at: `2026-01-01T00:00:0${index}Z`,
          level,
          logger: `plex_manager.services.long_logger_${index}`,
          message: `${level.toLowerCase()} diagnostic message`,
          context: index === 0 ? { request_id: 42, nested: { phase: 'scan' } } : null,
        })),
      }),
    })

    render(<Logs />)

    const list = screen.getByRole('list')
    expect(list).toHaveClass(
      'overflow-hidden',
      'rounded-[10px]',
      'border-hairline',
      'bg-surface-deep',
    )
    const expectedClasses: Record<string, string> = {
      DEBUG: 'text-faint',
      INFO: 'text-searching',
      WARNING: 'text-downloading',
      ERROR: 'text-error',
      CRITICAL: 'text-error',
    }
    for (const level of levels) {
      const levelText = within(list).getByText(level)
      expect(levelText).toHaveClass(expectedClasses[level] as string)
      const row = levelText.closest('li')
      expect(row).toHaveClass('px-[14px]', 'py-[7px]', 'border-hairline')
    }

    const debugRow = within(list).getByText('DEBUG').closest('li')
    expect(debugRow).not.toBeNull()
    const time = (debugRow as HTMLElement).querySelector('time')
    expect(time).toHaveAttribute('datetime', '2026-01-01T00:00:00Z')
    expect(time?.textContent).toBe(new Date('2026-01-01T00:00:00Z').toLocaleString())
    expect(
      within(debugRow as HTMLElement).getByText('plex_manager.services.long_logger_0'),
    ).toHaveAttribute('title', 'plex_manager.services.long_logger_0')
    expect(within(debugRow as HTMLElement).getByText('debug diagnostic message')).toHaveClass(
      'text-ink',
    )
    expect(
      within(debugRow as HTMLElement).getByText(
        '{"request_id":42,"nested":{"phase":"scan"}}',
      ),
    ).toHaveClass('font-mono', 'break-all', 'text-faint')
    expect(
      Array.from((debugRow as HTMLElement).children).map((child) => child.tagName),
    ).toEqual(['TIME', 'SPAN', 'SPAN', 'DIV'])
  })

  it('switches to the live ring-buffer tail when the toggle is on', () => {
    setup()
    render(<Logs />)
    expect((useLogs as unknown as Mock).mock.calls.at(-1)?.[1]).toEqual({ enabled: true })
    expect((useLogsTail as unknown as Mock).mock.calls.at(-1)?.[0]).toEqual({
      enabled: false,
      limit: 200,
    })
    fireEvent.click(screen.getByRole('checkbox', { name: /live tail/i }))
    expect(screen.getByText('tick')).toBeInTheDocument()
    expect(screen.queryByText(/evicted title x/i)).not.toBeInTheDocument()
    expect((useLogs as unknown as Mock).mock.calls.at(-1)?.[1]).toEqual({ enabled: false })
    expect((useLogsTail as unknown as Mock).mock.calls.at(-1)?.[0]).toEqual({
      enabled: true,
      limit: 200,
    })
  })

  it('keeps live tail all-level, page-locally searchable, and reports dropped durable records', () => {
    setup({
      tail: tailPage({
        dropped_count: 7,
        events: [
          ...tailPage().events,
          {
            created_at: '2026-01-01T00:00:02Z',
            level: 'ERROR',
            logger: 'plex_manager.services.reconcile',
            message: 'reconcile failed',
            context: null,
          },
        ],
      }),
    })
    render(<Logs />)
    fireEvent.click(screen.getByRole('checkbox', { name: /live tail/i }))

    expect(screen.getByText(/7 record\(s\) dropped from durable storage since startup/i)).toBeInTheDocument()
    expect(screen.getByText('tick')).toBeInTheDocument()
    expect(screen.getByText('reconcile failed')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: 'Search' }), {
      target: { value: 'reconcile' },
    })
    expect(screen.queryByText('tick')).not.toBeInTheDocument()
    expect(screen.getByText('reconcile failed')).toBeInTheDocument()
    expect((useLogsTail as unknown as Mock).mock.calls.at(-1)?.[0]).toEqual({
      enabled: true,
      limit: 200,
    })
  })

  it('keeps an existing live row stable when a newer tail record is prepended', () => {
    setup()
    const { rerender } = render(<Logs />)
    fireEvent.click(screen.getByRole('checkbox', { name: /live tail/i }))
    const existingRow = screen.getByText('tick').closest('li')
    expect(existingRow).not.toBeNull()

    ;(useLogsTail as unknown as Mock).mockReturnValue({
      data: tailPage({
        events: [
          {
            created_at: '2026-01-01T00:00:02Z',
            level: 'INFO',
            logger: 'plex_manager.web.app',
            message: 'newer tick',
            context: null,
          },
          ...tailPage().events,
        ],
      }),
      isLoading: false,
      isError: false,
      error: null,
      refetch: vi.fn(),
    })
    rerender(<Logs />)

    expect(screen.getByText('tick').closest('li')).toBe(existingRow)
    expect(screen.getByText('newer tick')).toBeInTheDocument()
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
      target: { value: ' 42 ' },
    })
    const lastCall = (useLogs as unknown as Mock).mock.calls.at(-1)
    expect(lastCall?.[0]).toEqual(expect.objectContaining({ correlationId: '42' }))
    // Numeric terms are already exact server-side filters; the client must not
    // hide a correlated row just because its message/logger omits the digits.
    expect(screen.getByText(/evicted title x/i)).toBeInTheDocument()
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

  it('scopes exports only by an exact numeric correlation id', async () => {
    const mutateAsync = vi.fn().mockResolvedValue('correlated trail')
    setup()
    ;(useExportLogs as unknown as Mock).mockReturnValue({ mutateAsync, isPending: false })
    render(<Logs />)

    fireEvent.change(screen.getByRole('textbox', { name: 'Search' }), {
      target: { value: ' 42 ' },
    })
    fireEvent.click(screen.getByRole('button', { name: /copy for diagnosis/i }))
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({ correlationId: '42' }))
  })

  it('does not pretend level or free-text display filters scope the server export', async () => {
    const mutateAsync = vi.fn().mockResolvedValue('complete trail')
    setup()
    ;(useExportLogs as unknown as Mock).mockReturnValue({ mutateAsync, isPending: false })
    render(<Logs />)

    fireEvent.change(screen.getByRole('combobox', { name: 'Level' }), {
      target: { value: 'ERROR' },
    })
    fireEvent.change(screen.getByRole('textbox', { name: 'Search' }), {
      target: { value: 'evicted' },
    })
    fireEvent.click(screen.getByRole('button', { name: /copy for diagnosis/i }))
    await waitFor(() => expect(mutateAsync).toHaveBeenCalledWith({}))
  })

  it('surfaces an export read failure without copying or downloading', async () => {
    const mutateAsync = vi.fn().mockRejectedValue({
      code: 'unknown_error',
      message: 'Export endpoint unavailable',
      status: 503,
    })
    setup()
    ;(useExportLogs as unknown as Mock).mockReturnValue({ mutateAsync, isPending: false })
    render(<Logs />)

    fireEvent.click(screen.getByRole('button', { name: /copy for diagnosis/i }))
    await waitFor(() =>
      expect(toastMock).toHaveBeenCalledWith(
        expect.objectContaining({
          title: 'Export failed',
          description: 'Export endpoint unavailable',
          intent: 'error',
        }),
      ),
    )
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled()
    expect(downloadTextFile).not.toHaveBeenCalled()
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

  it('uses distinct canonical empty guidance for durable history and the live tail', () => {
    setup({
      logs: durablePage({ total: 0, events: [] }),
      tail: tailPage({ dropped_count: 0, events: [] }),
    })
    render(<Logs />)

    const durableGuidance = screen.getByText('Try a wider filter or time range.')
    expect(durableGuidance.closest('[role="status"]')).toHaveClass(
      'rounded-[10px]',
      'border-dashed',
    )
    expect(screen.getByText('No matching log events')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /live tail/i }))
    expect(screen.queryByText('Try a wider filter or time range.')).not.toBeInTheDocument()
    const liveGuidance = screen.getByText('Nothing has been logged yet.')
    expect(liveGuidance.closest('[role="status"]')).toHaveClass(
      'rounded-[10px]',
      'border-dashed',
    )
    expect(screen.getByText('No matching log events')).toBeInTheDocument()
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

  it('resets durable pagination when level or search changes', () => {
    setup({ logs: durablePage({ total: 250 }) })
    render(<Logs />)

    fireEvent.click(screen.getByRole('button', { name: /^next$/i }))
    expect((useLogs as unknown as Mock).mock.calls.at(-1)?.[0]).toEqual(
      expect.objectContaining({ offset: 100 }),
    )

    fireEvent.change(screen.getByRole('combobox', { name: 'Level' }), {
      target: { value: 'WARNING' },
    })
    expect((useLogs as unknown as Mock).mock.calls.at(-1)?.[0]).toEqual(
      expect.objectContaining({ offset: 0, level: 'WARNING' }),
    )

    fireEvent.click(screen.getByRole('button', { name: /^next$/i }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Search' }), {
      target: { value: 'eviction' },
    })
    expect((useLogs as unknown as Mock).mock.calls.at(-1)?.[0]).toEqual(
      expect.objectContaining({ offset: 0, level: 'WARNING' }),
    )
  })
})
