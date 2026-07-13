import * as RadixDialog from '@radix-ui/react-dialog'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useDiscoverHome, useDiscoverSearch } from '../api/hooks'
import type { DiscoverResult } from '../api/types'
import { PosterCard } from './ui/PosterCard'
import { TileStatusGlyph } from './ui/TileStatusGlyph'
import { QuickRequestButton } from './QuickRequestButton'
import { CenteredSpinner, StateMessage } from './ui/feedback'
import { TitleDetailModal } from './TitleDetailModal'
import { useDiscoverTilePresentation } from './useDiscoverTilePresentation'

const SEARCH_DEBOUNCE_MS = 300

function useDebounced<T>(value: T, delayMs: number): [T, (nextValue: T) => void] {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(timer)
  }, [delayMs, value])

  return [debounced, setDebounced]
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return (
    target.isContentEditable ||
    target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') !==
      null
  )
}

function anotherDialogIsOpen(): boolean {
  return Array.from(
    document.querySelectorAll<HTMLElement>('[role="dialog"], [role="alertdialog"]'),
  ).some(
    (dialog) =>
      dialog.getAttribute('data-state') !== 'closed' &&
      dialog.getAttribute('aria-hidden') !== 'true',
  )
}

function popularTitles(home: ReturnType<typeof useDiscoverHome>['data']): DiscoverResult[] {
  const seen = new Set<string>()
  const titles: DiscoverResult[] = []

  for (const row of home?.rows ?? []) {
    if (row.row_type !== 'popular' && row.row_type !== 'popular_tv') continue
    for (const item of row.items) {
      const key = `${item.media_type}-${item.tmdb_id}`
      if (seen.has(key)) continue
      seen.add(key)
      titles.push(item)
    }
  }

  return titles
}

