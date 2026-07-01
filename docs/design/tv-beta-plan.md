# TV Show Support — Implementation Blueprint (beta 2)

Extends the movies-first loop (request → search → grab → import → available) to TV
(seasons + episodes), reusing every movie component as the literal template.
Repo root: `/home/Projects/Plex-Management`.

## Grounding conventions (MUST obey)
- **Domain purity test** (`tests/domain/test_domain_purity.py`) forbids domain modules importing httpx/sqlalchemy/guessit/fastapi/models/adapters/web/repositories. New domain modules (naming, import_validation, season_pack, season_rollup) duplicate enum *string values* as literals (precedent: `repositories/downloads.py:27-29`).
- **Two-phase honest availability**: `completed` ("Finalizing", scan triggered) → `available` (Plex confirmed). TV reuses this shape PER SEASON.
- **CAS everywhere**: `downloads.update_status_if_in`, partial-unique-index dedup + IntegrityError catch-and-reread (`request_service.py:159-184`). New `SeasonRequestRepository.ensure` follows the identical IntegrityError pattern.
- **Season already first-class at search/grab**: `media_match.matches_media` has `expected_season` gate (`_season_covers` l.52-65); `decision_service.preview` builds `IndexerSearchRequest(season=season)` gating year(movie)/season(tv) l.88-99; `grab_service.grab` threads `season` into `Download.season` l.175,244. **Gap is downstream of grab**: import, validation, availability, naming.
- **Deferred markers = seams to fill**: `import_service.py:294-303` ("tv import deferred"), `:517-518` (`if media_type != "movie": continue`), `adapters/plex/library.py:304-305` (`is_available` raises NotImplementedError tv), `trigger_scan` movie-only `:362-378`.
- **`native_enum=False`** → adding a RequestStatus member needs NO CHECK migration (precedent `41d427bd38e6`).
- **Tests build schema via `Base.metadata.create_all`** (`tests/services/conftest.py`), NOT Alembic → every index/constraint change mirrored in BOTH `models.py` AND the migration.
- **guessit parses `episode`** same shape as `season`; add `_coerce_episode` sibling to `source_mapping._coerce_season` (l.301-319).
- **openapi gate**: `frontend` `gen:check` diffs freshly-generated `schema.d.ts`. Every backend schema/path change → `make openapi && make gen-client` before frontend compiles.

## Headline architecture decisions
1. TV `MediaRequest` = the show; new-used `SeasonRequest(media_request_id, season_number, status)` = per-season lifecycle unit. `CreateRequestBody.seasons: list[int]|None` (omitted/empty = whole aired series). `MediaRequest.status` = **computed rollup** of season statuses via pure `domain/season_rollup.rollup_status`, persisted after every season transition. New `RequestStatus.partially_available`.
2. `Download.season` non-null for TV, null for movies (already true) = the discriminator at the service layer.
3. Grabs stay **per-season**, never per-show. Whole-series = N SeasonRequest rows searched/grabbed/imported independently → S1 and S2 download concurrently → widen one-active-download invariant to one-active-per-(request, season).
4. Import validation: NEW sibling `validate_season_import` (validate EVERY file, partial success), not reshaping `validate_import`.
5. Plex TV availability is **per-season** (`leafCount>0`), not per-episode, for the beta. Per-episode completeness = scoped follow-up.
6. Season-pack scoring additive & gated: new `prefer_season_pack: bool=False` param to `decide()`, only True when operator requests a whole season.

## 3. TV naming — `domain/naming.py` (extend, pure stdlib)
- `plex_tv_show_relative_dir(title, year) -> 'Series (Year)'`
- `plex_tv_season_relative_dir(title, year, season) -> 'Series (Year)/Season NN'` (season=0 → 'Season 00' free via `f"Season {season:02d}"`)
- `_episode_token(season, episodes) -> 'S02E05'` or `'S02E05-E07'` (sorted dash range for multi-ep files; raise on empty)
- `plex_tv_episode_relative_path(title, year, season, episodes, ext) -> 'Series (Year)/Season NN/Series - sNNeMM[-eKK].ext'` (filename omits year, Sonarr default; reuse existing `clean_title`)
- Tests: normal, multi-ep, specials (Season 00), colon title, year=None.

