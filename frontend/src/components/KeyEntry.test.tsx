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

  it('reports a rejected key distinctly and does not proceed on a 401', async () => {
    h.mutateAsync.mockRejectedValue({ code: 'invalid_api_key', message: 'nope', status: 401 })
    const onAuthenticated = vi.fn()

    render(<KeyEntry onAuthenticated={onAuthenticated} />)
    typeKey('bad-key')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    await waitFor(() => expect(screen.getByText(/rejected/i)).toBeInTheDocument())
    expect(onAuthenticated).not.toHaveBeenCalled()
  })

  it('reports a connectivity problem distinctly (not a bad key) when the server is unreachable', async () => {
    h.mutateAsync.mockRejectedValue({ code: 'unknown_error', message: 'boom', status: 0 })
    const onAuthenticated = vi.fn()

    render(<KeyEntry onAuthenticated={onAuthenticated} />)
    typeKey('recovery-key')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    await waitFor(() => expect(screen.getByText(/reach the server/i)).toBeInTheDocument())
    expect(screen.queryByText(/rejected/i)).not.toBeInTheDocument()
    expect(onAuthenticated).not.toHaveBeenCalled()
  })

  it('switches to Plex sign-in via the secondary affordance', () => {
    const onUsePlex = vi.fn()

    render(<KeyEntry onAuthenticated={vi.fn()} onUsePlex={onUsePlex} />)
    fireEvent.click(screen.getByRole('button', { name: 'Use Plex sign-in' }))

    expect(onUsePlex).toHaveBeenCalledTimes(1)
  })
})
