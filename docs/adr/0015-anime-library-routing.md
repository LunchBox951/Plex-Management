# ADR-0015: Anime library routing (optional anime roots, routing only)

- **Status:** Accepted
- **Date:** 2026-07-04
- **Context builds on:** [ADR-0001](0001-integrated-app-borrowed-brains.md)
  (borrowed brains — don't re-derive), [ADR-0011](0011-tv-season-episode-support.md)
  (the optional `tv_root` pattern this mirrors), [ADR-0012](0012-operability-health-logs-eviction.md)
  (the root-guarded `FileSystemPort` eviction/purge primitive), [ADR-0014](0014-correction-verbs.md)
  (report-issue's purge, which shares that same guard).

## Context

`is_anime` detection already exists end-to-end: the TMDB keyword `210024`
("Anime") is checked in the metadata adapter, persisted on `MediaRequest`, and
surfaced as a UI badge — the same signal Overseerr uses
(`MediaRequestSubscriber.ts`, `ANIME_KEYWORD_ID = 210024`). What v2 lacked was
anywhere to *do* anything with it: every anime title imported into the same
`movies_root`/`tv_root` as everything else, with no way to keep an anime
collection on its own Plex library/disk.

Two adjacent stacks were considered and explicitly **not** followed:

- **The `prototype/` service** routes anime at the qBittorrent *download*
  layer (`ANIME_PATH` env var, terminal-only setup), not the Plex import — a
  north-star #2 violation (terminal-only config) this project does not repeat.
- **Sonarr's dual-audio / anime-numbering scoring** is a real, deeper feature
  (absolute episode numbers, anime release-group parsing, language as an
  ordered tiebreak) that would touch the decision engine. That work is
  deferred to v1.1+ (design doc §9) and is explicitly **out of scope** here.

## Decision

Ship the **routing half only**: two new, entirely optional settings,
`anime_movie_root` and `anime_tv_root`, mirroring `tv_root`'s optional
treatment in every respect (Overseerr's `activeAnimeDirectory` /
optional-override-else-default shape, ported directly —
`MediaRequestSubscriber.ts:481-484`).

- **Storage:** both are plain keys in the existing settings key-value store
  (`KNOWN_SETTING_KEYS`) — **no DB column, no Alembic migration**. `is_anime`
  itself already exists as a real column; this ADR adds no new detection or
  persistence for it.
- **Routing:** in `import_service`, when the owning request has
  `is_anime=True` **and** the matching anime root is configured, the anime
  root is used *instead of* `movies_root`/`tv_root`; otherwise (root unset, or
  `is_anime=False`) behavior is byte-for-byte identical to before this feature
  existed. `naming.py` is unchanged — an anime library is an ordinary Plex
  root with the same `Title (Year)` / `Season NN` layout; only the base
  `Path` differs.
- **Delete-guard (non-negotiable, not just a nicety):** `get_eviction_filesystem`
  — the only `FileSystemPort` whose `delete()` may ever run — now also takes
  the anime roots and adds them to its containment allowlist. Without this, an
  anime title's `library_path` sits outside every guarded root and
  `purge_library_path` silently `refused`s: a report-issue on anime content
  would blocklist + re-search but leave the bad file on disk forever. This is
  a regression the routing feature itself introduces, so it ships in the same
  PR at all three call sites (`report_issue_endpoint`, `POST /ops/evict`, the
  periodic eviction tick in `web/app.py`).
- **Disk-pressure sweep + gauges:** the periodic/manual eviction sweeps and the
  `GET /ops/disk` + health-dashboard gauges also enumerate the anime roots as
  their own rows. Without this, a separate anime disk is never a
  pressure-eviction candidate (`eviction_service._under_root` filters
  candidates to the enumerated roots) and its usage never appears on the
  Status page — a silent gap north star #3 forbids. `EvictErrorItem.root`'s
  `Literal` widens accordingly (a wire-contract change, so `make openapi` +
  `make gen-client` regenerate the OpenAPI doc and the FE client types).
- **Settings/setup UI:** both roots get an optional Plex-library picker,
  reusing the existing `movies_root`/`tv_root` picker pattern verbatim
  (an anime library is just another ordinary Plex movie/tv section). Neither
  field gates setup completion or Save — both stay optional. A Plex
  reconnect clears a stale anime root exactly as it already clears
  `movies_root`/`tv_root`.

## Explicitly out of scope

- **Dual-audio / Japanese-language scoring.** Deferred to v1.1+, to be built
  (when it is) as a Sonarr-style ordered tiebreak — a new weight tier *below*
  `_SEEDER_WEIGHT` in the decision engine's additive scoring, gated on
  `is_anime` and reading `ParsedRelease.languages` — never a multiplier that
  can outrank a hard quality cutoff (the prototype's `base_score *= 2.0`
  mistake). Not touched by this ADR.
- **Migrating already-imported anime.** Adding an anime root is not
  retroactive: existing anime titles stay under `movies_root`/`tv_root`.
  Eviction/report-issue purge use the stored `library_path` breadcrumb, so
  they keep working regardless of which root a given title actually lives
  under. A future "relocate to anime library" correction button is left for a
  later ADR if wanted.
- **New anime-specific detection.** `is_anime`/keyword 210024 is unchanged;
  this ADR only adds a destination for content already flagged that way.

## Consequences

- An anime collection can live on its own Plex library/disk, purely by
  setting two optional paths — no re-derived detection, no naming change, no
  schema migration.
- The one genuine regression risk (the delete-guard allowlist) is closed in
  the same change, at every call site that builds a delete-capable
  filesystem.
- Anime content is a first-class citizen of the disk-pressure sweep and the
  Status dashboard, not a silent blind spot on a second disk.
- Dual-audio scoring remains a clearly-scoped future ADR, kept away from the
  just-hardened decision engine.