## 4. Scoping model
- No new tables. `SeasonRequest` created lazily/idempotently at grab time (like `Download`). `Download.episodes_json`: None = import all valid; `[4,5,6]` = only those, skip rest silently. No FK Download→SeasonRequest; pairing = tuple `(media_request_id, season)`.
- **`domain/season_rollup.py`** (new, pure): `rollup_status(season_statuses) -> str`. Precedence `("import_blocked","downloading","searching","no_acceptable_release","completed")` wins outright; else all-available→available; mix-with-available→partially_available; any-pending→pending; all-failed→failed. Bare string literals.
- **`RequestStatus.partially_available = "partially_available"`** in models.py. No CHECK migration. Include in `uq_media_requests_active` predicate (still in-flight, blocks dup). Keep OUT of `TERMINAL_REQUEST_STATUS_VALUES` (`request_service.py:47-49`) and `_SETTLED_REQUEST_STATUSES` (`repositories/requests.py:23-25`).
- **`ports/repositories.py`**: add `SeasonRequestRecord`(id, media_request_id, season_number, status, tmdb_id denormalized) + `SeasonRequestRepository` Protocol: get, list_for_request, list_by_status, ensure(idempotent IntegrityError-safe), set_status, mark_completed, mark_available.
- **`repositories/season_requests.py`** (new): `SqlSeasonRequestRepository` mirrors `SqlRequestRepository` incl. ensure() IntegrityError-catch-and-reread.
- **`services/season_request_service.py`** (new): ensure_seasons (per season: library.is_available(tmdb,"tv",season=n)→'available' else 'pending'), set_status, mark_completed, mark_available, mark_no_acceptable_release, _recompute_parent (re-read all season statuses → rollup_status → persist onto MediaRequest, SAME transaction).
- **`request_service.create_request`**: add `seasons: list[int]|None=None`. For tv: run ensure_seasons for (seasons verbatim OR range(1, season_count+1), specials excluded) on EVERY call incl. dedup path (restructure so ensure_seasons runs after resolving record, not inside early return). `CreateRequestBody.seasons`; router threads it.

## 5. TV import
- **Widen `uq_downloads_active_request`** (models.py:280-292): unique on `(media_request_id, COALESCE(season, -1))`. Plain `(media_request_id, season)` FAILS (NULL!=NULL lets concurrent movie grabs stop colliding). COALESCE(-1) sentinel keeps movie guarantee + real per-season TV uniqueness.
- `repositories/downloads.py::find_active_for_request(media_request_id, season=None)` filters `Download.season==season` when given. Update Protocol + call sites: `grab_service.grab` (pass season), `queue.py::grab_endpoint` no_acceptable branch (pass body.season).
- **`grab_service.grab`**: terminal write → if season: `season_request_service.set_status(...downloading)` else request set_status. Add `episodes: list[int]|None=None` → `Download.episodes_json` (thread through `download_repo.create`).
- **`queue_service`**: `_handle_failed` AND `mark_failed` currently `request_repo.set_status(...searching)` unconditionally → route TV through `season_request_service.set_status(...searching)` when `record.season is not None` (both call sites).
- **`ports/filesystem.py`**: add `list_video_files(root) -> list[tuple[str,int,str]]` (abs, size, folder-qualified rel). Implement in `local.py` by refactoring `largest_video_file`'s walk (symlink containment, extras/sample pruning reused) into shared `_iter_video_files`.
- **`import_service.py`**: split the `media_type != "movie"` block → `if movie: existing path UNCHANGED; elif tv: if tv_root None → _block("tv library root is not configured"); else _import_tv_locked(...)`.
- **`_import_tv_locked`** (new, mirrors `_import_download_locked`): resolve content via `_resolve_content`; enumerate files via `fs.list_video_files` (asyncio.to_thread) with the anchor-above-content-root rel-path trick; `validate_season_import(expected_season=download.season, requested_episodes=download.episodes_json)`; if accepted empty → `_block` with capped joined per-file reasons; else claim Importing (CAS update_status_if_in); one `import_started` history; per accepted file `dst = tv_root / plex_tv_episode_relative_path(...)`, reuse generic `_place_file(fs, src, dst)`; one `imported` history row per accepted episode (message "S02E05 -> Season 02/..."); ONE `trigger_scan(season_dir_abs, "tv")` after all placements; on scan failure roll back only files placed this call, `_block`; on success finalize Imported (CAS) + `season_request_service.mark_completed(...)`. Rejected files logged (not persisted) unless whole-season blocks. Idempotent (skip-if-same-size). `download_path` stamped with season folder for observability.
- **`run_import_cycle`** gains `tv_root: str|None`. **`run_availability_cycle`**: keep movie loop; ADD loop over `SeasonRequest` rows in `completed` → `library.is_available(tmdb,"tv",season=n)` → `season_request_service.mark_available`, catch Plex/NotImplemented → rollback+warn.
- **`web/app.py::_reconcile_once`**: fetch tv_root alongside movies_root; change guard from `if library and movies_root:` (skips whole cycle incl TV when movies_root unset) to `if library:` always, passing both (possibly-None) roots → each type surfaces its own honest per-row block. `queue.py::import_endpoint` depends on both optional roots (no upfront 409).

