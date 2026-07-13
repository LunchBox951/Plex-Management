# ADR-0021: Plex watchlist request automation

- **Status:** Accepted
- **Date:** 2026-07-12

## Context

Plex users already collect movies and shows in the Universal Watchlist. Requiring
them to repeat that intent in Plex Manager leaves a manual gap in the v1
request-to-watchable loop. Watchlist membership must also protect desired media
from disk-pressure eviction without turning a transient Plex outage into mass
unprotection.

Plex documents the user-facing watchlist feature, but its cloud metadata endpoint
is not part of the documented Plex Media Server API. RSS feeds require Plex Pass
and lose the signed-in-user identity needed for attribution.

## Decision

Poll each signed-in user's watchlist with that user's stored Plex token. The
cloud wire shape lives behind a dedicated `WatchlistPort`; tokens travel only in
`X-Plex-Token` and never enter URLs or logs. Movies map to movie requests and
shows map to whole-show requests through the existing request service.

Persist a complete per-user snapshot only after every page fetches successfully.
Failures retain the last good snapshot, so an outage cannot remove eviction
protection. Successful removals remove only watchlist-derived protection: they do
not cancel requests, delete media, or alter the independent `keep_forever` pin.

Active-title dedup remains global. A request keeps its original `user_id` as
creator/audit provenance, while `request_subscribers` grants every later
requester visibility. Subscribers are read-only; creator/admin mutation authority
is unchanged. Ownerless API-key automation rows remain ownerless when a browser
user later subscribes; visibility never silently grants cancel/report authority.
When duplicate rows collapse, every subscriber is copied to the surviving row.
Eviction checks current membership during candidate ranking and
again in the final database claim immediately before deletion.

Synchronization is enabled by default every 15 minutes. Both enablement and the
bounded interval are web-editable; changing either wakes the worker immediately
rather than waiting out the prior interval. Per-user and per-entry failures are
isolated and summarized through health status without credential-bearing detail.

## Consequences

- Plex endpoint drift produces a visible, retryable degraded sync while the last
  safe snapshot remains protective.
- Any user's current membership protects the title and, for television, every
  tracked season.
- Shared request history becomes visible without expanding cancellation or
  correction authority.
- Request quotas, approval policies, and richer shared-request controls remain
  explicit follow-ups; this worker must route future limits through the same
  request-policy boundary rather than inventing a second policy engine.
