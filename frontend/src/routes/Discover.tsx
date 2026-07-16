import { useMemo, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { useDiscoverHome } from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { Row } from '../components/Row'
import { Spotlight } from '../components/Spotlight'
import { TitleDetailModal } from '../components/TitleDetailModal'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { useDiscoverTilePresentation } from '../components/useDiscoverTilePresentation'

function createLoadId(): string {
  const cryptoApi = typeof globalThis.crypto === 'undefined' ? undefined : globalThis.crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID()
  }

  const bytes = new Uint8Array(16)
  if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    cryptoApi.getRandomValues(bytes)
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256)
    }
  }
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function Discover() {
  const [loadId] = useState(createLoadId)
  const home = useDiscoverHome({ loadId })
  // Every tile currently visible on this page — the compact live-state poll's
  // key set (issue #370 phase 2). Recomputed only when the underlying data
  // changes, not on every render, so the poll's query key stays stable.
  const visibleItems = useMemo<DiscoverResult[]>(
    () => [
      ...(home.data?.spotlights ?? []),
      ...(home.data?.rows ?? []).flatMap((row) => row.items),
    ],
    [home.data],
  )
  const { tileState, quickRequestable, requestStateRevision } = useDiscoverTilePresentation(
    visibleItems,
    home.dataUpdatedAt,
  )
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
                subtitle={row.subtitle ?? undefined}
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
