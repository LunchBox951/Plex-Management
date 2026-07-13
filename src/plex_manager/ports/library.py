"""LibraryPort — the media-server interface (Plex in v1).

Defined now, stubbed in the alpha: the import/availability pipeline is deferred,
but the reconciler and import service are written against this Protocol so the
wiring is a drop-in later. All methods are async.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = ["LibraryPort", "LibrarySection", "WatchState", "WatchStateQuery"]


class LibrarySection(BaseModel):
    """A library section (Plex "library") the server exposes."""

    model_config = ConfigDict(frozen=True)

    key: str
    title: str
    type: Literal["movie", "show"]
    locations: tuple[str, ...] = ()


class WatchState(BaseModel):
    """Plex watch status for one movie or TV season (ADR-0012 eviction input).

    ``last_viewed_at`` is ``None`` when Plex has never recorded a view, in which
    case ``watched`` MUST also be ``False`` -- an implementation must never report
    an inconsistent ``watched=True`` with no timestamp; the eviction domain
    (``domain/eviction.py``) treats a missing timestamp as never-eligible
    regardless of ``watched``, so this keeps the two signals honestly aligned at
    the source.

    ``last_viewed_at`` is always tz-AWARE once constructed (issue #82): eviction
    and retention-telemetry both subtract it against UTC-aware cutoffs
    (``domain/eviction.py``, ``services/retention_telemetry_service.py``'s own
    ``_as_utc``), and a naive value straight off a careless adapter/fake would
    raise ``TypeError`` deep inside that arithmetic instead of at this boundary.
    A naive input is NORMALIZED by re-attaching UTC -- mirroring the identically
    named ``_as_utc`` idiom already used at every other naive-datetime boundary in
    this codebase (``repositories/downloads.py``, ``repositories/log_events.py``,
    ``repositories/requests.py``, ``repositories/season_requests.py``,
    ``services/retention_telemetry_service.py``) -- rather than rejected: every one
    of those precedents treats "naive but was always meant as UTC" as the honest
    normalization, never a hard failure over a timezone a well-behaved caller
    simply forgot to attach.
    """

    model_config = ConfigDict(frozen=True)

    watched: bool
    last_viewed_at: datetime | None = None

    @field_validator("last_viewed_at")
    @classmethod
    def _normalize_naive_to_utc(cls, value: datetime | None) -> datetime | None:
        """Re-attach UTC to a naive ``last_viewed_at`` (see class docstring)."""
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class WatchStateQuery(BaseModel):
    """One item's watch-state lookup within a :meth:`LibraryPort.resolve_watch_states`
    batch (ADR-0012 eviction candidate assembly, issues #213/#238).

    Carries exactly the same three positional/keyword inputs a single
    :meth:`LibraryPort.watch_state` call takes -- ``tmdb_id``, ``media_type``, and
    (TV-only) ``season`` -- plus the ``library_path`` deletion-target breadcrumb
    (issue #207) the path-correlated read resolves against. ``season`` is REQUIRED
    for ``media_type='tv'`` (eviction is always per-season) and ignored for movies,
    mirroring :meth:`LibraryPort.watch_state`'s own contract; an implementation
    raises ``ValueError`` for a TV query with ``season=None`` exactly as the
    single-item method does.
    """

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    media_type: Literal["movie", "tv"]
    season: int | None = None
    library_path: str | None = None


@runtime_checkable
class LibraryPort(Protocol):
    """Query availability, trigger scans, and list sections on the media server."""

    async def is_available(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        use_cache: bool = True,
        season: int | None = None,
    ) -> bool:
        """Return whether the item is already present in the library.

        ``use_cache=False`` forces a fresh read of the server, bypassing any
        cached-presence fast path. The request-dedup path passes it so a title just
        REMOVED from the library is seen as absent immediately, instead of a stale
        "present" answer held for the cache TTL.

        ``season`` (TV only) scopes the lookup to a single season: present means
        that season's ``leafCount>0`` on the show in Plex, the per-season
        availability granularity used by the TV beta (per-episode completeness is
        a deferred follow-up). Ignored for movies and for a whole-show TV check
        (``season=None``).
        """
        raise NotImplementedError

    async def present_seasons(self, tmdb_id: int) -> frozenset[int]:
        """Return the season numbers already present for a show, from ONE library read.

        A season is "present" when it has at least one episode indexed
        (``leafCount>0``) — the same per-season granularity as :meth:`is_available`
        with a ``season``. Provided ALONGSIDE ``is_available`` so a caller checking
        many seasons of one show (``season_request_service.ensure_seasons``) pays a
        SINGLE library crawl instead of one per season. Always reflects the library
        as it is NOW (like ``is_available(use_cache=False)`` — never trusts a cached
        absence); empty when the show is absent or has no indexed season.

        NOTE: this crawls EVERY show section's full ``/all`` listing to build the
        whole-library season map (see the adapter's ``_collect_present_tv_seasons``).
        A caller that only needs a KNOWN set of shows' seasons should use
        :meth:`season_presence` instead — it still costs exactly one page-walk, but
        answers only the requested ids rather than the whole library's map.
        """
        raise NotImplementedError

    async def season_presence(self, tmdb_ids: Collection[int]) -> Mapping[int, frozenset[int]]:
        """Return the season numbers present for EACH show in ``tmdb_ids``, via ONE
        BATCH-shaped targeted lookup.

        Unlike :meth:`present_seasons` (which crawls every show section's FULL
        listing to answer for ANY one show, repeated per caller), this resolves
        ALL of ``tmdb_ids`` from a SINGLE page-walk across every show section —
        cost model: one page-walk total, plus one ``/children`` fetch per matched
        item (not per requested id — a show may have more than one matching item,
        see below) — the batch availability reconcile
        (``import_service.run_availability_cycle``) depends on this to check every
        distinct pending show in a tick without re-paging the library once per
        show. A tmdb id absent from every show section maps to an empty
        ``frozenset`` (never omitted from the returned mapping).

        A show can legitimately have MORE THAN ONE matching item across (or within)
        show sections — e.g. the same title catalogued in both a "TV Shows" and an
        "Anime" section, or a duplicate entry in one section — so an implementation
        MUST union the present seasons across every item matching a given tmdb id,
        never just the first match. Returning only the first hit's seasons can
        under-report a season that is actually present on a later duplicate,
        stranding it at "Finalizing" forever.

        Always reads FRESH (like ``present_seasons`` — never trusts a cached
        absence): a season that just finished indexing must be seen on the very
        next check, not held stale for a cache TTL.

        Failure isolation (round 4, #136 review): a requested id is present as a
        KEY in the returned mapping only when its lookup SUCCEEDED — an empty
        ``frozenset`` genuinely means "no seasons present", while an id OMITTED
        from the mapping means its lookup FAILED and the caller must treat it as
        unknown/retry-next-cycle, never as "not yet available". This matters
        because one show's underlying metadata lookup can fail independently
        (e.g. a row deleted between an earlier crawl and this lookup, or a
        persistently bad row returning 404/500) without that being a genuine
        whole-batch transport failure — an implementation MUST isolate a single
        show's lookup failure from the rest of the batch rather than letting it
        abort every other requested id. A whole-batch transport failure (the
        page-walk itself failing) is still allowed to raise
        ``PlexLibraryError``/``PlexAuthError`` — the caller's own try/except
        around the whole call handles that, leaving every requested id
        unresolved for that tick.
        """
        raise NotImplementedError

    async def present_ids(
        self,
        keys: Sequence[tuple[int, Literal["movie", "tv"]]],
        *,
        refresh_absent: bool = False,
    ) -> frozenset[tuple[int, Literal["movie", "tv"]]]:
        """Return the subset of ``(tmdb_id, media_type)`` pairs present in the library.

        The BATCH presence accessor for tile decoration (Discover/Search) AND for
        the availability reconcile cycle: a whole page's (or tick's) keys are
        answered from AT MOST one movie crawl plus one show crawl total -- never
        one library read per title (the prototype's "20 tiles = 20 crawls"
        anti-pattern).

        ``refresh_absent=False`` (the default, used by tile decoration): trusts a
        warmed snapshot as-is, even if it does not contain one of the queried keys
        -- tiles are HINTS and tolerate the short presence-cache staleness, so a
        miss pages Plex once and warms the cache for the next page-load, but a hit
        is never re-verified. The authoritative fresh dedup decision stays on the
        create path (``is_available(use_cache=False)``), never here.

        ``refresh_absent=True`` (used by the availability reconcile cycle,
        ``import_service.run_availability_cycle``): trusts a cached PRESENCE but
        never a cached ABSENCE for a queried key, mirroring ``is_available``'s
        contract at batch granularity -- a warmed snapshot that does not confirm
        EVERY queried key as present triggers exactly one fresh crawl before
        answering (never per-key, never more than one crawl per call). This
        matters because a Plex partial scan is asynchronous: the scan-triggered
        cache invalidation (``trigger_scan``) can be followed by a reconcile tick
        that pages Plex BEFORE indexing finishes, caching that miss; without
        ``refresh_absent`` a subsequent tick would trust that stale absence for
        the rest of the cache TTL instead of promoting the title on the very next
        tick after it actually finishes indexing.

        Presence is SHOW-LEVEL for TV (the show is in the library), the granularity
        a tile needs -- per-season detail stays in the title modal. Only ever yields
        ``"available"``/absent per key: a request-rollup status
        (``requested``/``processing``/``partially_available``) is NOT a presence
        concept and comes from the request store, not here. A section type is only
        crawled when at least one requested key is of that type.
        """
        raise NotImplementedError

    async def confirm_paths(
        self,
        media_type: Literal["movie", "tv"],
        library_paths: Collection[str],
    ) -> frozenset[str]:
        """GUID-INDEPENDENT fallback: confirm ``library_paths`` by DIRECTORY-PREFIX
        match against the file path(s) Plex reports for its indexed items (issue
        #158).

        Some titles are matched by Plex's metadata provider to an item that carries
        no ``tmdb://`` (or even a WRONG/unrelated) guid -- new/obscure releases, or a
        provider mismatch -- so GUID-based confirmation (:meth:`present_ids`)
        can never succeed for them, no matter how long the import cycle waits.
        This is the app's OWN fallback: it knows exactly which folder it placed a
        completed download's file(s) into (the ``library_path`` breadcrumb,
        ADR-0012), so it can ask "did Plex index a file *there*", independent of
        which (or whether any) guid Plex's provider assigned.

        ``library_paths`` are CONTAINER-namespace directories (a movie's folder, or
        a TV season's directory) -- never a bare file. Each is confirmed when SOME
        item's reported file path, after translating the section's own
        HOST-namespace location the same way :meth:`trigger_scan` reverses it, sits
        AT or BELOW that directory. Matching is PURELY by path -- never by title or
        year -- so a same-title/same-year but genuinely different file can never
        false-confirm.

        Batched like :meth:`present_ids`/:meth:`season_presence`: every requested
        path is answered from ONE crawl of the relevant (movie or show) sections,
        never one crawl per path -- a caller with many pending rows in one tick
        still costs a single pass. Returns the SUBSET of ``library_paths`` that
        confirmed; a path with no match (including one under no known section at
        all) is simply absent from the result, never raised. A genuine crawl
        failure (the section walk itself failing) is allowed to raise
        ``PlexLibraryError``/``PlexAuthError`` -- the caller's own try/except
        handles that exactly like :meth:`present_ids`'s batch failure, leaving
        every queried path unconfirmed for this tick's retry.
        """
        raise NotImplementedError

    async def trigger_scan(self, path: str, media_type: Literal["movie", "tv"]) -> None:
        """Ask the media server to scan ``path`` (partial-scan when supported).

        ``media_type`` scopes which library sections are candidates for the
        ``path``-prefix match (movie sections for movies, show sections for TV),
        so a TV season folder is never matched against a movie section (or vice
        versa) and the full-refresh fallback stays scoped to the relevant kind.

        Raises ``NotImplementedError`` by default (issue #81): a silent no-op
        default would let a future adapter or fake falsely report a completed
        Plex scan after an import or purge, so a missing override must fail
        loudly at call time instead.
        """
        raise NotImplementedError

    async def list_sections(self, *, use_cache: bool = True) -> list[LibrarySection]:
        """Return the configured library sections.

        ``use_cache=False`` forces a fresh read of the server, bypassing the
        adapter's own TTL-cached snapshot -- for callers where staleness itself
        is user-visible (the Settings library picker, the setup wizard's "Test
        connection", the health dashboard's live probe), never for the warmed
        fast paths (``is_available``, ``trigger_scan``, ``watch_state``) that
        rely on this staying cheap.
        """
        raise NotImplementedError

    async def watch_state(
        self,
        tmdb_id: int,
        media_type: Literal["movie", "tv"],
        *,
        season: int | None = None,
        library_path: str | None = None,
    ) -> WatchState:
        """Return whether ``tmdb_id`` (optionally one TV season) has been watched.

        Movie (``media_type='movie'``): watched means Plex's ``viewCount>0`` for
        the item; ``season`` is ignored. TV (``media_type='tv'``): ``season`` is
        REQUIRED -- eviction is always per-season (mirroring ``is_available``'s
        per-season granularity), never whole-show -- and watched means every
        episode of that season has been viewed (``viewedLeafCount == leafCount``
        on Plex's season metadata). ``last_viewed_at`` is the item's/season's Plex
        ``lastViewedAt``.

        An item absent from the library (never imported, or removed) reports
        ``watched=False, last_viewed_at=None`` honestly rather than raising --
        it can never be an eviction candidate anyway, so there is nothing to
        recover from by treating it as an error.

        ``library_path`` (issue #207) is the ADR-0012 deletion-target breadcrumb
        stored on the request/season row. When PROVIDED, the implementation MUST
        resolve watch state ONLY from the Plex item(s) whose reported media file
        path corresponds to ``library_path``, and MUST FAIL CLOSED --
        ``WatchState(watched=False, last_viewed_at=None)`` -- when the target is
        absent, or ambiguous across items that report DIFFERENT underlying media
        file paths (e.g. the same title imported into two sections as genuinely
        distinct copies on disk). It MUST NEVER union "watched anywhere" across
        such genuinely distinct duplicates: a watched duplicate must never
        authorize evicting an unwatched one.

        Issue #239: more than one correlated item that all report the IDENTICAL
        set of underlying media file paths is NOT the ambiguous case above -- it
        is the SAME physical copy merely indexed by more than one Plex section
        (e.g. a broad section plus a nested section both covering the same
        files), and the implementation MUST treat it as one logical item rather
        than failing closed: merged ``watched`` is ``True`` if ANY such hit is
        watched, and the merged ``last_viewed_at`` is the NEWEST watched
        timestamp among them -- never the oldest, so a section that is slow to
        reflect a rewatch can never make a recent rewatch look stale enough to
        fall inside eviction's grace-window deletion criteria. Only hits whose
        reported file paths genuinely differ stay fail-closed.

        ``library_path=None`` keeps the legacy UNCORRELATED first-match read --
        only for callers with no known target, e.g. a row predating the
        breadcrumb.
        """
        raise NotImplementedError

    async def resolve_watch_states(self, queries: Sequence[WatchStateQuery]) -> list[WatchState]:
        """Resolve MANY watch-state lookups from ONE crawl of each relevant section
        (issues #213/#238) -- the batch counterpart of :meth:`watch_state`.

        Returns a list aligned 1:1 with ``queries`` (same length, same order): the
        Nth :class:`WatchState` is exactly what :meth:`watch_state` would return for
        the Nth :class:`WatchStateQuery`'s ``tmdb_id``/``media_type``/``season``/
        ``library_path``. Each result is byte-for-byte identical to the per-item
        method's -- this method changes only HOW MANY server round-trips the whole
        set costs, never WHAT any one lookup resolves to (the merge-correctness
        contract in :meth:`watch_state`'s docstring, incl. issue #239, is preserved
        verbatim). An empty ``queries`` touches no network and returns ``[]``.

        Cost model (the whole reason this exists): eviction candidate assembly used
        to call :meth:`watch_state` once PER candidate, and each such call re-paged
        the whole Plex section from offset zero (``Theta(candidates * section size)``,
        going quadratic as the tracked set scales with the library -- issue #213).
        This crawls each relevant section ONCE for the whole batch (a memoized
        tmdb-id index), reads each distinct show's ``/children`` season listing at
        most ONCE (not once per candidate season -- issue #213), and reads each
        distinct season's episode ``/children`` at most once (folding in issue
        #238's path-correlated per-candidate re-crawl). Net: ``O(sections + distinct
        shows + distinct seasons)`` round-trips, independent of the candidate count.

        Like :meth:`watch_state`, it reads FRESH every call (deliberately uncached):
        the snapshot is consistent WITHIN one assembly pass -- exactly the "one
        fresh section crawl per media type per candidate-assembly pass" issue #213
        specifies -- while separate sweeps still each observe fresh Plex state. The
        pre-claim re-read in ``eviction_service._evict_one`` stays a per-candidate
        :meth:`watch_state` call, so the rewatch-during-sweep guard (issue #209) is
        untouched. A ``media_type='tv'`` query with ``season=None`` raises
        ``ValueError``, exactly as :meth:`watch_state` does.
        """
        raise NotImplementedError
