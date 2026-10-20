import { useState } from 'react'
import type { RequestResponse } from '../../api/types'
import { requestStatus } from '../../lib/status'
import { Button } from '../ui/Button'
import { ProgressBar } from '../ui/ProgressBar'
import { StatusBadge } from '../ui/StatusBadge'

/** One request rendered as a row card (poster · identity · live state). */
export function RequestRow({
  request,
  onOpen,
  onReSearch,
  line,
}: {
  request: RequestResponse
  onOpen: () => void
  onReSearch: (() => void) | null
  /** What it's waiting on, in plain words ("Looking for a good copy"). */
  line?: string | undefined
}) {
  const meta: string[] = []
  if (request.year != null) meta.push(String(request.year))
  meta.push(request.media_type)

  const showProgress = request.status === 'downloading' && request.download_progress != null
  const progressPercent = showProgress
    ? Math.round(Math.min(1, Math.max(0, request.download_progress ?? 0)) * 100)
    : null

  // Fall back to the gradient placeholder both when there's no poster_url AND
  // when a real one fails to load (404 / expired TMDB URL) — a bad URL must
  // never leave a broken-image icon sitting in the row.
  const [imgFailed, setImgFailed] = useState(false)
  const showImg = Boolean(request.poster_url) && !imgFailed

  return (
    <li className="group relative grid grid-cols-[46px_minmax(0,1fr)] items-center gap-x-4 gap-y-3 rounded-xl border border-hairline bg-surface p-[13px] transition-colors hover:border-white/15 sm:grid-cols-[46px_minmax(0,1fr)_auto] sm:px-4">
      {/* A stretched native button keeps the card and the shortcut as sibling
          controls. It sits above the visual row but below the shortcut, so all
          ordinary mouse/touch/Enter/Space activation opens details exactly once. */}
      <button
        type="button"
        aria-label={`Open details for ${request.title}`}
        onClick={onOpen}
        className="absolute inset-0 z-10 cursor-pointer rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-gold/50"
      />

      {showImg ? (
        <img
          src={request.poster_url ?? undefined}
          alt=""
          loading="lazy"
          className="aspect-[2/3] w-[46px] rounded-[5px] object-cover"
          onError={() => setImgFailed(true)}
        />
      ) : (
        <div className="aspect-[2/3] w-[46px] rounded-[5px] bg-poster bg-gradient-to-b from-white/10 to-transparent" />
      )}

      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
          <p className="min-w-0 truncate font-display text-[15px] leading-tight font-bold text-ink">
            {request.title}
          </p>
          <span className="font-mono text-[11px] leading-none font-medium text-muted">
            {meta.join(' · ')}
          </span>
          {request.is_anime ? (
            <span className="shrink-0 rounded bg-gold/15 px-1.5 py-1 font-mono text-[9.5px] leading-none font-semibold tracking-[0.06em] text-gold">
              ANIME
            </span>
          ) : null}
        </div>

        <div className="mt-2 flex min-w-0 flex-wrap items-center gap-2">
          <StatusBadge status={requestStatus(request.status)} />
          {showProgress && progressPercent != null ? (
            <div className="flex min-w-40 max-w-60 flex-[1_1_15rem] items-center gap-2">
              <ProgressBar
                value={request.download_progress ?? 0}
                label={`Download progress for ${request.title}`}
                className="min-w-20 flex-1"
              />
              <span className="shrink-0 font-mono text-[11px] text-muted tabular-nums">
                {progressPercent}%
              </span>
            </div>
          ) : null}
          {request.media_type === 'tv' && request.seasons && request.seasons.length > 0 ? (
            <ul className="flex min-w-0 flex-wrap gap-1">
              {request.seasons.map((season) => {
                // Episode-level fallback progress (ADR-0020, issue #178): "N/M"
                // while a whole-season request is partially assembled from a mix
                // of pack/episode grabs. Both counts are null for a season the
                // fallback has never touched (the common clean-pack-import case)
                // or once N reaches M (the badge degrades to the plain "Sxx" —
                // the status itself already reads completed/available then).
                const { imported_episode_count: imported, target_episode_count: target } =
                  season
                const detail =
                  target != null && imported != null && imported < target
                    ? `S${season.season_number} ${imported}/${target}`
                    : `S${season.season_number}`
                return (
                  <li key={season.season_number}>
                    <StatusBadge status={requestStatus(season.status)} detail={detail} />
                  </li>
                )
              })}
            </ul>
          ) : null}
        </div>
        {line ? <p className="mt-1.5 truncate font-mono text-[11px] text-faint">{line}</p> : null}
      </div>

      <div className="pointer-events-none relative z-20 col-start-2 row-start-2 flex shrink-0 items-center justify-end gap-2 sm:col-start-3 sm:row-start-1">
        {onReSearch ? (
          <Button
            variant="ghost"
            size="sm"
            className="pointer-events-auto bg-gold/10 text-gold ring-1 ring-inset ring-gold/30 hover:bg-gold/20 hover:text-gold focus-visible:ring-gold/60"
            aria-label={`Re-search ${request.title}`}
            onClick={(event) => {
              event.stopPropagation()
              onReSearch()
            }}
          >
            Re-search
          </Button>
        ) : null}
        <span aria-hidden className="text-lg leading-none text-faint">
          ›
        </span>
      </div>
    </li>
  )
}
