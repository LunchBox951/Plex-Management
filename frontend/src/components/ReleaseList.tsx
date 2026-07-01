import type { AcceptedRelease, SearchPreviewResponse } from '../api/types'
import { Button } from './ui/Button'
import { StateMessage } from './ui/feedback'

const REJECTION_LABELS: Record<string, string> = {
  quality_not_wanted: 'Quality not in profile',
  blocklisted: 'Blocklisted',
  wrong_media: "Doesn't match the title",
  format_score_too_low: 'Score too low',
  no_quality_detected: 'No quality detected',
}

function rejectionLabel(reason: string): string {
  return REJECTION_LABELS[reason] ?? reason.replace(/_/g, ' ')
}

// A single episode ("S02E05"), a multi-episode file ("S02E05-E07"), or a whole
// season pack ("S02"/"S02.COMPLETE", no episode named).
const EPISODE_RE = /\bS(\d{1,2})E(\d{1,3})(?:-E?(\d{1,3}))?\b/i
const SEASON_ONLY_RE = /\bS(\d{1,2})\b/i

/**
 * Best-effort "S02E05" / "S02 pack" chip parsed from the release TITLE. The
 * contract carries no structured season/episode field for a scored release —
 * `ScoredRelease.parsed` is a backend-internal DTO, never serialized — so this
 * is cosmetic only: nothing here feeds back into grabbing (that always sends
 * the release's `guid`). `null` for a title this simple pattern can't read
 * (a movie release, or unusual tv naming) — the chip is optional, never a gate.
 */
function seasonEpisodeChip(title: string): string | null {
  const episode = EPISODE_RE.exec(title)
  if (episode) {
    const season = `S${episode[1]!.padStart(2, '0')}`
    const first = `E${episode[2]!.padStart(2, '0')}`
    const last = episode[3] ? `-E${episode[3].padStart(2, '0')}` : ''
    return `${season}${first}${last}`
  }
  const seasonOnly = SEASON_ONLY_RE.exec(title)
  return seasonOnly ? `S${seasonOnly[1]!.padStart(2, '0')} pack` : null
}

interface ReleaseListProps {
  preview: SearchPreviewResponse
  onGrab: (release: AcceptedRelease) => void
  /** guid of the release currently being grabbed (shows the spinner). */
  grabbingGuid: string | null
  /** false until a request exists — grabbing needs a request id. */
  canGrab: boolean
}

/**
 * The decision-engine result: ranked acceptable releases (each grabbable) and the
 * releases that were rejected, with the reason. Rejections are surfaced, never
 * hidden — "no acceptable release" is a visible, honest state (north star #3).
 */
export function ReleaseList({ preview, onGrab, grabbingGuid, canGrab }: ReleaseListProps) {
  const { accepted, rejected, no_acceptable_release } = preview

  return (
    <div className="flex flex-col gap-5">
      {no_acceptable_release || accepted.length === 0 ? (
        <StateMessage
          tone="error"
          title="No acceptable release found"
          message="Every candidate was rejected by the quality gate or blocklist. You can re-search later — nothing was grabbed."
        />
      ) : (
        <section className="flex flex-col gap-2">
          <h3 className="font-mono text-xs tracking-wide text-faint uppercase">
            Ranked releases · {accepted.length}
          </h3>
          <ol className="flex flex-col gap-2">
            {accepted.map((rel, i) => {
              const seasonEpisode = seasonEpisodeChip(rel.title)
              return (
                <li
                  key={rel.guid}
                  className="flex items-center gap-3 rounded-xl border border-hairline bg-surface p-3"
                >
                  <span className="w-6 shrink-0 text-center font-display text-sm font-bold text-gold">
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-medium text-ink" title={rel.title}>
                      {rel.title}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-faint">
                      {seasonEpisode ? (
                        <span className="rounded bg-white/8 px-1.5 py-0.5 font-semibold text-muted ring-1 ring-white/10">
                          {seasonEpisode}
                        </span>
                      ) : null}
                      <span className="text-muted">{rel.quality_name}</span>
                      <span>{rel.resolution}</span>
                      <span>{rel.source}</span>
                      {typeof rel.seeders === 'number' ? <span>{rel.seeders} seeders</span> : null}
                      <span className="truncate">{rel.indexer}</span>
                    </div>
                  </div>
                  <Button
                    size="sm"
                    variant={i === 0 ? 'primary' : 'secondary'}
                    disabled={!canGrab}
                    loading={grabbingGuid === rel.guid}
                    onClick={() => onGrab(rel)}
                    title={canGrab ? undefined : 'Request this title first'}
                  >
                    Grab
                  </Button>
                </li>
              )
            })}
          </ol>
          {!canGrab ? (
            <p className="font-mono text-[11px] text-faint">Request this title to grab a release.</p>
          ) : null}
        </section>
      )}

      {rejected.length > 0 ? (
        <section className="flex flex-col gap-2">
          <h3 className="font-mono text-xs tracking-wide text-faint uppercase">
            Rejected · {rejected.length}
          </h3>
          <ul className="flex flex-col gap-1.5">
            {rejected.map((rel, i) => (
              <li
                key={`${rel.title}-${i}`}
                className="flex items-center justify-between gap-3 rounded-lg border border-hairline/60 px-3 py-2"
              >
                <span className="min-w-0 flex-1 truncate text-[13px] text-muted" title={rel.title}>
                  {rel.title}
                </span>
                <span className="shrink-0 font-mono text-[11px] text-error/90">
                  {rejectionLabel(rel.reason)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  )
}