/** Global, accessible TMDB search mounted in the authenticated app header. */
export function SearchOverlay() {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<DiscoverResult | null>(null)
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [detailsTrigger, setDetailsTrigger] = useState<HTMLButtonElement | null>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const [debouncedQuery, setDebouncedQuery] = useDebounced(query, SEARCH_DEBOUNCE_MS)
  const trimmedQuery = query.trim()
  const trimmedDebouncedQuery = debouncedQuery.trim()
  const hasQuery = trimmedQuery.length > 0
  const queryReady = hasQuery && trimmedDebouncedQuery === trimmedQuery

  // Pass a non-empty query only after the raw value has remained stable for the
  // full debounce. Clearing/changing the input disables the prior query at once.
  const search = useDiscoverSearch(queryReady ? trimmedDebouncedQuery : '')
  // The overlay mounts on every authenticated route; only fetch the Discover
  // home feed (a TMDB fan-out) once the dialog is actually open.
  const home = useDiscoverHome({ enabled: open })
  const suggestions = useMemo(() => popularTitles(home.data), [home.data])
  const results = queryReady ? (search.data?.results ?? []) : []
  const activeDataUpdatedAt = hasQuery ? search.dataUpdatedAt : home.dataUpdatedAt
  // Same visibility gate: no /requests observer (Layout's badge already polls
  // that query) until tiles can actually render.
  const { tileState, quickRequestable } = useDiscoverTilePresentation(activeDataUpdatedAt, {
    enabled: open,
  })

  useEffect(() => {
    const openWithSlash = (event: KeyboardEvent) => {
      if (
        event.key !== '/' ||
        event.defaultPrevented ||
        event.repeat ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        isEditableTarget(event.target) ||
        isEditableTarget(document.activeElement) ||
        anotherDialogIsOpen()
      ) {
        return
      }

      event.preventDefault()
      setOpen(true)
    }

    document.addEventListener('keydown', openWithSlash)
    return () => document.removeEventListener('keydown', openWithSlash)
  }, [])

  const onSearchOpenChange = (nextOpen: boolean) => {
    setOpen(nextOpen)
    if (nextOpen) return
    setQuery('')
    setDebouncedQuery('')
    setSelected(null)
    setDetailsOpen(false)
    setDetailsTrigger(null)
  }

  const openTitle = (title: DiscoverResult, trigger: HTMLButtonElement) => {
    setDetailsTrigger(trigger)
    setSelected(title)
    setDetailsOpen(true)
  }

  let announcement: string
  if (!hasQuery) {
    if (home.isLoading) announcement = 'Loading popular titles.'
    else if (home.isError) announcement = 'Popular titles failed to load.'
    else announcement = `${suggestions.length} popular ${suggestions.length === 1 ? 'title' : 'titles'}.`
  } else if (!queryReady || (search.isFetching && results.length === 0)) {
    announcement = 'Searching.'
  } else if (results.length > 0) {
    // A failed refetch keeps the last good data; say so instead of presenting
    // stale results as current.
    announcement = search.isError
      ? `Couldn’t update results for ${trimmedDebouncedQuery}. Showing ${results.length} earlier ${results.length === 1 ? 'result' : 'results'}.`
      : `${results.length} ${results.length === 1 ? 'result' : 'results'} for ${trimmedDebouncedQuery}.`
  } else if (search.isError) {
    announcement = 'Search failed.'
  } else {
    announcement = `No matches for ${trimmedDebouncedQuery}.`
  }

  const renderCards = (items: DiscoverResult[]) => (
    <div className="grid grid-cols-[repeat(auto-fill,minmax(112px,1fr))] gap-x-3 gap-y-5 sm:grid-cols-[repeat(auto-fill,minmax(142px,1fr))] sm:gap-x-4 sm:gap-y-6">
      {items.map((item) => {
        const state = tileState(item)
        return (
          <PosterCard
            key={`${item.media_type}-${item.tmdb_id}`}
            title={item.title}
            year={item.year ?? null}
            posterUrl={item.poster_url ?? null}
            seed={item.tmdb_id}
            onClick={(trigger) => openTitle(item, trigger)}
            badge={state ? <TileStatusGlyph status={state} /> : undefined}
            action={
              state === null && quickRequestable(item) ? (
                <QuickRequestButton item={item} />
              ) : undefined
            }
          />
        )
      })}
    </div>
  )

  return (
    <RadixDialog.Root open={open} onOpenChange={onSearchOpenChange}>
      <RadixDialog.Trigger asChild>
        <button
          ref={triggerRef}
          type="button"
          aria-label="Search TMDB to request"
          aria-haspopup="dialog"
          className="flex size-10 shrink-0 items-center justify-center gap-2 rounded-full bg-white/6 text-faint ring-1 ring-inset ring-white/10 transition-colors hover:bg-white/10 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60 lg:h-9 lg:w-[13.25rem] lg:justify-start lg:px-3"
        >
          <SearchIcon className="size-4 shrink-0" />
          <span className="hidden min-w-0 flex-1 truncate text-left text-[13px] font-medium lg:block">
            Search TMDB to request…
          </span>
          <kbd className="hidden shrink-0 rounded-md bg-black/25 px-1.5 py-0.5 font-mono text-[10px] text-faint ring-1 ring-inset ring-white/10 lg:inline-flex">
            /
          </kbd>
        </button>
      </RadixDialog.Trigger>

      <RadixDialog.Portal>
        <RadixDialog.Overlay className="fixed inset-0 z-50 bg-black/85" />
        <RadixDialog.Content
          className="fixed inset-0 z-50 flex h-dvh flex-col bg-bg/[0.98] text-ink outline-none"
          onOpenAutoFocus={(event) => {
            event.preventDefault()
            inputRef.current?.focus()
            inputRef.current?.select()
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault()
            triggerRef.current?.focus()
          }}
        >
          <RadixDialog.Title className="sr-only">Search TMDB</RadixDialog.Title>
          <RadixDialog.Description className="sr-only">
            Search for a movie or TV show to view details and request it.
          </RadixDialog.Description>

          <div className="shrink-0 border-b border-hairline bg-bg/95 px-4 sm:px-8 lg:px-11">
            <div className="mx-auto flex w-full max-w-[100rem] items-center gap-3 py-4 sm:gap-4 sm:py-5">
              <div className="relative min-w-0 flex-1">
                <SearchIcon className="pointer-events-none absolute top-1/2 left-4 size-5 -translate-y-1/2 text-faint sm:left-5 sm:size-6" />
                <input
                  ref={inputRef}
                  type="search"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  aria-label="Search TMDB"
                  placeholder="Search TMDB to request…"
                  autoComplete="off"
                  className="h-14 w-full rounded-2xl bg-surface pr-4 pl-12 text-lg text-ink ring-1 ring-inset ring-white/10 outline-none placeholder:text-faint focus-visible:ring-2 focus-visible:ring-gold/50 sm:h-16 sm:pr-5 sm:pl-14 sm:text-[26px]"
                />
              </div>
              <kbd className="hidden font-mono text-[11px] tracking-wide text-faint sm:block">
                ESC
              </kbd>
              <RadixDialog.Close asChild>
                <button
                  type="button"
                  aria-label="Close search"
                  className="flex size-11 shrink-0 items-center justify-center rounded-full bg-white/7 text-muted ring-1 ring-inset ring-white/10 transition-colors hover:bg-white/12 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60"
                >
                  <CloseIcon />
                </button>
              </RadixDialog.Close>
            </div>
          </div>

          <p aria-live="polite" aria-atomic="true" className="sr-only">
            {announcement}
          </p>

          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-8 sm:py-8 lg:px-11">
            <div className="mx-auto w-full max-w-[100rem]">
              {!hasQuery ? (
                <section aria-labelledby="popular-search-heading">
                  <h2
                    id="popular-search-heading"
                    className="mb-5 font-display text-xl font-extrabold text-ink sm:text-2xl"
                  >
                    Popular to request
                  </h2>
                  {home.isLoading ? (
                    <CenteredSpinner label="Loading popular titles…" />
                  ) : home.isError ? (
                    <StateMessage
                      tone="error"
                      title="Couldn’t load popular titles"
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
                  ) : suggestions.length === 0 ? (
                    <StateMessage
                      title="No popular titles"
                      message="Popular movies and shows will appear here when they’re available."
                    />
                  ) : (
                    renderCards(suggestions)
                  )}
                </section>
              ) : !queryReady || (search.isFetching && results.length === 0) ? (
                <CenteredSpinner label="Searching…" />
              ) : results.length > 0 ? (
                <section aria-labelledby="search-results-heading">
                  <div className="mb-5 flex items-baseline justify-between gap-4">
                    <h2
                      id="search-results-heading"
                      className="font-display text-xl font-extrabold text-ink sm:text-2xl"
                    >
                      {results.length} {results.length === 1 ? 'result' : 'results'} for “
                      {trimmedDebouncedQuery}”
                    </h2>
                    {search.isFetching ? (
                      <span role="status" className="shrink-0 font-mono text-xs text-faint">
                        Updating…
                      </span>
                    ) : search.isError ? (
                      // TanStack Query keeps the last good `data` when a refetch
                      // fails, so these results are stale. Never present them as
                      // current: surface the failure and a retry inline.
                      <span className="flex shrink-0 items-center gap-3 font-mono text-xs text-error">
                        Couldn’t update — showing earlier results
                        <button
                          type="button"
                          onClick={() => void search.refetch()}
                          className="rounded-md bg-white/8 px-2.5 py-1 font-sans text-xs font-semibold text-ink ring-1 ring-inset ring-white/10 hover:bg-white/12 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gold/60"
                        >
                          Retry
                        </button>
                      </span>
                    ) : null}
                  </div>
                  {renderCards(results)}
                </section>
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
              ) : (
                <StateMessage
                  title="No matches"
                  message={`Nothing on TMDB matched “${trimmedDebouncedQuery}”.`}
                />
              )}
            </div>
          </div>

          {/* Mount the details modal only once a title has been selected: it
              calls its full request/queue hook surface before its own null
              guard, which would otherwise fire hidden fetches (an admin /queue
              GET among them) whenever the overlay is open. `selected` survives
              a details close, so Radix stays mounted through the close and the
              returnFocusTo handoff below still runs. */}
          {selected ? (
            <TitleDetailModal
              title={selected}
              open={detailsOpen}
              onOpenChange={setDetailsOpen}
              returnFocusTo={() =>
                detailsTrigger?.isConnected ? detailsTrigger : inputRef.current
              }
            />
          ) : null}
        </RadixDialog.Content>
      </RadixDialog.Portal>
    </RadixDialog.Root>
  )
}

function SearchIcon({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden
      className={className}
    >
      <circle cx="11" cy="11" r="7" />
      <path d="m20 20-4-4" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden
      className="size-5"
    >
      <path d="m6 6 12 12M18 6 6 18" />
    </svg>
  )
}
