import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
} from 'react'
import { useCreateRequest } from '../api/hooks'
import type { CreateRequestBody, DiscoverResult } from '../api/types'
import type { ApiError } from '../lib/errors'
import { PLEX_WEB_APP_URL } from '../lib/plex'
import { requestStatus, type StatusPresentation } from '../lib/status'
import { Button } from './ui/Button'
import { buttonClasses } from './ui/button-variants'
import { StatusBadge } from './ui/StatusBadge'
import { useToast } from './ui/toast'

const ROTATION_DELAY_MS = 6_500
const FADE_DURATION_MS = 300

interface SpotlightProps {
  items: DiscoverResult[]
  stateFor: (item: DiscoverResult) => StatusPresentation | null
  canQuickRequest: (item: DiscoverResult) => boolean
  onOpen: (item: DiscoverResult) => void
  /** Positive revision of the last settled request snapshot; zero while stale. */
  stateRevision?: number
  /** Pauses for UI outside the carousel, such as title/search dialogs. */
  paused?: boolean
}

function itemKey(item: DiscoverResult): string {
  return `${item.media_type}:${item.tmdb_id}`
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(() =>
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false,
  )

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    const update = () => setReduced(query.matches)
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  return reduced
}

function asApiError(error: unknown): ApiError {
  return error as ApiError
}

/**
 * Server-composed featured-title carousel.
 *
 * Rotation is deliberately local presentation state; title selection, request
 * state, request safety, and feed composition remain owned by their existing
 * server/query paths. A home refresh preserves the active media key whenever it
 * still exists, even if the server returns fresh item objects or a new order.
 */
