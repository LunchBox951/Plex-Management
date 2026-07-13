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
  /** Denser, darker rows for the title modal's administrator footer. */
  variant?: 'default' | 'admin'
}

/**
 * The decision-engine result: ranked acceptable releases (each grabbable) and the
 * releases that were rejected, with the reason. Rejections are surfaced, never
 * hidden — "no acceptable release" is a visible, honest state (north star #3).
 */
export function ReleaseList({
  preview,
  onGrab,
  grabbingGuid,
  canGrab,
  variant = 'default',
}: ReleaseListProps) {
  const { accepted, rejected, no_acceptable_release } = preview
  const admin = variant === 'admin'

  return (
    <div className={admin ? 'flex flex-col gap-4' : 'flex flex-col gap-5'}>
      {no_acceptable_release || accepted.length === 0 ? (
        <StateMessage
          tone="error"
          title="No acceptable release found"
          message={
            rejected.length > 0
              ? 'Every candidate was rejected — see the reasons below. You can re-search later — nothing was grabbed.'
              : 'No candidates were found for this search. You can re-search later — nothing was grabbed.'
          }
        />
      ) : (
        <section className={admin ? 'flex min-w-0 flex-col gap-2' : 'flex flex-col gap-2'}>
          <h3
            className={
              admin
                ? 'font-mono text-[10.5px] tracking-[0.12em] text-faint uppercase'
                : 'font-mono text-xs tracking-wide text-faint uppercase'
            }
          >
            Ranked releases · {accepted.length}
          </h3>
          <ol className={admin ? 'flex min-w-0 flex-col gap-1.5' : 'flex flex-col gap-2'}>
            {accepted.map((rel, i) => {
              const seasonEpisode = seasonEpisodeChip(rel.title)
              return (
                <li
                  key={rel.guid}
                  className={
                    admin
                      ? 'flex min-w-0 flex-col gap-2 rounded-lg border border-hairline bg-black/15 px-3 py-2.5 sm:flex-row sm:items-center sm:gap-3'
                      : 'flex items-center gap-3 rounded-xl border border-hairline bg-surface p-3'
                  }
                >
                  <span
                    className={
                      admin
                        ? 'hidden w-5 shrink-0 text-center font-mono text-[11px] font-semibold text-gold sm:inline'
                        : 'w-6 shrink-0 text-center font-display text-sm font-bold text-gold'
                    }
                  >
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div
                      className={
                        admin
                          ? 'truncate font-mono text-[12px] font-medium text-ink'
                          : 'truncate text-sm font-medium text-ink'
                      }
                      title={rel.title}
                    >
                      {rel.title}
                    </div>
                    <div
                      className={
                        admin
                          ? 'mt-1 flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1 font-mono text-[10.5px] text-faint'
                          : 'mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-faint'
                      }
                    >
                      {seasonEpisode ? (
                        <span className="rounded bg-white/8 px-1.5 py-0.5 font-semibold text-muted ring-1 ring-white/10">
                          {seasonEpisode}
                        </span>
                      ) : null}
                      <span className="text-muted">{rel.quality_name}</span>
                      <span>{rel.resolution}</span>
                      <span>{rel.source}</span>
                      {typeof rel.seeders === 'number' ? (
                        <span>{rel.seeders} seeders</span>
                      ) : null}
                      <span className={admin ? 'max-w-full break-words' : 'truncate'}>
                        {rel.indexer}
                      </span>
                    </div>
                  </div>
                  <Button
                    size="sm"
                    variant={admin ? 'secondary' : i === 0 ? 'primary' : 'secondary'}
                    className={
                      admin
                        ? 'self-end bg-gold/8 text-gold ring-gold/30 hover:bg-gold/15 focus-visible:ring-gold/60 sm:self-auto'
                        : undefined
                    }
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
        <section className={admin ? 'flex min-w-0 flex-col gap-2' : 'flex flex-col gap-2'}>
          <h3
            className={
              admin
                ? 'font-mono text-[10.5px] tracking-[0.12em] text-faint uppercase'
                : 'font-mono text-xs tracking-wide text-faint uppercase'
            }
          >
            Rejected · {rejected.length}
          </h3>
          <ul className={admin ? 'flex min-w-0 flex-col gap-1.5' : 'flex flex-col gap-1.5'}>
            {rejected.map((rel, i) => (
              <li
                key={`${rel.title}-${i}`}
                className={
                  admin
                    ? 'flex min-w-0 flex-col items-start gap-1.5 rounded-lg border border-hairline/60 bg-black/10 px-3 py-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3'
                    : 'flex items-center justify-between gap-3 rounded-lg border border-hairline/60 px-3 py-2'
                }
              >
                <span
                  className={
                    admin
                      ? 'min-w-0 max-w-full flex-1 truncate font-mono text-[11.5px] text-muted'
                      : 'min-w-0 flex-1 truncate text-[13px] text-muted'
                  }
                  title={rel.title}
                >
                  {rel.title}
                </span>
                <span
                  className={
                    admin
                      ? 'max-w-full rounded bg-error/10 px-1.5 py-0.5 font-mono text-[10px] leading-snug text-error ring-1 ring-inset ring-error/20 sm:shrink-0'
                      : 'shrink-0 font-mono text-[11px] text-error/90'
                  }
                >
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
