import { useRef, useState, type ReactNode } from 'react'
import { useCreateRequest, useTvSeasonEpisodes, useTvSeasons } from '../api/hooks'
import type { CreateRequestBody, RequestResponse, TvEpisodeItem } from '../api/types'
import { cn } from '../lib/cn'
import { isApiError, isNotImplemented, toApiError } from '../lib/errors'
import {
  TV_CHOICES,
  alreadyRequestedFrom,
  buildTvRequestBody,
  defaultTvChoice,
  describeTvChoice,
  episodeKey,
  formatAirDate,
  formatRuntime,
  formatSeasonList,
  formatShortAirDate,
  hasAired,
  isEpisodeRequested,
  type AlreadyRequested,
  type EpisodePicks,
  type TvChoice,
} from '../lib/tvRequest'
import { useIsPhone } from '../lib/useMediaQuery'
import { Button } from './ui/Button'
import { CenteredSpinner } from './ui/feedback'
import { NotAvailableYet } from './ui/NotAvailableYet'
import { Sheet } from './ui/Sheet'
import { StatusBadge } from './ui/StatusBadge'
import { useToast } from './ui/toast'

export interface TvRequestPickerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  tmdbId: number
  /** The show's name, for the sheet title and the success toast. */
  title: string
  /**
   * The show's existing request ("Request more"): what it already covers is
   * shown checked and locked, and only NEW picks are sent (the backend merges
   * them into the same request).
   */
  existingRequest?: RequestResponse | null | undefined
  /** Fires after a successful create, once the sheet has closed, with the body that was sent. */
  onRequested?: ((created: RequestResponse, body: CreateRequestBody) => void) | undefined
}

/** One-line explanations under each choice (phones show the selected one's inline). */
const CHOICE_HINTS: Record<TvChoice, string> = {
  latest_season:
    "Every episode of the newest season. Episodes that haven't aired yet are grabbed when they do.",
  new_episodes: 'Nothing from before today. Each new episode is grabbed after it airs.',
  episodes: 'Pick single episodes, season by season.',
  seasons: 'Pick whole seasons.',
  every: 'The whole show, every season.',
}

/** The honest 501 copy per choice: the backend answers `not_implemented` for both tv_mode values today. */
const NOT_AVAILABLE_COPY: Record<TvChoice, { title: string; message: string }> = {
  latest_season: {
    title: "Requesting only the latest season isn't available yet.",
    message: 'Use Choose seasons and tick the newest season instead.',
  },
  new_episodes: {
    title: "Getting new episodes as they air isn't available yet.",
    message: 'Use Choose episodes or Choose seasons for now.',
  },
  episodes: {
    title: "Requesting single episodes isn't available yet.",
    message: 'Use Choose seasons for now.',
  },
  seasons: {
    title: "Requesting chosen seasons isn't available yet.",
    message: 'Use Every season for now.',
  },
  every: {
    title: "Requesting this show isn't available yet.",
    message: 'Try again later.',
  },
}

const IN_PLEX = { label: 'In Plex', intent: 'available' } as const
const REQUESTED = { label: 'Requested', intent: 'neutral' } as const

/**
 * The TV request sheet: opened by Request (and Request more) on any TV title,
 * it lets someone ask for exactly the part of a show they want — the latest
 * season, new episodes from now on, chosen episodes, chosen seasons, or every
 * season. Season tabs and the footer (a plain-words restatement plus the
 * Request button) stay put while the list between them scrolls.
 *
 * Requesting is reversible, so the button acts at once: success closes the
 * sheet with a toast; a failure stays open and says why above the buttons.
 */
export function TvRequestPicker(props: TvRequestPickerProps) {
  // Mounted only while open, so every opening starts from a clean slate (the
  // default choice is re-derived and old picks never leak into a new visit).
  if (!props.open) return null
  return <TvRequestSheet {...props} />
}

