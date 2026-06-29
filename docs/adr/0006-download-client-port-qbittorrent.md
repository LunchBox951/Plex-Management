# ADR-0006: `DownloadClient` port; qBittorrent as the v1 adapter

- **Status:** Accepted — 2026-06-29
- **Deciders:** LunchBox951 (owner)

## Context

Per [ADR-0001](0001-integrated-app-borrowed-brains.md), the app owns the entire
brain and delegates only the actual byte-downloading. The owner's download client
is qBittorrent. The prototype talked to qBittorrent directly and let its state
drift from the app's database, producing errors when a torrent the app still
tracked had already been removed from the client.

## Decision

Define a narrow **`DownloadClientPort`** interface in the domain core (add by
magnet/URL, query status/progress, pause/resume, remove, report completion/save
path) and implement it with a single **qBittorrent adapter** for v1. The brain
never imports a qBittorrent type directly.

The **reconciler** owns truth across the client, the database, the filesystem,
and Plex; adapters report observed state, and the reconciler heals divergence
(e.g. a torrent removed from the client is reconciled out of the DB rather than
crashing a monitor loop).

## Consequences

**Positive**
- qBittorrent is not hardcoded into the brain; Transmission/Deluge/etc. can be
  added later as adapters without touching the core.
- The brain is testable against a fake `DownloadClientPort` with no network.
- The drift class of bugs is addressed by design (reconciler, not scattered
  side-effects).

**Negative / risks**
- A port adds a small indirection over calling the client directly. Worth it for
  testability and swappability.
- Only qBittorrent is implemented in v1 (YAGNI on other clients) — the seam exists
  but is not exercised by a second adapter yet.

## Alternatives considered

- **Hardcode qBittorrent throughout** (prototype approach) — simplest, but couples
  the brain to one client and entangles client state with domain state. Rejected.