## 6. TV import validation — `domain/import_validation.py` (extend, new fn)
- New `ImportRejectionReason.NO_EPISODE_NUMBER = "no_episode_number"`.
- DTOs: `EpisodeImportResult(video, parsed, episodes: tuple[int,...])`, `EpisodeImportRejection(relative_path, reason, detail)`, `SeasonImportValidation(accepted, rejected, skipped_not_requested)`.
- `validate_season_import(files, *, parser, profile, expected_title, expected_tmdb_id, expected_season, requested_episodes=None)`: per file — drop sample/extras (`_looks_like_sample_name`); parse folder-qualified rel path; `matches_media(..., expected_season=expected_season)` (wrong-season file = WRONG_MEDIA); quality hard gate (same profile check); sample-floor size (50 MiB / unknown-rejects); NEW episode-number gate (parsed.episode None → NO_EPISODE_NUMBER); requested_episodes filter → no overlap = skipped_not_requested (benign, distinct bucket), any overlap keeps multi-ep file. Partial legitimately allowed.
- **`domain/release.py`**: add `episode: int|list[int]|None=None` to `ParsedRelease` (mirror season shape).
- **`domain/source_mapping.py`**: add `_coerce_episode` (mirror `_coerce_season`), wire into `to_parsed_release`.
- Tests: full-accept pack, mixed accept/reject (one CAM ep), wrong-season-in-pack, sample/NFO filter, requested-episodes skip, multi-ep overlap.