export function Spotlight({
  items,
  stateFor,
  canQuickRequest,
  onOpen,
  stateRevision = 0,
  paused = false,
}: SpotlightProps) {
  const { toast } = useToast()
  const createRequest = useCreateRequest()
  const reducedMotion = usePrefersReducedMotion()
  const keys = useMemo(() => items.map(itemKey), [items])
  const keysSignature = keys.join('|')
  const [activeKey, setActiveKey] = useState(() => keys[0] ?? '')
  const [previousKeysSignature, setPreviousKeysSignature] = useState(keysSignature)
  const [transitionFrom, setTransitionFrom] = useState<DiscoverResult | null>(null)
  const [manualPaused, setManualPaused] = useState(false)
  const [pointerInside, setPointerInside] = useState(false)
  const [focusInside, setFocusInside] = useState(false)
  const [documentHidden, setDocumentHidden] = useState(() => document.hidden)
  const [dwellReset, setDwellReset] = useState(0)
  const [localRequestPending, setLocalRequestPending] = useState(false)
  const [immediateStates, setImmediateStates] = useState<
    Record<string, { presentation: StatusPresentation; requestRevision: number }>
  >({})
  const rotationTimer = useRef<number | null>(null)
  const fadeTimer = useRef<number | null>(null)
  const requestInFlight = useRef(false)
  const latestSettledRevision = useRef(stateRevision)

  // React's supported "adjust state while rendering" pattern keeps a removed
  // active key from unexpectedly returning on a later feed refresh. The guarded
  // signature update causes at most one immediate rerender per changed key list.
  if (previousKeysSignature !== keysSignature) {
    setPreviousKeysSignature(keysSignature)
    if (transitionFrom) setTransitionFrom(null)
    if (!keys.includes(activeKey)) {
      setActiveKey(keys[0] ?? '')
    }
  }

  if (reducedMotion && transitionFrom) setTransitionFrom(null)

  const activeIndex = Math.max(0, keys.indexOf(activeKey))
  const activeItem = items[activeIndex] ?? null
  const resolvedActiveKey = activeItem ? itemKey(activeItem) : ''
  const derivedState = activeItem ? stateFor(activeItem) : null
  const immediateState = immediateStates[resolvedActiveKey]
  const activeState = activeItem
    ? (derivedState ?? immediateState?.presentation ?? null)
    : null

  // Retire the mutation-response bridge as soon as the shared request query has
  // acknowledged this title. Guarding on map membership makes this a one-render
  // state adjustment rather than an effect/render cascade.
  const requestRefetchAcknowledged =
    immediateState !== undefined &&
    stateRevision > 0 &&
    stateRevision !== immediateState.requestRevision
  if (
    activeItem &&
    immediateState !== undefined &&
    (derivedState !== null || requestRefetchAcknowledged)
  ) {
    const next = { ...immediateStates }
    delete next[resolvedActiveKey]
    setImmediateStates(next)
  }

  const clearFadeTimer = useCallback(() => {
    if (fadeTimer.current !== null) {
      window.clearTimeout(fadeTimer.current)
      fadeTimer.current = null
    }
  }, [])

  const transitionTo = useCallback(
    (nextKey: string) => {
      if (!activeItem || nextKey === resolvedActiveKey || !keys.includes(nextKey)) return
      clearFadeTimer()
      if (reducedMotion) {
        setTransitionFrom(null)
        setActiveKey(nextKey)
        return
      }

      setTransitionFrom(activeItem)
      setActiveKey(nextKey)
    },
    [activeItem, clearFadeTimer, keys, reducedMotion, resolvedActiveKey],
  )
  const transitionToRef = useRef(transitionTo)

  useEffect(() => {
    transitionToRef.current = transitionTo
  }, [transitionTo])

  useEffect(() => {
    if (stateRevision > 0) latestSettledRevision.current = stateRevision
  }, [stateRevision])

  useEffect(() => {
    if (!transitionFrom || reducedMotion) return
    fadeTimer.current = window.setTimeout(() => {
      fadeTimer.current = null
      setTransitionFrom(null)
    }, FADE_DURATION_MS)
    return clearFadeTimer
  }, [clearFadeTimer, reducedMotion, transitionFrom])

  useEffect(() => {
    const onVisibilityChange = () => setDocumentHidden(document.hidden)
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [])

  const rotationPaused =
    paused ||
    manualPaused ||
    pointerInside ||
    focusInside ||
    documentHidden ||
    localRequestPending ||
    createRequest.isPending ||
    reducedMotion

  // A timeout is scheduled afresh after every slide/pause/manual-dot change.
  // Request-state polling does not participate in these dependencies, otherwise
  // its five-second cadence could starve a 6.5-second carousel indefinitely.
  useEffect(() => {
    if (rotationTimer.current !== null) {
      window.clearTimeout(rotationTimer.current)
      rotationTimer.current = null
    }
    if (keys.length < 2 || rotationPaused || !resolvedActiveKey) return

    rotationTimer.current = window.setTimeout(() => {
      rotationTimer.current = null
      const currentIndex = keys.indexOf(resolvedActiveKey)
      const nextIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % keys.length
      const nextKey = keys[nextIndex] ?? keys[0]
      if (nextKey) transitionToRef.current(nextKey)
    }, ROTATION_DELAY_MS)

    return () => {
      if (rotationTimer.current !== null) {
        window.clearTimeout(rotationTimer.current)
        rotationTimer.current = null
      }
    }
  }, [dwellReset, keys, keysSignature, resolvedActiveKey, rotationPaused])

  useEffect(
    () => () => {
      if (rotationTimer.current !== null) window.clearTimeout(rotationTimer.current)
      clearFadeTimer()
    },
    [clearFadeTimer],
  )

  const selectDot = (nextKey: string) => {
    setDwellReset((value) => value + 1)
    transitionTo(nextKey)
  }

  const leaveFocus = (event: FocusEvent<HTMLElement>) => {
    const next = event.relatedTarget
    if (!(next instanceof Node) || !event.currentTarget.contains(next)) {
      setFocusInside(false)
    }
  }

  const requestActive = async () => {
    if (!activeItem || requestInFlight.current || localRequestPending || createRequest.isPending) {
      return
    }

    // A season-less TV POST means whole aired series. When freshness/history
    // makes that unsafe, the same visible CTA opens Details for season choice.
    if (!canQuickRequest(activeItem)) {
      onOpen(activeItem)
      return
    }

    const requestedItem = activeItem
    const key = itemKey(requestedItem)
    const body: CreateRequestBody = {
      tmdb_id: requestedItem.tmdb_id,
      media_type: requestedItem.media_type,
    }
    requestInFlight.current = true
    setLocalRequestPending(true)
    try {
      const response = await createRequest.mutateAsync(body)
      setImmediateStates((current) => ({
        ...current,
        [key]: {
          presentation: requestStatus(response.status),
          requestRevision: latestSettledRevision.current,
        },
      }))
      toast({ title: `Requested ${requestedItem.title}`, intent: 'success' })
    } catch (error) {
      toast({
        title: 'Request failed',
        description: asApiError(error).message,
        intent: 'error',
      })
    } finally {
      requestInFlight.current = false
      setLocalRequestPending(false)
    }
  }

  if (!activeItem) return null

  const visibleTransitionFrom = reducedMotion ? null : transitionFrom

  return (
    <section
      aria-label="Featured titles"
      aria-roledescription="carousel"
      className="relative mb-10 h-[520px] w-full overflow-hidden bg-bg sm:h-[600px] lg:h-[680px]"
      onPointerEnter={() => setPointerInside(true)}
      onPointerLeave={() => setPointerInside(false)}
      onFocusCapture={() => setFocusInside(true)}
      onBlurCapture={leaveFocus}
    >
      {visibleTransitionFrom ? (
        <SpotlightSlide
          key={`outgoing-${itemKey(visibleTransitionFrom)}`}
          item={visibleTransitionFrom}
          state={stateFor(visibleTransitionFrom)}
          className="spotlight-fade-out"
          inert
        />
      ) : null}
      <SpotlightSlide
        key={resolvedActiveKey}
        item={activeItem}
        state={activeState}
        className={visibleTransitionFrom ? 'spotlight-fade-in' : undefined}
        requestPending={localRequestPending || createRequest.isPending}
        onRequest={() => void requestActive()}
        onOpen={() => onOpen(activeItem)}
      />

      {items.length > 1 ? (
        <div className="absolute inset-x-0 bottom-3 z-30 flex items-center justify-center gap-0.5">
          {items.map((item, index) => {
            const key = itemKey(item)
            const current = key === resolvedActiveKey
            return (
              <button
                key={key}
                type="button"
                aria-label={`Show ${index + 1} of ${items.length}: ${item.title}`}
                aria-current={current ? 'true' : undefined}
                onClick={() => selectDot(key)}
                className="group/dot flex size-6 items-center justify-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-gold/70"
              >
                <span
                  aria-hidden
                  className={`size-1 rounded-full transition-colors ${
                    current ? 'bg-gold' : 'bg-white/40 group-hover/dot:bg-white/70'
                  }`}
                />
              </button>
            )
          })}
        </div>
      ) : null}

      {!reducedMotion && items.length > 1 ? (
        <button
          type="button"
          aria-label={`${manualPaused ? 'Play' : 'Pause'} spotlight rotation`}
          aria-pressed={manualPaused}
          onClick={() => setManualPaused((value) => !value)}
          className="absolute right-5 bottom-3 z-40 flex h-7 items-center rounded-full bg-black/35 px-2.5 font-mono text-[10px] font-semibold tracking-wide text-muted ring-1 ring-inset ring-white/15 backdrop-blur-sm outline-none hover:bg-black/55 hover:text-ink focus-visible:ring-2 focus-visible:ring-gold/70 sm:right-8 lg:right-11"
        >
          {manualPaused ? 'Play' : 'Pause'}
        </button>
      ) : null}
    </section>
  )
}

interface SpotlightSlideProps {
  item: DiscoverResult
  state: StatusPresentation | null
  className?: string | undefined
  inert?: boolean
  requestPending?: boolean
  onRequest?: () => void
  onOpen?: () => void
}

function SpotlightSlide({
  item,
  state,
  className,
  inert = false,
  requestPending = false,
  onRequest,
  onOpen,
}: SpotlightSlideProps) {
  const meta = [item.media_type === 'tv' ? 'TV' : 'Movie', item.year]
    .filter((value): value is string | number => value !== null && value !== undefined)
    .join(' · ')

  return (
    <div
      className={`absolute inset-0 ${className ?? ''}`}
      aria-hidden={inert || undefined}
      // Outgoing slides are visual transition residue only and can never retain
      // focus or pointer targeting while the active slide changes beneath them.
      {...(inert ? { inert: true } : {})}
    >
      {item.backdrop_url ? (
        <div
          className="absolute inset-0 bg-cover bg-center"
          style={{ backgroundImage: `url(${item.backdrop_url})` }}
          aria-hidden
        />
      ) : (
        <div
          data-testid="spotlight-art-fallback"
          className="absolute inset-0 bg-gradient-to-br from-surface via-poster to-bg"
          aria-hidden
        />
      )}

      <div
        data-testid="spotlight-bottom-fade"
        className="spotlight-bottom-fade absolute inset-0"
        aria-hidden
      />
      <div
        data-testid="spotlight-side-fade"
        className="spotlight-side-fade absolute inset-0"
        aria-hidden
      />
      <div className="spotlight-radial-scrim absolute inset-0" aria-hidden />

      <div className="relative flex h-full items-end px-5 pb-12 sm:px-8 sm:pb-14 lg:px-11">
        <div className="w-full max-w-[600px]">
          <div className="spotlight-copy-shadow">
            <div className="flex items-center gap-3 font-mono text-xs tracking-wide text-gold">
              <span>{meta}</span>
            </div>
            {inert ? (
              <div className="mt-2 font-display text-4xl leading-[1.02] font-extrabold text-ink sm:text-5xl lg:text-6xl">
                {item.title}
              </div>
            ) : (
              <h1 className="mt-2 font-display text-4xl leading-[1.02] font-extrabold text-ink sm:text-5xl lg:text-6xl">
                {item.title}
              </h1>
            )}
            {item.overview ? (
              <p className="mt-3 line-clamp-3 max-w-xl text-sm leading-relaxed text-muted sm:text-[15px]">
                {item.overview}
              </p>
            ) : null}
          </div>

          {!inert ? (
            <div className="mt-5 flex flex-wrap items-center gap-2.5">
              {state === null ? (
                <Button loading={requestPending} onClick={onRequest}>
                  + Request
                </Button>
              ) : state.intent === 'available' ? (
                <a
                  href={PLEX_WEB_APP_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className={buttonClasses()}
                >
                  Open in Plex ↗
                  <span className="sr-only"> opens in a new tab</span>
                </a>
              ) : (
                <StatusBadge status={state} className="min-h-8 px-3" />
              )}
              <Button variant="secondary" onClick={onOpen}>
                Details
              </Button>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
