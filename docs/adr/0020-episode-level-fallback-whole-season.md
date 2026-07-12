# ADR-0020: Episode-level fallback for whole-season TV requests

- **Status:** Accepted
- **Date:** 2026-07-11
- **Context builds on:** [ADR-0011](0011-tv-season-episode-support.md) (per-season
  TV lifecycle), [ADR-0013](0013-auto-grab-worker.md) (the auto-grab worker this
  fallback runs inside), [ADR-0001](0001-integrated-app-borrowed-brains.md)
  (borrowed decision engine).

## Context

`domain/decision_engine.py`'s `prefer_season_pack` gate (issue #167) made a
whole-season TV request permanently reject any release that is not itself a
season pack. That gate is correct and stays: "The Last Man on Earth" S04 was
auto-grabbed as stray single episodes three times in production before it
existed, because every single-episode release for that season quietly out-ranked
nothing once every season pack was exhausted/blocklisted. But the gate has no
positive counterpart — it closes a wrong path without opening a right one, and
two live incidents exposed the gap:

- **"The Last Man on Earth" S04** — only two season packs ever existed for this
  season, and both are permanently unacceptable (blocklisted/bad quality). All 89
  single-episode releases for the season are rejected `NOT_SEASON_PACK` forever.
  The season can never reach `available` even though every individual episode is
  readily available as a single-episode release.
