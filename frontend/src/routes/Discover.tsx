import { useEffect, useState } from 'react'
import { useDiscoverHome, useDiscoverSearch } from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { PosterCard } from '../components/ui/PosterCard'
import { CenteredSpinner, StateMessage } from '../components/ui/feedback'
import { TitleDetailModal } from '../components/TitleDetailModal'
import { Row } from '../components/Row'
import { Spotlight } from '../components/Spotlight'

/** Debounce a value so we don't fire a TMDB search on every keystroke. */
function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delayMs)
    return () => clearTimeout(id)
  }, [value, delayMs])
  return debounced
}

export function Discover() {
  const [query, setQuery] = useState('')
  const debounced = useDebounced(query, 300)
  const hasQuery = debounced.trim().length > 0

  const search = useDiscoverSearch(debounced)
  const home = useDiscoverHome()

  // One selected-title + modal state, shared across the home and search branches.
  const [selected, setSelected] = useState<DiscoverResult | null>(null)
  const [modalOpen, setModalOpen] = useState(false)

  const openTitle = (title: DiscoverResult) => {
    setSelected(title)
    setModalOpen(true)
  }

  const results = search.data?.results ?? []

  return (
    <div>
      <div className="mb-8">
        <h1 className="font-display text-3xl font-extrabold">Discover</h1>
        <p className="mt-1 text-muted">Search TMDB to request a movie or show.</p>
        <div className="mt-5">
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search for a title…"
            aria-label="Search TMDB"
            autoFocus
            className="h-12 w-full rounded-xl bg-surface px-4 text-base text-ink ring-1 ring-inset ring-white/10 outline-none placeholder:text-faint focus-visible:ring-2 focus-visible:ring-gold/50"
          />
        </div>
      </div>

      {!hasQuery ? (
        home.isLoading ? (
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
            <Spotlight item={home.data?.spotlight ?? null} onOpen={openTitle} />
            {(home.data?.rows ?? []).map((row) => (
              <Row
                key={row.row_type}
                title={row.title}
                items={row.items}
                onSelect={openTitle}
              />
            ))}
          </>
        )
      ) : search.isError ? (
        <StateMessage
          tone="error"
          title="Search failed"
          message={search.error.message}
          action={
            <button
              type="button"
              onClick={() => void search.refetch()}
              className="rounded-lg bg-white/8 px-4 py-2 text-sm font-semibold text-ink ring-1 ring-inset ring-white/10 hover:bg-white/12"
            >
              Retry
            </button>
          }
        />
      ) : search.isFetching && results.length === 0 ? (
        <CenteredSpinner label="Searching…" />
      ) : results.length === 0 ? (
        <StateMessage title="No matches" message={`Nothing on TMDB matched “${debounced}”.`} />
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(140px,1fr))] gap-x-4 gap-y-5">
          {results.map((title) => (
            <PosterCard
              key={`${title.media_type}-${title.tmdb_id}`}
              title={title.title}
              year={title.year ?? null}
              posterUrl={title.poster_url ?? null}
              seed={title.tmdb_id}
              onClick={() => openTitle(title)}
            />
          ))}
        </div>
      )}

      <TitleDetailModal title={selected} open={modalOpen} onOpenChange={setModalOpen} />
    </div>
  )
}
