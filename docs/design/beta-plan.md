# Early Beta — scope & plan

- **Status:** In progress (first beta session)
- **Goal:** make a requested movie become **watchable in Plex end-to-end**
  (close the import loop the alpha deliberately left open), and bring the
  request-facing UI up to the **cinematic Discover home** of the design handoff —
  reusing the alpha's correctly-shaped ports and the quality brain that already
  exists.

This is a planning artifact for the beta; like [`alpha-plan.md`](alpha-plan.md)
and [`frontend-alpha-plan.md`](frontend-alpha-plan.md) it is removed at the v1
cleanup ([issue #3](https://github.com/LunchBox951/Plex-Management/issues/3)). The
durable decisions live in the ADRs — primarily
[ADR-0010](../adr/0010-import-pipeline-honest-availability.md) (import pipeline,
honest availability, public request-state contract). Design source: the Claude
Design handoff ([issue #7](https://github.com/LunchBox951/Plex-Management/issues/7))
— *a template, not a source of truth; engineering docs win on conflict.*

## The beta's right edge

The alpha proved `request → search → grab`. The beta closes the loop and dresses
the front:

> a completed download is **validated** against the requested movie (reusing the
> CAM/TS hard-cutoff brain on the file), **hardlinked** into the Movies library
> under the Plex naming convention, and a **targeted Plex scan** is triggered;
> the reconciler then **confirms availability** and the request flips to
> *Available*. The home screen becomes a **cinematic Discover** (spotlight +
> trending/popular rows + real poster art), titles carry their live state, and
> **report-issue** correction is one click.

**Movies-first.** TV season/episode scoping, an SSE event stream, multi-root
libraries, a backend image proxy, Plex-OAuth identity, and recommendation rows are
**deferred to the next beta** — helpers branch on `media_type` so we are not
painted into a corner.

## Architecture (recap)

Ports-and-adapters; the `domain/` core stays pure. The beta is **remake, not
reinvent and not port the prototype**: the seams already exist
(`LibraryPort`, `FileSystemPort`, the `ImportPending → Importing → Imported`
states, `Download.download_path`, `DownloadHistoryEvent.import_started/imported`),
and the validation/quality brain (`media_match`, `source_mapping`,
`quality_service`, `decision_engine`) is reused verbatim on the completed file.

## Track A — Backend: close the loop (the spine)

1. **`movies_root` config** (web-operable, no schema change): add to
   `KNOWN_SETTING_KEYS`, the setup wizard body + `routers/setup.py` mapping, the
   settings UI, and a `setup_validation` probe (path exists / writable). Add
   `get_filesystem()` and `get_library()` dependency factories in `web/deps.py`.
2. **Pure `domain/naming.py`**: Radarr-style `clean_title` (colon-smart,
   bad-char map, reserved device names, separator/trailing cleanup) +
   `plex_movie_relative_path(title, year, ext) → "Title (Year)/Title (Year).ext"`.
   Stdlib-only; covered by the domain-purity test + table-driven cases.
3. **Harden `LocalFileSystem`**: disk-space preflight (`available_bytes ≥ src`)
   and size-verify-and-rollback after the copy fallback in `hardlink_or_copy`; add
   `largest_video_file(root)` (skip `sample`/extras). Port stays sync; the service
   calls it via `asyncio.to_thread`.
4. **Real `PlexLibrary` adapter** (replace the stub): `list_sections`
   (GET `/library/sections`, with `locations`), `trigger_scan` (path→section
   prefix match, GET `/library/sections/{key}/refresh?path=…`, fallback
   refresh-all), `is_available` (page `/library/sections/{id}/all?includeGuids=1`,
   **match by TMDB id from the item GUIDs**, never title/year). `X-Plex-Token` +
   `Accept: application/json` headers; typed `PlexLibraryError`/`PlexAuthError`
   never swallowed; the present-tmdb-id index cached in `app.state` with a TTL.
5. **Validation gate — pure `domain/import_validation.py`**: pick the primary
   video file, parse its name, run `matches_media` (reject `WRONG_MEDIA`) →
   `resolve_quality` + `check_quality` against the **profile** (reject CAM/TS and
   not-wanted — *profile-allowed, not equal-to-grab*) → size-based sample check
   (**indeterminate rejects**) → multipart reject. Returns a typed
   `ImportValidation(accepted, rejections)`. Needs `DownloadClientPort.list_files`
   + a qBit `/torrents/files` wrapper.
6. **`services/import_service.py`**: for `ImportPending` rows — resolve source
   (live `content_path` → `download_path` → `save_path`), validate; on accept
   advance `ImportPending → Importing`, `hardlink_or_copy` to the named dest under
   `movies_root`, `trigger_scan`, advance to `Imported`, write the history events,
   and mark the request **`completed`** ("Finalizing"); on reject move to
   **`ImportBlocked`** with a surfaced reason. Idempotent; dst-collision = skip.
7. **Honest availability** + **wire the loop**: the reconciler confirms
   `is_available(tmdb_id)` for `completed` requests and flips them to
   **`available`** (`library_verified_at`). Import auto-fires from
   `reconcile_and_list` for `ImportPending` rows; `POST /queue/{id}/import` is the
   operator retry for `ImportBlocked`.
8. **Request-time Plex dedupe**: `create_request` optionally calls `is_available`;
   an in-Plex movie is recorded directly as `available` (skip search/grab). The
   `LibraryPort` dep is optional — an unconfigured/unreachable Plex logs a visible
   warning and the request proceeds.

## Track B — Backend: Discover MVP (unblocks the frontend)

9. **TMDB adapter**: add `backdrop_url` (w780) + a parameterized image size;
   add `trending_movies` / `popular_movies` / `upcoming_movies` (reuse the
   leak-safe `_get`, the person-dropping `_parse_search_row`, and `_TtlCache`).
   A frozen `MediaPage{page,total_pages,total_results,results}` DTO.
10. **`GET /discover/home`** + `GET /discover/{category}?page=`: the service
    **composes rows server-side with items embedded** (`{row_type, title,
    items[]}`) via `asyncio.gather(return_exceptions=True)` so one failed row is an
    honest empty/errored row, never a silent drop. `row_type` is an open string
    and items carry `media_type`, so TV / recommendation rows are additive later.
    Row order is a server-side constant (no migration).

## Track C — Frontend: cinematic home

Reuses the existing tokens, `PosterCard` (gradient fallback, lazy-load, badge
slots), `StatusBadge` + `status.ts` (6-state), `ProgressBar`, `Dialog` (reserved
hero slot), and `ReleaseList`. Net-new:

11. **`Row.tsx`** (CSS scroll-snap + `scrollBy` chevrons with end-disabling — no
    Swiper/react-spring) + **`Spotlight.tsx`** (full-bleed backdrop hero) +
    `useDiscoverHome`. `Discover.tsx` branches: empty query → spotlight + rows;
    query → today's search grid. Real poster/backdrop art with gradient fallback.
12. **6-state `TitleDetailModal`**: derive the title's state by correlating
    `/requests` + `/queue` keyed on `media_type:tmdb_id` (no new endpoint), keep
    the existing race guard, and render a state-aware action zone (Request /
    Finalizing / In your library / rejections + Re-search / Report a problem).
    **Report-issue** wires the existing `mark-failed`/`blocklist`/`re-search`
    behind a confirm dialog — **not** an issues table.
13. **Self-host fonts**: bundle Archivo / Hanken Grotesk / IBM Plex Mono as woff2
    (`@fontsource/*`, build-time) and drop the Google Fonts CDN `<link>`
    (ADR-0005 / north-star #2).

## Cross-cutting

- **Migrations** (one per schema change, per CLAUDE.md): the schema is largely
  import-ready, so the beta needs **one** migration — adding `media_requests`
  `poster_url` / `backdrop_url` (art on Requests/Queue rows) via
  `alembic revision --autogenerate`. The import-blocked reason reuses the existing
  `Download.failed_reason` field, the completed-file path is resolved live from the
  client (no persisted column), and `movies_root` is a `settings` row — none of
  these need a migration.
- **Contract gate**: a single `openapi.json` export + `make gen-client` regen
  after the new endpoints/DTOs land; CI `gen:check` + the OpenAPI-freshness gate
  enforce no drift. **All frontend wiring is hard-gated behind this.**
- **Adapter wiring**: `PlexLibrary` goes stub → real; the TMDB adapter grows the
  backdrop base + discover methods; both are reached only through the new
  `web/deps` factories so `domain`/`services` stay adapter-free. The importer
  receives ports by injection and never imports an adapter.
- **Background task**: a lightweight reconcile loop in the app lifespan (one
  asyncio task, ~15 s, own session, best-effort adapters) reconciles the client,
  drains imports, and confirms availability — so `GET /queue` stays a fast read
  and never blocks on a copy (run via `asyncio.to_thread`). Imports are guarded +
  idempotent. A configurable / multi-worker scheduler is a noted follow-up.
- **Honesty thread**: a failed import is `ImportBlocked` (retryable, in-app
  button), never silent and never stranded; `no_acceptable_release` stays a
  visible retryable state; Plex token and TMDB key are never logged.

## Quality gates

Python: `ruff check` · `ruff format --check` · `pyright --strict` · `pytest`
(domain-purity test covers `naming.py` / `import_validation.py`; new ports get
`tmp_path` / `httpx.MockTransport` tests). Frontend: `tsc --noEmit` · eslint ·
`vitest` (6-state derivation + `Row` scroll/keyboard) · the gen-client drift
check · build. `make check` green at every step.

## Build sequence

1. Spec + ADR (this doc + ADR-0010).
2. Track A backend: config + naming + FS hardening + `PlexLibrary` + validation +
   `import_service` + loop wiring + dedupe, each with tests.
3. Track B backend: TMDB discover methods + `/discover/home`.
4. Regenerate `openapi.json` + the typed client (the gate).
5. Track C frontend: `Row` / `Spotlight` / `useDiscoverHome`, the 6-state modal +
   report-issue, self-hosted fonts.
6. Migrations for the additive columns.
7. Adversarial review loop, then a live browser-driven smoke against real Plex /
   Prowlarr / qBittorrent / TMDB, then the PR.

## Deferred to the next beta

TV season/episode scoping · SSE/WebSocket event stream + a configurable / multi-
worker reconciler scheduler · multi-root libraries (Radarr-style longest-parent-match) · a backend
image proxy/cache (posters/backdrops still load from TMDB's CDN) · Plex-OAuth
identity + avatars · recommendation/affinity rows · disk-pressure eviction ·
policy-based retention.