function TvRequestSheet({
  open,
  onOpenChange,
  tmdbId,
  title,
  existingRequest,
  onRequested,
}: TvRequestPickerProps) {
  const { toast } = useToast()
  const isPhone = useIsPhone()
  const createRequest = useCreateRequest()
  const seasonsQuery = useTvSeasons(tmdbId)

  // Ascending from the API; tabs and the season checklist show newest first.
  const seasons = seasonsQuery.data?.seasons ?? []
  const newestFirst = [...seasons].sort((a, b) => b - a)
  const latestSeason = newestFirst[0] ?? null
  // The newest season drives the default choice and both automatic choices' copy.
  const latestEpisodesQuery = useTvSeasonEpisodes(tmdbId, latestSeason)
  const latestEpisodes = latestEpisodesQuery.data?.episodes

  const [pickedChoice, setPickedChoice] = useState<TvChoice | null>(null)
  const [tabSeason, setTabSeason] = useState<number | null>(null)
  const [episodePicks, setEpisodePicks] = useState<EpisodePicks>({})
  const [seasonPicks, setSeasonPicks] = useState<number[]>([])
  const [failure, setFailure] = useState<{ choice: TvChoice; error: unknown } | null>(null)
  const listTop = useRef<HTMLDivElement>(null)

  const already = alreadyRequestedFrom(existingRequest)

  // Until the user picks, the default follows the show: Latest season while it
  // airs, Every season once it ended. `null` while that's still being worked out.
  const defaultChoice: TvChoice | null = seasonsQuery.isSuccess
    ? latestSeason === null || latestEpisodesQuery.isError
      ? 'every'
      : latestEpisodes
        ? defaultTvChoice(latestEpisodes)
        : null
    : null
  const choice = pickedChoice ?? defaultChoice

  const activeTab = tabSeason ?? latestSeason
  const tabEpisodesQuery = useTvSeasonEpisodes(
    tmdbId,
    choice === 'episodes' ? activeTab : null,
  )
  const tabEpisodes = tabEpisodesQuery.data?.episodes ?? []

  const input = { choice: choice ?? 'every', latestSeason, episodePicks, seasonPicks }
  const words = describeTvChoice(input)
  const nothingToAdd = already.wholeShow
  const canSubmit = choice !== null && words.ready && !nothingToAdd && !createRequest.isPending

  const selectChoice = (next: TvChoice) => {
    setPickedChoice(next)
    setFailure(null)
  }

  const toggleEpisode = (season: number, episode: TvEpisodeItem) => {
    const key = episodeKey(season, episode.episode_number)
    setFailure(null)
    setEpisodePicks((prev) => {
      if (prev[key]) {
        const rest = { ...prev }
        delete rest[key]
        return rest
      }
      return {
        ...prev,
        [key]: {
          season,
          episode: episode.episode_number,
          unaired: !hasAired(episode.air_date),
        },
      }
    })
  }

  const selectAllAired = (season: number) => {
    setFailure(null)
    setEpisodePicks((prev) => {
      const next = { ...prev }
      for (const episode of tabEpisodes) {
        if (!hasAired(episode.air_date) || isEpisodeLocked(already, season, episode)) continue
        next[episodeKey(season, episode.episode_number)] = {
          season,
          episode: episode.episode_number,
          unaired: false,
        }
      }
      return next
    })
  }

  const toggleSeason = (season: number) => {
    setFailure(null)
    setSeasonPicks((prev) =>
      prev.includes(season) ? prev.filter((s) => s !== season) : [...prev, season],
    )
  }

  const showTab = (season: number) => {
    setTabSeason(season)
    // A new season's list starts at the top, not wherever the last one was.
    // (jsdom has no scrollIntoView, hence the guard.)
    const top = listTop.current
    if (top && typeof top.scrollIntoView === 'function') top.scrollIntoView({ block: 'start' })
  }

  const submit = async () => {
    const submitted = choice
    if (!canSubmit || submitted === null) return
    setFailure(null)
    const body: CreateRequestBody = {
      tmdb_id: tmdbId,
      media_type: 'tv',
      ...buildTvRequestBody(input),
    }
    try {
      const created = await createRequest.mutateAsync(body)
      toast({ title: `Requested ${title}`, description: words.toast, intent: 'success' })
      onOpenChange(false)
      onRequested?.(created, body)
    } catch (error) {
      setFailure({ choice: submitted, error })
    }
  }

  const pickedInSeason = (season: number) =>
    Object.values(episodePicks).filter((p) => p.season === season).length

  // --- header: the five choices, plus the season tabs while choosing episodes ---
  const choiceControls = (
    <fieldset className="min-w-0">
      <legend className="sr-only">What to request</legend>
      <div
        className={cn(
          isPhone ? 'flex flex-col gap-1.5' : 'flex flex-wrap gap-1.5',
          'pt-1 pb-3',
        )}
      >
        {TV_CHOICES.map((option) => {
          const selected = choice === option.value
          return (
            <label
              key={option.value}
              className={cn(
                'relative cursor-pointer outline-none has-[:focus-visible]:ring-2 has-[:focus-visible]:ring-gold/60',
                isPhone
                  ? 'flex min-h-11 items-start gap-3 rounded-[10px] bg-surface-deep px-3 py-2.5'
                  : 'inline-flex h-[34px] items-center rounded-full px-3.5 text-[13px] font-semibold',
                isPhone
                  ? selected
                    ? 'ring-2 ring-gold ring-inset'
                    : ''
                  : selected
                    ? 'bg-gold text-gold-ink'
                    : 'text-muted ring-1 ring-white/12 ring-inset hover:text-ink',
              )}
            >
              <input
                type="radio"
                name="tv-request-choice"
                value={option.value}
                checked={selected}
                onChange={() => selectChoice(option.value)}
                className={isPhone ? 'mt-0.5 size-4 shrink-0 accent-gold' : 'sr-only'}
              />
              {isPhone ? (
                <span className="min-w-0">
                  <span className="block text-sm leading-5 font-semibold text-ink">
                    {option.label}
                  </span>
                  {selected ? (
                    <span className="block text-xs leading-[17px] text-faint">
                      {CHOICE_HINTS[option.value]}
                    </span>
                  ) : null}
                </span>
              ) : (
                option.label
              )}
            </label>
          )
        })}
      </div>
    </fieldset>
  )

  const seasonTabs =
    choice === 'episodes' && newestFirst.length > 0 && activeTab !== null ? (
      <div className="flex min-w-0 items-end gap-3 border-b border-hairline">
        <div
          role="tablist"
          aria-label="Seasons"
          className="-mb-px flex min-w-0 flex-1 gap-1 overflow-x-auto"
        >
          {newestFirst.map((season) => {
            const picked = pickedInSeason(season)
            const selected = season === activeTab
            return (
              <button
                key={season}
                type="button"
                role="tab"
                aria-selected={selected}
                aria-label={
                  picked > 0 ? `Season ${String(season)}, ${String(picked)} picked` : undefined
                }
                aria-controls="tv-request-episodes"
                onClick={() => showTab(season)}
                className={cn(
                  'shrink-0 border-b-2 px-3 text-[13px] font-semibold whitespace-nowrap outline-none',
                  'focus-visible:ring-2 focus-visible:ring-gold/60',
                  isPhone ? 'min-h-11' : 'min-h-9',
                  selected
                    ? 'border-gold text-ink'
                    : 'border-transparent text-faint hover:text-muted',
                )}
              >
                Season {season}
                {picked > 0 ? (
                  <span aria-hidden className="ml-1 font-mono text-[10.5px] font-medium text-gold">
                    {picked}
                  </span>
                ) : null}
              </button>
            )
          })}
        </div>
        <button
          type="button"
          onClick={() => selectAllAired(activeTab)}
          disabled={!tabEpisodesQuery.isSuccess}
          className={cn(
            'shrink-0 self-center rounded px-1 text-[12.5px] font-semibold text-gold outline-none',
            'hover:underline focus-visible:ring-2 focus-visible:ring-gold/60 disabled:opacity-50',
            isPhone ? 'min-h-11' : 'min-h-8',
          )}
        >
          Select all aired
        </button>
      </div>
    ) : null

  const header = (
    <div className="min-w-0">
      <p className="font-mono text-[11px] tracking-wide text-faint">
        {seasonsQuery.isSuccess
          ? seasons.length === 1
            ? '1 season'
            : `${String(seasons.length)} seasons`
          : 'TV'}
        {alreadyLine(already) ? ` · ${alreadyLine(already)}` : ''}
      </p>
      {choiceControls}
      {seasonTabs}
    </div>
  )

  // --- body: what the selected choice means, or the list to pick from ---
  let body: ReactNode
  if (seasonsQuery.isPending) {
    body = <CenteredSpinner label="Loading seasons…" />
  } else if (seasonsQuery.isError) {
    body = (
      <ErrorLine
        message={errorMessage(seasonsQuery.error)}
        onRetry={() => void seasonsQuery.refetch()}
      />
    )
  } else if (choice === null) {
    body = <CenteredSpinner label="Checking the latest season…" />
  } else if (choice === 'episodes') {
    body = (
      <div id="tv-request-episodes" role="tabpanel" aria-label={`Season ${String(activeTab)}`}>
        <div ref={listTop} />
        {tabEpisodesQuery.isPending ? (
          <CenteredSpinner label="Loading episodes…" />
        ) : tabEpisodesQuery.isError ? (
          <ErrorLine
            message={errorMessage(tabEpisodesQuery.error)}
            onRetry={() => void tabEpisodesQuery.refetch()}
          />
        ) : tabEpisodes.length === 0 ? (
          <p className="py-6 text-sm text-faint">TMDB lists no episodes for this season yet.</p>
        ) : (
          <ul aria-label={`Season ${String(activeTab)} episodes`}>
            {tabEpisodes.map((episode) => (
              <EpisodeRow
                key={episode.episode_number}
                season={activeTab ?? 0}
                episode={episode}
                already={already}
                picked={Boolean(episodePicks[episodeKey(activeTab ?? 0, episode.episode_number)])}
                onToggle={toggleEpisode}
                isPhone={isPhone}
              />
            ))}
          </ul>
        )}
      </div>
    )
  } else if (choice === 'seasons') {
    body = (
      <ul aria-label="Seasons" className="pt-1">
        {newestFirst.map((season) => {
          const locked = already.wholeShow || already.seasons.has(season)
          const partial = !locked && already.episodes.has(season)
          return (
            <li key={season} className="border-b border-hairline last:border-b-0">
              <label
                className={cn(
                  'flex min-h-11 items-center gap-3 py-2.5',
                  locked ? 'cursor-default' : 'cursor-pointer',
                )}
              >
                <input
                  type="checkbox"
                  aria-label={`Season ${String(season)}`}
                  className="size-4 shrink-0 accent-gold"
                  checked={locked || seasonPicks.includes(season)}
                  disabled={locked}
                  onChange={() => toggleSeason(season)}
                />
                <span
                  className={cn(
                    'flex-1 font-display text-[15px] font-bold text-ink',
                    locked && 'opacity-60',
                  )}
                >
                  Season {season}
                </span>
                {locked ? <StatusBadge status={REQUESTED} /> : null}
                {partial ? (
                  <span className="font-mono text-[11px] text-faint">Some episodes requested</span>
                ) : null}
              </label>
            </li>
          )
        })}
      </ul>
    )
  } else {
    body = (
      <ChoiceExplainer
        choice={choice}
        latestSeason={latestSeason}
        latestEpisodes={latestEpisodes ?? []}
        seasonCount={seasons.length}
      />
    )
  }

  const notAvailable = failure && isNotImplemented(failure.error) ? NOT_AVAILABLE_COPY[failure.choice] : null
  const summary = nothingToAdd ? 'Every season is already requested' : words.summary

  const footer = (
    <div className="flex min-w-0 flex-col gap-3">
      {notAvailable ? (
        <NotAvailableYet title={notAvailable.title} message={notAvailable.message} />
      ) : failure ? (
        <p role="alert" className="text-sm text-error">
          {errorMessage(failure.error)}
        </p>
      ) : null}
      <div className={cn('flex min-w-0 gap-3', isPhone ? 'flex-col' : 'items-center')}>
        <p aria-live="polite" className="min-w-0 flex-1 text-[13.5px] leading-[18px] text-muted">
          <b className="font-semibold text-ink">{summary}</b>
          {words.note && !nothingToAdd ? <> · {words.note}</> : null}
        </p>
        <div className={cn('flex gap-2', isPhone && 'w-full')}>
          <Button
            variant="ghost"
            size={isPhone ? 'lg' : 'md'}
            className={cn(isPhone && 'flex-1')}
            onClick={() => onOpenChange(false)}
          >
            Cancel
          </Button>
          <Button
            size={isPhone ? 'lg' : 'md'}
            className={cn(isPhone && 'flex-[2]')}
            disabled={!canSubmit}
            loading={createRequest.isPending}
            onClick={() => void submit()}
          >
            {words.button}
          </Button>
        </div>
      </div>
    </div>
  )

  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={`Choose what to request from ${title}`}
      header={header}
      footer={footer}
      size="lg"
    >
      {body}
    </Sheet>
  )
}

