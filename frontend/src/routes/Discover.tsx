import { useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { useDiscoverHome } from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { Row } from '../components/Row'
import { Spotlight } from '../components/Spotlight'
import { TitleDetailModal } from '../components/TitleDetailModal'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useDiscoverTilePresentation } from '../components/useDiscoverTilePresentation'

export function Discover() {
  const home = useDiscoverHome()
  const { tileState, quickRequestable, requestStateRevision } =
    useDiscoverTilePresentation(home.dataUpdatedAt)
  const [selected, setSelected] = useState<DiscoverResult | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  const layoutContext = useOutletContext<{ searchOpen?: boolean } | null>()
  const searchOpen = layoutContext?.searchOpen ?? false

  const openTitle = (title: DiscoverResult) => {
    setSelected(title)
    setModalOpen(true)
  }

  return (
    <div className="w-full">
      {home.isLoading ? (
        <div className="px-5 py-8 sm:px-8 lg:px-11">
          <CenteredSpinner label="Loading Discover…" />
        </div>
      ) : home.isError ? (
        <div className="px-5 py-8 sm:px-8 lg:px-11">
          <StateMessage
            tone="error"
            title="Couldn’t load Discover"
            message={home.error.message}
            action={
              <button
                type="button"
                onClick={() => void home.refetch()}
                className="rounded-lg bg-white/8 px-4 py-2 text-sm font-semibold text-ink ring-1 ring-inset ring-white/10 hover:bg-white/12"
              >
                Retry
              </button>
            }
          />
        </div>
      ) : (
        <>
          <Spotlight
            items={home.data?.spotlights ?? []}
            onOpen={openTitle}
            stateFor={tileState}
            canQuickRequest={quickRequestable}
            stateRevision={requestStateRevision}
            paused={modalOpen || searchOpen}
          />
          <div className="px-5 sm:px-8 lg:px-11">
            {(home.data?.rows ?? []).map((row) => (
              <Row
                key={row.row_type}
                title={row.title}
                items={row.items}
                onSelect={openTitle}
                tileState={tileState}
                quickRequestable={quickRequestable}
              />
            ))}
          </div>
        </>
      )}

      {/* Same lazy-mount as the search overlay: the modal runs its full
          request/queue hook surface before its own null guard, so don't mount
          it until a title has been selected. `selected` survives a close, so
          Radix stays mounted through its exit. */}
      {selected ? (
        <TitleDetailModal title={selected} open={modalOpen} onOpenChange={setModalOpen} />
      ) : null}
    </div>
  )
}
