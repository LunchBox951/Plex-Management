import { useRef, useState, type ReactNode } from 'react'
import type { DiscoverResult, RequestResponse } from '../../api/types'
import { TitleDetailModal, type TitleDetailModalAction } from '../TitleDetailModal'
import { requestToDiscoverResult } from '../../lib/requestTitle'

/**
 * Open the shared TitleDetailModal on one request row — reused, not forked. It
 * correlates its own live state by (tmdb_id, media_type), so opening it from a
 * request reproduces the full state-aware action zone (re-search, retry-import,
 * report a problem, cancel, keep-forever) right where the status lives.
 *
 * The modal is pinned to the EXACT row that was clicked (`boundRequestId`): an
 * admin's list can show two users' rows for the same title, and the modal's own
 * title-based correlation would otherwise resolve every verb to the FIRST match.
 */
export function useRequestDialog(): {
  openRequest: (request: RequestResponse) => void
  /** Open the modal straight into its release list ("Pick a release myself"). */
  pickRelease: (request: RequestResponse) => void
  dialog: ReactNode
} {
  const [selected, setSelected] = useState<DiscoverResult | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const [modalAction, setModalAction] = useState<TitleDetailModalAction | null>(null)
  const [boundRequestId, setBoundRequestId] = useState<number | null>(null)
  const nextActionToken = useRef(0)

  const openRequest = (request: RequestResponse) => {
    setModalAction(null)
    setSelected(requestToDiscoverResult(request))
    setBoundRequestId(request.id)
    setModalOpen(true)
  }

  const pickRelease = (request: RequestResponse) => {
    nextActionToken.current += 1
    // Resolve the target season from THIS request row's fresh season data — the
    // first no-release season is exactly the state the shortcut exists to fix.
    // The modal must not derive it from its own season selection: a reused modal
    // instance can still hold another title's pick when the action fires (see
    // TitleDetailModalAction.season). `null` for movies, and as the defensive tv
    // fallback (request-scoped preview) if no tracked season reads no-release.
    const season =
      request.media_type === 'tv'
        ? (request.seasons?.find((s) => s.status === 'no_acceptable_release')?.season_number ??
          null)
        : null
    setSelected(requestToDiscoverResult(request))
    setBoundRequestId(request.id)
    setModalAction({ kind: 're-search', requestId: request.id, season, token: nextActionToken.current })
    setModalOpen(true)
  }

  const changeModalOpen = (open: boolean) => {
    setModalOpen(open)
    if (!open) {
      setModalAction(null)
      setBoundRequestId(null)
    }
  }

  const dialog =
    selected !== null ? (
      <TitleDetailModal
        title={selected}
        open={modalOpen}
        onOpenChange={changeModalOpen}
        action={modalAction}
        boundRequestId={boundRequestId}
      />
    ) : null

  return { openRequest, pickRelease, dialog }
}
