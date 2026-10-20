import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMarkNotificationsRead, useNotifications } from '../../api/hooks'
import { NotificationList } from './NotificationList'

vi.mock('../../api/hooks', () => ({
  useNotifications: vi.fn(),
  useMarkNotificationsRead: vi.fn(),
}))

const markRead = vi.fn()

function renderList() {
  return render(
    <MemoryRouter>
      <NotificationList />
    </MemoryRouter>,
  )
}

describe('NotificationList', () => {
  beforeEach(() => {
    markRead.mockReset()
    vi.mocked(useMarkNotificationsRead).mockReturnValue({ mutate: markRead } as never)
  })

  it('shows an honest "not available yet" state while the backend answers 501', () => {
    vi.mocked(useNotifications).mockReturnValue({
      isLoading: false,
      isError: true,
      error: { code: 'not_implemented', message: 'x', status: 501 },
    } as never)
    renderList()
    expect(screen.getByRole('status')).toHaveTextContent("Notifications aren't available yet.")
  })

  it('opens an item and marks it read', () => {
    vi.mocked(useNotifications).mockReturnValue({
      isLoading: false,
      isError: false,
      data: {
        unread_count: 1,
        notifications: [
          {
            id: 9,
            kind: 'ready_to_watch',
            message: 'Survivor S50 E4 is ready to watch',
            created_at: new Date().toISOString(),
            read: false,
            request_id: 4,
          },
        ],
      },
    } as never)
    renderList()
    fireEvent.click(screen.getByRole('button', { name: /Survivor S50 E4 is ready to watch/ }))
    expect(markRead).toHaveBeenCalledWith([9])
  })

  it('says so when there is nothing new', () => {
    vi.mocked(useNotifications).mockReturnValue({
      isLoading: false,
      isError: false,
      data: { unread_count: 0, notifications: [] },
    } as never)
    renderList()
    expect(screen.getByText("You're all caught up.")).toBeInTheDocument()
  })
})
