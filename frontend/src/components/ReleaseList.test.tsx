import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { AcceptedRelease, SearchPreviewResponse } from '../api/types'
import { ReleaseList } from './ReleaseList'

function accepted(overrides: Partial<AcceptedRelease> = {}): AcceptedRelease {
  return {
    guid: 'guid-1',
    indexer: 'Indexer A',
    quality_name: 'WEBDL-1080p',
    resolution: '1080p',
    score: 1000,
    source: 'WEBDL',
    title: 'Some.Release.1080p.WEB-DL',
    seeders: 42,
    info_hash: 'abc',
    ...overrides,
  }
}

describe('ReleaseList', () => {
  it('surfaces the no-acceptable-release state honestly', () => {
    const preview: SearchPreviewResponse = {
      accepted: [],
      rejected: [{ title: 'CAM.copy', reason: 'quality_not_wanted' }],
      no_acceptable_release: true,
    }
    render(<ReleaseList preview={preview} onGrab={vi.fn()} grabbingGuid={null} canGrab={false} />)
    expect(screen.getByText(/no acceptable release found/i)).toBeInTheDocument()
    // The rejection reason is shown, not hidden.
    expect(screen.getByText(/quality not in profile/i)).toBeInTheDocument()
  })

  it('lists ranked releases and grabs the chosen one when allowed', () => {
    const onGrab = vi.fn()
    const preview: SearchPreviewResponse = {
      accepted: [accepted({ guid: 'g1', title: 'Top' }), accepted({ guid: 'g2', title: 'Second' })],
      rejected: [],
      no_acceptable_release: false,
    }
    render(<ReleaseList preview={preview} onGrab={onGrab} grabbingGuid={null} canGrab />)
    const grabButtons = screen.getAllByRole('button', { name: /grab/i })
    expect(grabButtons).toHaveLength(2)
    fireEvent.click(grabButtons[1]!)
    expect(onGrab).toHaveBeenCalledWith(expect.objectContaining({ guid: 'g2' }))
  })

  it('disables grabbing until a request exists', () => {
    const preview: SearchPreviewResponse = {
      accepted: [accepted()],
      rejected: [],
      no_acceptable_release: false,
    }
    render(<ReleaseList preview={preview} onGrab={vi.fn()} grabbingGuid={null} canGrab={false} />)
    expect(screen.getByRole('button', { name: /grab/i })).toBeDisabled()
    expect(screen.getByText(/request this title to grab/i)).toBeInTheDocument()
  })
})