## 7. Plex TV availability + scan — `adapters/plex/library.py`
- New endpoint `GET /library/metadata/{ratingKey}/children` (verified vs overseerr `server/api/plexapi.ts:217-223`): returns season Metadata[] with `index` (season#) + `leafCount` (episode count).
- **`ports/library.py`**: widen `is_available(tmdb_id, media_type, *, use_cache=True, season: int|None=None)`; `trigger_scan(path, media_type: Literal["movie","tv"])` now required.
- `_collect_present_tv_seasons() -> dict[int, frozenset[int]]`: page show-type sections (`/all?includeGuids=1`), extract show tmdb via existing `_extract_tmdb_ids`, then `/library/metadata/{ratingKey}/children`, parse index/leafCount (season with leafCount>0). New `_TV_SEASONS_CACHE: _TtlCache` (same key/pattern/positive-only discipline as `_PRESENT_TMDB_CACHE`).
- `is_available(tv, season=None)` → show present; `season=N` → season in map. Same use_cache semantics (trust cached PRESENT, never cached ABSENT).
- `trigger_scan(path, media_type)`: `relevant = [s for s in sections if s.type == ("show" if tv else "movie")]`; path-prefix match within relevant; full-refresh-fallback scoped to relevant; raise PlexLibraryError only if relevant empty.
- Call sites: movie `trigger_scan(str(dst.parent), "movie")`; TV `trigger_scan(str(season_dir_abs), "tv")`. `tests/web/fakes.py::FakeLibrary` widened.
- Deferred: true per-episode completeness.

## 8. Season-pack scoring — `domain/season_pack.py` (new)
- `classify_release_scope(parsed) -> Literal["single_episode","season_pack","multi_season_pack","unknown"]`: season list len>1→multi; season int & episode None→season_pack; season int & episode set→single_episode; season None→unknown.
- **`decision_engine.decide`**: add `prefer_season_pack: bool=False`. Byte-identical default. When True: `_compare` tiebreak → compare_by_profile (dominant) → scope preference (season_pack wins) → seeders → size. Add matching `_SCOPE_WEIGHT` (~1e9, between index & seeder weights) to `ScoredRelease.score`.
- **`decision_service.preview`**: add `episodes: list[int]|None`. `prefer_season_pack = media_type=="tv" and season is not None and not episodes`. If `len(episodes)==1` set `IndexerSearchRequest.episode=str(episodes[0])` (Prowlarr adapter already forwards season/ep params l.233-236). `SearchPreviewRequest`/`GrabRequest` gain `episodes`.

## 9. TMDB + discovery
- **`ports/metadata.py`**: add `trending_tv(page=1)`, `popular_tv(page=1)` → MediaPage. (No tv "upcoming".)
- **`adapters/tmdb/adapter.py`**: refactor `_movie_page` → generic `_list_page(path, cache_prefix, page, kind)`; movie methods become wrappers; add `trending_tv`→`/trending/tv/week`, `popular_tv`→`/tv/popular`.
- **`services/discovery_service.py`**: widen `_ROWS` (+`("trending_tv","Trending TV this week")`, `("popular_tv","Popular TV shows")`) and `DiscoverCategory`; `list_category` branches. `DiscoverHomeRow.row_type` already open string (no contract change); `GET /discover/{category}` Literal widens additively → regen openapi/schema.

## 10. Settings — `tv_root` OPTIONAL everywhere (mirror movies_root)
- `web/deps.py::KNOWN_SETTING_KEYS += "tv_root"`; add `get_tv_root` (409 if unset) + `get_tv_root_optional`.
- `web/schemas.py`: `SettingsResponse`/`SettingsUpdate` += `tv_root: str|None`; `SetupCompleteRequest` += `tv_root: str|None` (optional).
- `routers/setup.py::complete`: write tv_root only when non-empty.
- **`PlexLibraryOption`** gains `section_type: Literal["movie","tv"]`. `setup_validation.movie_library_options` → generalized `library_options(sections, probe_writable)` returning both tagged by type; `validate_plex` hard fail becomes "no movie AND no tv library at all" (movie-only or tv-only Plex is legit).
- `routers/settings.py::plex_libraries_endpoint` returns generalized list. No migration (settings is key/value).
- Frontend: `SetupWizard.tsx` add optional "TV Library" section (filter `section_type==='tv'`, don't gate allVerified); `Settings.tsx` mirror Library section with tv_root field + picker.

## 11. Frontend (all types flow from regenerated schema.d.ts)
- `web/schemas.py::QueueItem` gains `season: int|None`, `episodes: list[int]|None`; `queue.py::_to_item` maps from `Download`.
- `routers/requests.py`: `RequestResponse` gains `seasons: list[SeasonStatus]|None`; `_to_response` becomes async, fetches `list_for_request` for tv rows only (batch to avoid N+1 on the list). `RequestRecord` (services DTO) unchanged.
- `TitleDetailModal.tsx`: tv → season selector before Request/Preview (needs season_count; simplest small `GET /discover/tv/{tmdb_id}` or extend detail). Thread selected season(s) into `CreateRequestBody.seasons`/`SearchPreviewRequest.season`. Reuse state machine per season; `liveRequest` correlation becomes season-aware (match media_type/tmdb_id AND season vs `RequestResponse.seasons`).
- `ReleaseList.tsx`: optional season/episode chip from `scored.parsed`.
- `Discover.tsx`: no change (Row/Spotlight/PosterCard media_type-agnostic; new rows render via open row_type).
- `Queue.tsx`: "S02E05"/"S02 pack" badge when `item.season != null`.
- `Requests.tsx`: per-season status list for tv rows.
- `lib/status.ts`: add `partially_available: {label:'Partially available', intent:'available'}`.

## 12. Migration — ONE revision `<rev>_tv_season_support.py` (mirror in models.py)
1. `uq_media_requests_active`: drop/recreate (pattern `41d427bd38e6`) with `'partially_available'` in predicate.
2. `uq_downloads_active_request`: drop/recreate unique on `(media_request_id, COALESCE(season,-1))`, same predicate.
3. New `uq_season_requests_media_season`: unique on `season_requests(media_request_id, season_number)` (no WHERE) — makes ensure() race-safe.
- downgrade reverses all three. **VERIFY expression-index syntax** (`sa.func.coalesce` as Index arg + op.create_index equivalent) on SQLite AND Postgres — the one piece not proven by existing precedent.

## 13. Dependency-ordered build sequence
1. Domain: naming → release(episode) → source_mapping(_coerce_episode) → import_validation(validate_season_import) → season_pack → season_rollup. (Risk: matches_media season-gate reuse.)
2. Schema: models.py (enum, both index redefs, SeasonRequest unique) + migration, verified SQLite (+Postgres if feasible). Risk: expression-index; explicit concurrent-movie-dup regression test.
3. Repos/ports: SeasonRequestRecord/Protocol, widened DownloadRepository.find_active_for_request/.create, season_requests.py, downloads.py season-aware; library/metadata/filesystem port widenings.
4. Adapters: local.py (list_video_files), tmdb (tv methods), plex (season presence + generalized trigger_scan). Highest external risk — cross-check /children leafCount.
5. Services: season_request_service → request_service(seasons) → grab_service(season status, episodes) → queue_service(season re-arm both paths) → decision_service(episodes/prefer_season_pack) → decision_engine(scoring) → import_service(_import_tv_locked, cycle) → discovery_service(tv rows).
6. Web: schemas → routers(requests/queue/discovery/setup/settings) → deps(tv_root) → app.py(reconcile). Risk: RequestResponse.seasons async N+1.
7. openapi: `make openapi && make gen-client` BEFORE any .tsx. (gen:check catches stale schema.)
8. Frontend UI: TitleDetailModal season selector (highest UI risk — season correlation) → Queue/Requests badges → Settings/SetupWizard tv_root pickers.

## 14. Test plan (highlights)
- Domain pure: naming, import_validation_tv, season_pack, season_rollup; domain_purity stays green.
- Adapters: local(list_video_files), tmdb(trending/popular_tv MockTransport + cache isolation), plex(is_available tv season present/absent/show-absent/leafCount=0; trigger_scan tv matched/fallback/no-section).
- Repos: season_request_repository(ensure idempotency+concurrent IntegrityError), download_repository(S1+S2 concurrent OK; same-season collides; two movie season=NULL still collide — COALESCE regression guard).
- Services: season_request_service(rollup), grab_service(episodes_json, concurrent seasons), decision_service(prefer_season_pack only whole-season tv), import_service_tv(happy multi-file+one scan+mark_completed; partial-accept; full-block; tv_root unset block).
- Web: requests(seasons creates N rows; 2nd POST grows set), queue(grab episodes, import tv_root-unset honest block), discovery(new categories), setup/settings(optional tv_root, generalized plex-libraries).
- Frontend: Settings.test tv fields; TitleDetailModal season correlation (assert movie path byte-identical + new per-season tests).
