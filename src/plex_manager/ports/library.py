"""LibraryPort — the media-server interface (Plex in v1).

Defined now, stubbed in the alpha: the import/availability pipeline is deferred,
but the reconciler and import service are written against this Protocol so the
wiring is a drop-in later. All methods are async.
"""

from __future__ import annotations

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
        """
        raise NotImplementedError

    async def trigger_scan(self, path: str, media_type: Literal["movie", "tv"]) -> None:
        """Ask the media server to scan ``path`` (partial-scan when supported).

        ``media_type`` scopes which library sections are candidates for the
        ``path``-prefix match (movie sections for movies, show sections for TV),
        so a TV season folder is never matched against a movie section (or vice
        versa) and the full-refresh fallback stays scoped to the relevant kind.
        """

    async def list_sections(self) -> list[LibrarySection]:
        """Return the configured library sections."""
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
