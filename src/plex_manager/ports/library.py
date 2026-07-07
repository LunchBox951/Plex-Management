"""LibraryPort — the media-server interface (Plex in v1).

Defined now, stubbed in the alpha: the import/availability pipeline is deferred,
but the reconciler and import service are written against this Protocol so the
wiring is a drop-in later. All methods are async.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = ["LibraryPort", "LibrarySection", "WatchState"]


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
    """

    model_config = ConfigDict(frozen=True)

    watched: bool
    last_viewed_at: datetime | None = None


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
        """
        raise NotImplementedError