function isEpisodeLocked(already: AlreadyRequested, season: number, episode: TvEpisodeItem): boolean {
  return episode.in_library === true || isEpisodeRequested(already, season, episode.episode_number)
}

/** The header's "what's already requested" note for Request more; null when nothing is. */
function alreadyLine(already: AlreadyRequested): string | null {
  if (already.wholeShow) return 'Every season requested'
  const parts: string[] = []
  if (already.seasons.size > 0) parts.push(`${formatSeasonList([...already.seasons])} requested`)
  if (already.episodes.size > 0) {
    parts.push(`some episodes of ${formatSeasonList([...already.episodes.keys()])} requested`)
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

function errorMessage(error: unknown): string {
  return isApiError(error) ? error.message : toApiError(error).message
}

function ErrorLine({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-3 py-6 text-sm text-error">
      <span className="min-w-0 flex-1">{message}</span>
      <Button variant="secondary" size="sm" onClick={onRetry}>
        Try again
      </Button>
    </div>
  )
}

/** The body for the three choices that need no list: what exactly will be requested. */
function ChoiceExplainer({
  choice,
  latestSeason,
  latestEpisodes,
  seasonCount,
}: {
  choice: 'latest_season' | 'new_episodes' | 'every'
  latestSeason: number | null
  latestEpisodes: readonly TvEpisodeItem[]
  seasonCount: number
}) {
  if (choice === 'latest_season') {
    const count = latestEpisodes.length
    return (
      <p className="pt-1 text-sm leading-relaxed text-muted">
        {latestSeason === null ? (
          'TMDB lists no seasons for this show yet.'
        ) : (
          <>
            <b className="font-semibold text-ink">Season {latestSeason}</b>
            {count > 0 ? `, all ${String(count)} ${count === 1 ? 'episode' : 'episodes'}` : ''}.
            Episodes that haven&apos;t aired yet are grabbed when they do. Nothing older, and it
            doesn&apos;t move on to the next season.
          </>
        )}
      </p>
    )
  }
  if (choice === 'new_episodes') {
    const next = latestEpisodes.find((episode) => !hasAired(episode.air_date))
    return (
      <div className="flex flex-col gap-3 pt-1">
        <p className="text-sm leading-relaxed text-muted">
          Each new episode is grabbed after it airs, from now on. Nothing older is downloaded.
        </p>
        <p className="font-mono text-[10.5px] font-semibold tracking-[0.14em] text-faint uppercase">
          First one
        </p>
        {next && latestSeason !== null ? (
          <div className="flex items-center gap-3 rounded-[10px] bg-surface-deep p-2.5">
            <Still episode={next} className="w-24" />
            <div className="min-w-0">
              <b className="block font-display text-[13.5px] leading-tight font-bold text-ink">
                S{latestSeason} E{next.episode_number}
                {next.name ? ` · ${next.name}` : ''}
              </b>
              <span className="font-mono text-[11.5px] text-faint">
                {formatAirDate(next.air_date)}
              </span>
            </div>
          </div>
        ) : (
          <p className="text-sm text-faint">
            The next episode hasn&apos;t been announced yet. It&apos;s grabbed once it airs.
          </p>
        )}
      </div>
    )
  }
  return (
    <p className="pt-1 text-sm leading-relaxed text-muted">
      <b className="font-semibold text-ink">
        All {seasonCount} {seasonCount === 1 ? 'season' : 'seasons'}
      </b>
      . Seasons that haven&apos;t aired yet are picked up when they do.
    </p>
  )
}

/** A 16:9 episode still: the TMDB image, or a neutral placeholder. */
function Still({
  episode,
  className,
  overlay,
}: {
  episode: TvEpisodeItem
  className?: string | undefined
  overlay?: ReactNode
}) {
  const [failed, setFailed] = useState(false)
  const showImage = Boolean(episode.still_url) && !failed
  const notAired = !hasAired(episode.air_date) && episode.air_date != null
  return (
    <div
      className={cn(
        'relative aspect-video shrink-0 overflow-hidden rounded-md bg-surface-deep ring-1 ring-white/8 ring-inset',
        className,
      )}
    >
      {showImage ? (
        <img
          src={episode.still_url ?? undefined}
          alt=""
          loading="lazy"
          className="size-full object-cover"
          onError={() => setFailed(true)}
        />
      ) : notAired && !overlay ? (
        <span className="absolute inset-0 flex items-center justify-center font-mono text-[9px] font-semibold tracking-wide text-faint">
          NOT AIRED YET
        </span>
      ) : null}
      {overlay}
    </div>
  )
}

function EpisodeRow({
  season,
  episode,
  already,
  picked,
  onToggle,
  isPhone,
}: {
  season: number
  episode: TvEpisodeItem
  already: AlreadyRequested
  picked: boolean
  onToggle: (season: number, episode: TvEpisodeItem) => void
  isPhone: boolean
}) {
  const inPlex = episode.in_library === true
  const requested = !inPlex && isEpisodeRequested(already, season, episode.episode_number)
  const locked = inPlex || requested
  const aired = hasAired(episode.air_date)
  const name = episode.name ?? `Episode ${String(episode.episode_number)}`
  const meta = [
    formatAirDate(episode.air_date),
    episode.runtime_minutes != null ? formatRuntime(episode.runtime_minutes) : null,
  ]
    .filter(Boolean)
    .join(' · ')

  let badge: ReactNode = null
  if (inPlex) badge = <StatusBadge status={IN_PLEX} />
  else if (requested) badge = <StatusBadge status={REQUESTED} />
  else if (!aired && episode.air_date != null) {
    badge = (
      <StatusBadge status={{ label: `Airs ${formatShortAirDate(episode.air_date)}`, intent: 'neutral' }} />
    )
  }

  return (
    <li className="border-b border-hairline last:border-b-0">
      <label
        className={cn(
          'flex items-start gap-3 py-3 sm:gap-3.5',
          locked ? 'cursor-default' : 'cursor-pointer',
        )}
      >
        <input
          type="checkbox"
          aria-label={`E${String(episode.episode_number)} ${name}`}
          className={cn('size-4 shrink-0 accent-gold', isPhone ? 'mt-1' : 'mt-[30px]')}
          checked={locked || picked}
          disabled={locked}
          onChange={() => onToggle(season, episode)}
        />
        <Still
          episode={episode}
          className={cn(isPhone ? 'w-[112px]' : 'w-[168px]', locked && 'opacity-60')}
          overlay={
            inPlex ? (
              <span className="absolute inset-0 flex items-center justify-center bg-[rgb(10_12_16/55%)] font-mono text-[11px] font-semibold text-muted">
                IN PLEX
              </span>
            ) : episode.still_url && episode.runtime_minutes != null ? (
              <span className="absolute right-1.5 bottom-1.5 rounded-[3px] bg-black/65 px-1.5 py-0.5 font-mono text-[10px] font-medium text-ink">
                {formatRuntime(episode.runtime_minutes)}
              </span>
            ) : undefined
          }
        />
        <div className={cn('min-w-0 flex-1', locked && 'opacity-60')}>
          <b className="block font-display text-[15px] leading-tight font-bold text-ink">
            <span className="mr-1.5 font-mono text-xs font-medium text-faint">
              E{episode.episode_number}
            </span>
            {name}
          </b>
          <div className="mt-[3px] mb-[5px] font-mono text-[11.5px] leading-4 text-faint">{meta}</div>
          {episode.overview ? (
            <p className="line-clamp-2 text-[13px] leading-[19px] text-muted">{episode.overview}</p>
          ) : null}
          {isPhone && badge ? <div className="mt-1.5">{badge}</div> : null}
        </div>
        {!isPhone ? (
          <div className="flex w-[104px] shrink-0 justify-end pt-0.5">{badge}</div>
        ) : null}
      </label>
    </li>
  )
}
