"""LibraryPort — the media-server interface (Plex in v1).

Defined now, stubbed in the alpha: the import/availability pipeline is deferred,
but the reconciler and import service are written against this Protocol so the
wiring is a drop-in later. All methods are async.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = ["LibraryPort", "LibrarySection"]


class LibrarySection(BaseModel):
    """A library section (Plex "library") the server exposes."""

    model_config = ConfigDict(frozen=True)

    key: str
    title: str
    type: Literal["movie", "show"]
    locations: tuple[str, ...] = ()


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
