# ADR-0010: The import pipeline, honest two-phase availability, and the public request-state contract

- **Status:** Accepted — 2026-06-30
- **Deciders:** LunchBox951 (owner)
- **Resolves:** the open items parked in
  [`overview.md` §11.4](../design/overview.md) ("state-machine states/transitions,
  finalized") and the design handoff's §5.1 request that the backend "publish the
  canonical request-state enum exposed to the API" and the report-issue guarantees.

## Context

The backend alpha ([#8](https://github.com/LunchBox951/Plex-Management/pull/8))
shipped `request → search → grab` and then **deliberately stops**: a completed
torrent is mapped to `DownloadState.ImportPending` and parked there. The
`LibraryPort` (`is_available` / `trigger_scan` / `list_sections`) exists only as a
stub that raises `NotImplementedError`, there is no import orchestration, and the
reconciler runs only when `GET /queue` is polled. Nothing a user requests can
become *Available* in Plex — the loop is open.

The persistence and the state machine were, however, built **import-ready**:
`DownloadState` already defines `ImportPending → Importing → Imported` and
`ImportBlocked`; `Download` carries `download_path`; `DownloadHistoryEvent`
already defines `import_started` / `imported`; `MediaRequest` carries
`completed_at` / `library_verified_at` / `library_removed_at`; and `RequestStatus`
already includes both `completed` and `available`. The gap is **behavioral, not
structural**.

This ADR finalizes the request-state contract the UI binds to and the shape of
the import pipeline that closes the loop. It applies the project's "borrow proven
brains" principle ([ADR-0001](0001-integrated-app-borrowed-brains.md)) to import
and quality-on-disk, standing on Radarr's import-decision/naming logic and
Overseerr's Plex-identity matching rather than re-deriving them.

## Decision

### 1. The public request-state contract

The API exposes `RequestStatus` as the per-title contract the front end maps
one-to-one (the design handoff's six-state model collapses onto it):

```
pending → searching → { no_acceptable_release | downloading }
        → completed → available
                    ↘ (correction) → searching
failed   is the terminal hard-failure (operator/visible)
```

- `no_acceptable_release` is a **surfaced, retryable** state, never a silent
  `failed` (north-star #3).
- `completed` means *imported to the library folder and a Plex scan was
  triggered, but Plex has not yet confirmed the item is indexed* — the UI shows a
  "Finalizing" state.
- `available` is asserted **only after** `LibraryPort.is_available(tmdb_id)`
  confirms Plex has indexed the title (see §3).

The download-side machine keeps its richer vocabulary
(`ImportPending`/`Importing`/`Imported`/`ImportBlocked`); only the *request*
status is the public contract.

### 2. The import pipeline (movies-first)

A pure validation gate plus a thin orchestration service over existing ports:

- **Validate on the completed file, reusing the decision brain.** The release
  parser, `media_match`, the CAM/TS/TELECINE/WORKPRINT hard-cutoff in
  `source_mapping`, and `quality_service.check_quality` already gate release
  *names*; the importer runs the **same brain on the completed file's name**. The
  file's quality is gated against the **profile** (allowed/not-allowed), **not**
  against the grabbed release's exact quality — a benign source drift (a WEBRip
  where a WEBDL was grabbed) imports, while CAM/TS/sample is rejected. This is the
  structural fix for the prototype's defining bug (it imported whatever video file
  appeared and marked it available). Sample detection is size-based for the beta,
  and an **indeterminate result rejects** (Radarr's honesty rule).
- **Hardlink-or-copy, never move.** The torrent keeps seeding, so the file is
  hardlinked into the library (instant, zero extra space on the same mount) with a
  size-verified copy + rollback fallback across mounts. `move` is never used on a
  seeding torrent.
- **Name to the Plex convention.** A pure `domain/naming.py` reimplements Radarr's
  filename-cleaning rules (colon-smart, bad-character mapping, reserved device
  names, separator/trailing cleanup) to produce `Title (Year)/Title (Year).ext`.
- **Trigger a targeted Plex scan.** The real `PlexLibrary` adapter resolves the
  movie section whose root is a path-prefix of the imported file and issues a
  **partial** `/library/sections/{key}/refresh?path=…` (the scan the prototype
  never did), authenticating with an `X-Plex-Token` header.
- **A failed import is `ImportBlocked`, not a silent failure.** It surfaces a
  reason and an in-app retry button (`POST /queue/{id}/import`) — correction
  without a terminal ([ADR-0005](0005-zero-terminal-web-operability.md)). A row is
  never stranded in `ImportPending` and never silently dropped.

The importer is **idempotent** (status compare-and-set on `ImportPending`) and is
driven by a **lightweight background reconcile loop** in the app lifespan — a
single asyncio task that, every ~15 s with its own session and best-effort
adapters, reconciles the client, drains imports, and confirms availability. This
keeps the reconciler the single owner of cross-system truth (overview §5) and
keeps `GET /queue` a fast read that never blocks on a multi-GB copy (which runs
off the event loop via `asyncio.to_thread`). A more capable scheduler (configurable
interval, multi-worker) is a noted follow-up.

### 3. Honest two-phase availability

Import does **not** flip the request to `available`. It marks `completed`
("Finalizing") and triggers the scan; the reconciler then confirms via
`is_available(tmdb_id)` — which matches **by TMDB id parsed from the Plex item's
GUIDs**, never by title/year (the prototype's false-positive bug) — and only then
sets `available` and stamps `library_verified_at`. A title is never reported
watchable before Plex has indexed it.

The same `is_available` powers a **request-time dedupe**: a movie already in Plex
is recorded directly as `available` instead of being searched and grabbed. The
Plex dependency is **optional** — an unconfigured or unreachable Plex logs a
visible warning and the request proceeds normally (it never blocks the pipeline,
and the error is an explicit logged decision, not a swallowed `False`).

### 4. Borrowed brains, written in our house style

Plex is integrated via a **fresh async `httpx` adapter** (typed errors at the
boundary, secrets never logged) — **not** the synchronous `plexapi` library, which
connects at import time and is not injectable. We borrow Overseerr's GUID-matching
*logic* and Radarr's import/naming *rules*, not their code or dependencies.

## Consequences

**Positive**
- The core promise is real: a requested movie becomes watchable in Plex,
  end-to-end, with every failure surfaced and retryable in-app.
- The defining prototype bug is structurally fixed — the same proven quality gate
  that rejects CAM at search time rejects it again on disk before import.
- The public `RequestStatus` contract is finalized, so the front end maps states
  one-to-one and the design handoff's open question is closed.

**Negative / risks (accepted)**
- Availability leans on a TTL-cached Plex GUID index; a title added seconds ago
  may briefly read as absent. The DB-level active-request uniqueness still
  serializes duplicate requests, so the worst case is a slightly delayed
  `available`, not a double grab.
- Import is driven by a single in-process background loop (no distributed worker);
  a multi-GB cross-mount copy runs in a thread to avoid blocking the event loop. A
  configurable-interval / multi-worker scheduler is a noted fast-follow.
- Movies-first: TV season/episode import/availability is **out of scope** and
  raises an honest deferral rather than faking a show-level answer.

**Reversibility.** The pipeline lives behind the existing `LibraryPort` /
`FileSystemPort` / `DownloadClientPort`; the naming and validation logic is pure
domain. A different media server or download client is a new adapter, not a
rewrite. The `completed → available` two-phase can collapse to one phase by
config without touching the contract.

## Alternatives considered

- **Optimistic availability** (flip to `available` immediately after triggering
  the scan). Simpler and one fewer state, but it asserts a title is watchable
  before Plex has indexed it — a soft version of the prototype's dishonesty.
  Rejected for conflicting with north-star #3; the honest two-phase costs only the
  `is_available` check we already build for dedupe.
- **`plexapi` library.** Lifts the Plex API surface for free, but is synchronous,
  connects at construction, and is not injectable/testable against a mock
  transport. Rejected for clashing with the async, ports-and-adapters,
  green-gate design.
- **Validate by re-checking equality with the grabbed release's quality.**
  Rejected per Radarr's own lesson (its `GrabbedReleaseQualitySpecification` only
  logs): benign source drift would cause false rejections. Gate on
  profile-allowed instead.
