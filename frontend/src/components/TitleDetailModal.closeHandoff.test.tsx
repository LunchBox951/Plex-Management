import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  useAuthMe,
  useCancelRequest,
  useCreateRequest,
  useGrab,
  useImportDownload,
  useMarkFailed,
  useQueue,
  useReportIssue,
  useTitleRequests,
  useSearchPreview,
  useSetKeepForever,
  useWithdrawSubscription,
} from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { TitleDetailModal } from './TitleDetailModal'

// Deliberately does NOT mock './ui/Dialog' (unlike TitleDetailModal.test.tsx),
// so this file exercises the REAL `handleCloseHandoff` wrapper wired into the
// REAL Radix `onCloseAutoFocus` — the exact mechanism issue #271's fix depends
// on. SearchOverlay.test.tsx only ever drives this contract through a mock
// that re-implements it (see that file's `TitleDetailModal` mock docstring),
// so without this file a regression in the real wrapper's ordering, or in
// `onClosed` never actually being wired to `onCloseAutoFocus`, would go
// undetected by the whole suite.
function idle() {
  return { mutateAsync: vi.fn(), isPending: false }
}

vi.mock('../api/hooks', () => ({
  useAuthMe: vi.fn(),
  useCreateRequest: vi.fn(),
  useSearchPreview: vi.fn(),
  useGrab: vi.fn(),
  useMarkFailed: vi.fn(),
  useImportDownload: vi.fn(),
  useTitleRequests: vi.fn(),
  useQueue: vi.fn(),
  useSetKeepForever: vi.fn(),
  useReportIssue: vi.fn(),
  useCancelRequest: vi.fn(),
  useWithdrawSubscription: vi.fn(),
}))

vi.mock('./ui/toast', () => ({ useToast: () => ({ toast: vi.fn() }) }))

const TITLE: DiscoverResult = {
  media_type: 'movie',
  tmdb_id: 42,
  title: 'Test Movie',
  year: 2021,
  library_state: 'none',
}

beforeEach(() => {
  vi.clearAllMocks()
  ;(useAuthMe as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
    data: { authenticated: true, auth_method: 'api_key', is_admin: true, user: null },
    isLoading: false,
  })
  ;(useCreateRequest as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useSearchPreview as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useGrab as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useMarkFailed as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useImportDownload as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useSetKeepForever as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useReportIssue as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useCancelRequest as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useWithdrawSubscription as unknown as ReturnType<typeof vi.fn>).mockReturnValue(idle())
  ;(useTitleRequests as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ authoritative: true, data: { requests: [] } })
  ;(useQueue as unknown as ReturnType<typeof vi.fn>).mockReturnValue({ data: { queue: [] } });
})

/** Renders the real TitleDetailModal beside a focusable trigger, controlled
 * exactly like a real caller (SearchOverlay): `open` is owned by this harness,
 * and `returnFocusTo`/`onClosed` are spies so the test can assert both the
 * call ORDER (target resolved before `onClosed` fires) and that focus is
 * actually restored — not just that the callbacks fired. */
function Harness({
  returnFocusTo,
  onClosed,
}: {
  returnFocusTo: () => HTMLElement | null
  onClosed: () => void
}) {
  const [open, setOpen] = useState(true)
  return <TitleDetailModal title={TITLE} open={open} onOpenChange={setOpen} returnFocusTo={returnFocusTo} onClosed={onClosed} />
}

describe('TitleDetailModal — real close handoff (issue #271)', () => {
  it('resolves the focus target, THEN fires onClosed exactly once, and actually returns focus', async () => {
    const trigger = document.createElement('button')
    trigger.textContent = 'trigger'
    document.body.appendChild(trigger)

    const calls: string[] = []
    const returnFocusTo = vi.fn(() => {
      calls.push('resolve-target')
      return trigger
    })
    const onClosed = vi.fn(() => calls.push('on-closed'))

    render(<Harness returnFocusTo={returnFocusTo} onClosed={onClosed} />)

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    await waitFor(() => {
      expect(onClosed).toHaveBeenCalledTimes(1)
    })

    // Ordering: the real `handleCloseHandoff` must resolve the target BEFORE
    // signalling the caller — a caller that nulls its selected-title state
    // (as SearchOverlay does) inside `onClosed` would otherwise race the
    // target resolution if the order were ever flipped.
    expect(calls).toEqual(['resolve-target', 'on-closed'])
    expect(returnFocusTo).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(trigger)

    document.body.removeChild(trigger)
  })

  it('never fires onClosed while the dialog is open, or more than once across a single close', async () => {
    const trigger = document.createElement('button')
    document.body.appendChild(trigger)
    const onClosed = vi.fn()

    render(<Harness returnFocusTo={() => trigger} onClosed={onClosed} />)
    expect(onClosed).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    await waitFor(() => expect(onClosed).toHaveBeenCalledTimes(1))
    // No further, delayed re-invocation once the handoff settles.
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(onClosed).toHaveBeenCalledTimes(1)

    document.body.removeChild(trigger)
  })
})
