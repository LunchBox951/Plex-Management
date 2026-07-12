import { useState } from 'react'
import { useDiscoverHome } from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { Row } from '../components/Row'
import { Spotlight } from '../components/Spotlight'
import { TitleDetailModal } from '../components/TitleDetailModal'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useDiscoverTilePresentation } from '../components/useDiscoverTilePresentation'

export function Discover() {
  const home = useDiscoverHome()
  const { tileState, quickRequestable } = useDiscoverTilePresentation(home.dataUpdatedAt)
  const [selected, setSelected] = useState<DiscoverResult | null>(null)
  const [modalOpen, setModalOpen] = useState(false)

  const openTitle = (title: DiscoverResult) => {
    setSelected(title)
    setModalOpen(true)
  }

  return (
    <div className="w-full px-5 py-8 sm:px-8 lg:px-11">
      {home.isLoading ? (
        <CenteredSpinner label="Loading Discover…" />
      ) : home.isError ? (
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
      ) : (
        <>
          <Spotlight
            item={home.data?.spotlight ?? null}
            onOpen={openTitle}
            state={home.data?.spotlight ? tileState(home.data.spotlight) : null}
          />
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
        </>
      )}

      <TitleDetailModal title={selected} open={modalOpen} onOpenChange={setModalOpen} />
    </div>
  )
}
