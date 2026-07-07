import type { ReactNode } from 'react'
import { cn } from '../../lib/cn'
import { glyphKind, INTENT_ICON, type GlyphKind, type StatusPresentation } from '../../lib/status'

interface TileStatusGlyphProps {
  status: StatusPresentation
  className?: string
}

/**
 * Compact status affordance for a Discover/Row tile (issue #135): replaces
 * the text `StatusBadge` pill in the badge slot with a small icon chip so the
 * grid reads as artwork first. Every icon is an inline SVG — the CSP forbids
 * an external icon font/CDN, and this app already self-hosts fonts for the
 * same reason (ADR-0005).
 *
 * Deliberately reads the SAME `StatusPresentation` the modal's `StatusBadge`
 * renders as text: `glyphKind`/`INTENT_ICON` in `lib/status.ts` are the one
 * place the icon and color are derived, so the tile's glyph and the modal's
 * descriptive label can never disagree about what a title's state is. This
 * component itself holds no state-name knowledge beyond that lookup.
 *
 * `downloading` gets a slim indeterminate (animated, not measured) bar under
 * the icon — Discover never fetches queue progress (that's an admin-only
 * `GET /queue` the modal alone reads), so any numeric percentage here would
 * be fabricated. An indeterminate hint is honest; a fake number is not.
 */
export function TileStatusGlyph({ status, className }: TileStatusGlyphProps) {
  const kind = glyphKind(status)
  const color = INTENT_ICON[status.intent]

  return (
    <div
      // `StatusBadge` (the thing this replaces) rendered the label as plain
      // visible text, so a screen reader browsing the grid announced it
      // alongside the poster. Swapping it for an icon must not silently drop
      // that information — `role="img"` + `aria-label` keeps the same status
      // announced, just visually compact. `title` is a bonus mouse tooltip.
      role="img"
      aria-label={status.label}
      title={status.label}
      className={cn('flex flex-col items-center gap-1', className)}
    >
      <div className="flex size-6 items-center justify-center rounded-full bg-black/55 ring-1 ring-inset ring-white/10">
        <GlyphIcon kind={kind} className={cn('size-3.5', color)} />
      </div>
      {kind === 'downloading' ? (
        <div className="h-[3px] w-6 overflow-hidden rounded-full bg-black/55">
          <div className={cn('h-full w-1/2 animate-pulse rounded-full', 'bg-downloading')} />
        </div>
      ) : null}
    </div>
  )
}

interface GlyphIconProps {
  kind: GlyphKind
  className?: string
}

/** Shared SVG shell — every glyph is a 24x24 stroke icon, currentColor. */
function GlyphIcon({ kind, className }: GlyphIconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.25}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden
    >
      {GLYPH_PATHS[kind]}
    </svg>
  )
}

// One path fragment per glyph kind (see `glyphKind` in lib/status.ts for the
// StatusPresentation -> kind mapping).
const GLYPH_PATHS: Record<GlyphKind, ReactNode> = {
  // Requested / processing: a plain clock face — waiting, not yet working.
  pending: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  // A genuine "searching indexers" status: an animated pulse ring reads as
  // active work in progress, distinct from the static pending clock.
  searching: (
    <g className="animate-pulse">
      <circle cx="12" cy="12" r="3" fill="currentColor" stroke="none" />
      <circle cx="12" cy="12" r="8.5" strokeOpacity={0.5} />
    </g>
  ),
  // Downloading: a down-arrow into a tray (paired with the indeterminate bar
  // rendered by TileStatusGlyph itself for the downloading kind).
  downloading: (
    <>
      <path d="M12 4v11" />
      <path d="M7.5 11.5 12 16l4.5-4.5" />
      <path d="M5 19.5h14" />
    </>
  ),
  // In library: a full checkmark.
  available: <path d="M5 12.5 9.5 17 19 7" />,
  // Partially available (tv rollup): a minus, mirroring Overseerr's
  // PARTIALLY_AVAILABLE glyph — deliberately distinct from the full check.
  partial: <path d="M5.5 12h13" />,
  // Error (no acceptable release / import blocked): an exclamation.
  error: (
    <>
      <path d="M12 7.5v6" />
      <circle cx="12" cy="16.75" r="0.75" fill="currentColor" stroke="none" />
    </>
  ),
}
