import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { KeyEntry } from './KeyEntry'

const h = vi.hoisted(() => ({
  mutateAsync: vi.fn(),
  isPending: false,
}))

vi.mock('../api/hooks', () => ({
  useExchangeApiKey: () => ({ mutateAsync: h.mutateAsync, isPending: h.isPending }),
}))

beforeEach(() => {
  h.mutateAsync.mockReset()
  h.isPending = false
})

function typeKey(value: string): void {
  fireEvent.change(screen.getByLabelText('Access key'), { target: { value } })
}

describe('KeyEntry break-glass exchange', () => {
  it('exchanges the pasted key and reports success (never stores it locally)', async () => {
    h.mutateAsync.mockResolvedValue({ authenticated: true, auth_method: 'api_key', is_admin: true })
    const onAuthenticated = vi.fn()

    render(<KeyEntry onAuthenticated={onAuthenticated} />)
    typeKey('recovery-key')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    // The key is handed straight to the exchange mutation — nothing writes it to
    // storage (CodeQL #263). The mutation is what attaches the X-Api-Key header.
    await waitFor(() => expect(h.mutateAsync).toHaveBeenCalledWith('recovery-key'))
    await waitFor(() => expect(onAuthenticated).toHaveBeenCalledTimes(1))
  })

  it('shows a retryable error and does not proceed when the exchange is rejected', async () => {
    h.mutateAsync.mockRejectedValue(new Error('invalid_api_key'))
    const onAuthenticated = vi.fn()

    render(<KeyEntry onAuthenticated={onAuthenticated} />)
    typeKey('bad-key')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    await waitFor(() => expect(screen.getByText(/rejected/i)).toBeInTheDocument())
    expect(onAuthenticated).not.toHaveBeenCalled()
  })

  it('switches to Plex sign-in via the secondary affordance', () => {
    const onUsePlex = vi.fn()

    render(<KeyEntry onAuthenticated={vi.fn()} onUsePlex={onUsePlex} />)
    fireEvent.click(screen.getByRole('button', { name: 'Use Plex sign-in' }))

    expect(onUsePlex).toHaveBeenCalledTimes(1)
  })
})