- **"Chainsmoker Cat" S01** — an airing show with zero season packs possible
  (nothing has bundled it yet, and won't until the season finishes airing): 69
  episode releases exist, all rejected the same way. The season sits in
  `no_acceptable_release` forever with no path to watchable.

Neither incident is a quality-profile problem or a blocklist problem — it is a
structural gap: **there is no way to assemble a whole-season request from
individual episodes when no acceptable pack exists**, and no per-episode
completeness tracking to know when such an assembly is actually done.

## Decision

1. **Two-pass decision flow inside the SAME auto-grab cycle, pack-first
   preserved.** Pass 1 is `decision_service.preview`'s existing behavior,
   unchanged: `prefer_season_pack=True` hard-rejects every non-pack release. If
   Pass 1 accepts a pack, it is grabbed and Pass 2 never runs — the issue #167
   failure mode (grabbing singles while a pack was viable) cannot recur, because
   Pass 2 is gated on Pass 1 having accepted **zero** packs this cycle, not on a
   relaxed retry of the same search. Pass 2, when it runs, searches the same
   season scope again but decides with `prefer_season_pack=False` and a NEW
   engine gate: `decide(..., episode_subset=missing)` rejects any candidate
   whose parsed episode set is empty (a pack reaching Pass 2) or not a subset of
   `missing` (the wrong episode, or one that overlaps an already-imported/
   downloading episode — no redundant grabs). The two gates are independent and
   mutually exclusive; a caller sets one or the other, never both.

2. **Episode-level completeness is tracked, not inferred.** A new table,
   `season_episode_states` (keyed `(season_request_id, episode_number)`,
   status `pending → grabbed → imported`), is the durable record of what the
   fallback has collected so far. One row exists per *aired* episode of the
   season — never a speculative row for an unaired one. `MetadataPort` gains
   `season_episodes(tmdb_id, season_number) -> list[EpisodeInfo]` (episode
   number + air date); the TMDB adapter implements it with the SAME in-process
   TTL-cache pattern the rest of the adapter already uses. A TMDB error (down,
   rate-limited, misconfigured) is a raise, never a silently-empty list — the
   caller treats it as **"target unknown this cycle"** and retries later; the
   fallback never guesses a target from an outage.

3. **The aired-target model.** `domain/season_completeness.py` (pure, stdlib
   only) is the arithmetic: `aired_target` keeps only episodes with a known air
   date on or before today (an unscheduled/undated episode is conservatively
   treated as "not yet aired", never entering the target); `compute_missing`
   subtracts imported and in-flight episodes from the target; `season_is_complete`
   is true only when a non-empty target is fully covered. An empty/unknown target
   (no `season_episode_states` rows at all — the common case for a season a
   clean pack import satisfied without the fallback ever touching it) degrades to
   the **legacy** behavior everywhere it matters: Pass 2 never runs (nothing
   "missing" is knowable), and an import of that season completes on any TV
   import exactly as it did before this feature existed.

4. **Import-side conditional completeness.** `import_service._import_tv_locked`
   used to call `mark_completed` unconditionally on any successful TV import.
   It is now conditional: the accepted episodes are recorded via
   `season_episode_service.apply_import` against the target read from
   `season_episode_states` **without calling TMDB** (import must work while
   TMDB is down — the target is whatever the fallback already seeded, and an
   unseeded target is the legacy "any import completes" case). If the target is
   known and not yet fully covered, the season is re-armed to `searching`
   (search backoff reset) instead of completing — the import that just landed
   is NOT undone or rejected, it simply isn't the LAST one needed. This closes
   the "true per-episode completeness" gap noted (but deferred) in
   `_import_tv_locked`'s original docstring.

5. **No new season-status enum value.** The season cycles through the existing
   `searching` / `no_acceptable_release` / `downloading` / `completed` /
   `available` states exactly as before — assembling a season from a mix of
   pack/episode grabs is invisible to the state machine, which only ever asks
   "is there an active download" / "did the last search find anything". The
   only new signal is the per-episode "N/M" progress, read directly from
   `season_episode_states` and surfaced on `SeasonStatus` (`imported_episode_count`
   / `target_episode_count`, both `None` when unseeded) for the UI badge.

6. **Airing seasons legitimately re-arm.** An `available`/`completed` season's
   aired target can grow after the fact (a new episode airs). A bounded pass
   inside the SAME auto-grab cycle (`season_episode_service.reconcile_airing`,
   capped at `AIRING_REFRESH_MAX_PER_CYCLE` seasons to protect the single TMDB
   budget) refreshes the target for terminal-but-done seasons and, when the
   refreshed target is no longer fully covered, CAS-re-arms the season
   (`available`/`completed → searching`, matching the explicit-CAS pattern
   `ensure_seasons` already uses for an evicted/`force_pending` season — never a
   plain unconditional write over a terminal status) so the newly-aired episode
   re-enters `DUE_SEARCH_STATUSES` and is collected via the ordinary Pass-1/
   Pass-2 flow, possibly in the very same cycle. This is **desired**, not a bug:
   a show legitimately becomes incomplete again the moment it airs more content
   than what was requested/imported.

7. **Serialization is free.** `grab_service.grab` plus the
   `uq_downloads_active_request` partial unique index already enforce at most
   one active download per `(request, season)`. A fallback grab creates a
   `Download` scoped to the season exactly like a pack grab; while it is active,
   auto-grab's pre-search `find_active_for_request` check skips the scope
   *before it costs a search*, so a second fallback grab for the same season can
   never start until the first one imports or fails. Episodes are therefore
   collected **one release at a time per season** — `compute_missing`'s
   "downloading" exclusion is computed defensively but is structurally empty at
   the point Pass 2 runs.
8. **TMDB is optional end-to-end.** `run_grab_cycle` gains an optional
   `metadata: MetadataPort | None` parameter; `web/app.py`'s `_autograb_once`
   resolves it best-effort (mirroring the existing Prowlarr/qBittorrent
   `ServiceNotConfiguredError` handling) and passes `None` when TMDB isn't
   configured. `None` cleanly disables both the airing pre-pass and Pass 2 —
   Pass 1 behaves byte-identically to before this feature existed. An install
   that has never configured TMDB (a config the app otherwise requires for
   search/discovery, but which the auto-grab loop itself does not strictly
   need) degrades honestly rather than crashing or silently guessing.

## Consequences

- The TLMOE-S04 and Chainsmoker-Cat-S01 shapes now have a path to watchable:
  a whole-season request with no viable pack is assembled episode-by-episode as
  releases become available, one grab per cycle, with an honest "N/M" progress
  signal instead of a permanent `no_acceptable_release` dead end.
- The issue #167 hard gate is untouched and still wins whenever a pack is
  viable — Pass 2 is strictly additive, gated on Pass 1 finding nothing.
- Migration `4ea46e54d51c` adds `season_episode_states` and backfills `imported`
  rows from existing imported downloads'/scopes' `episodes_json` (so e.g. TLMOE
  S4E07 counts as imported without a re-download); a whole-season pack import
  (`episodes_json IS NULL`) seeds no rows and reads as "target unknown" until
  the first auto-grab cycle refreshes it from TMDB — never synthesized offline.
- The contract change (`SeasonStatus.imported_episode_count` /
  `.target_episode_count`) regenerates `openapi.json` + the typed frontend
  client; the Requests UI renders "S`<n>` N/M" while a season is partially
  imported, plain "S`<n>`" otherwise.
- A future web-config knob for `AIRING_REFRESH_MAX_PER_CYCLE` (today a module
  constant, mirroring `AUTO_GRAB_MAX_SEARCHES_PER_CYCLE`) is a noted follow-up,
  same posture as ADR-0013's other tunables.
