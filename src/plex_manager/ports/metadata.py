"""MetadataPort — the media-metadata interface (TMDB in the alpha).

Kept deliberately thin: the alpha only needs to search, and to resolve a movie or
TV show by tmdb id. The DTOs are the cross-boundary contract; the adapter maps
TMDB's JSON into them. All methods are async (``httpx.AsyncClient``).
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "EpisodeInfo",
    "MediaPage",
    "MediaSearchResult",
    "MetadataPort",
    "MovieMetadata",
    "TvMetadata",
]

MediaKind = Literal["movie", "tv"]


class MediaSearchResult(BaseModel):
    """One row of a discovery search result."""

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    media_type: MediaKind
    title: str
    year: int | None = None
    overview: str | None = None
    poster_url: str | None = None
    backdrop_url: str | None = None


class MediaPage(BaseModel):
    """One page of a paginated discovery list (trending / popular / upcoming)."""

    model_config = ConfigDict(frozen=True)

    page: int
    total_pages: int
    total_results: int
    # Immutable tuple (issue #106): the TMDB adapter's ``_page_cache`` stores this
    # exact ``MediaPage`` instance and hands it back BY REFERENCE on every cache
    # hit within the TTL -- a mutable ``results`` list would let one caller's
    # in-place mutation (``.append``/``.sort``/...) corrupt what every later cache
    # hit sees. A ``list`` input is coerced by pydantic.
    results: tuple[MediaSearchResult, ...]


class MovieMetadata(BaseModel):
    """Resolved movie details needed to drive a request/search."""

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    imdb_id: str | None = None
    title: str
    year: int | None = None
    overview: str | None = None
    poster_url: str | None = None
    backdrop_url: str | None = None
    is_anime: bool = False


class TvMetadata(BaseModel):
    """Resolved TV-show details needed to drive a request/search."""

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    tvdb_id: int | None = None
    imdb_id: str | None = None
    title: str
    year: int | None = None
    overview: str | None = None
    poster_url: str | None = None
    backdrop_url: str | None = None
    season_count: int = 0
    is_anime: bool = False


class EpisodeInfo(BaseModel):
    """One episode of a TV season: its number and (if known) air date.

    Used by the episode-level fallback (ADR-0020, issue #178) to compute the
    aired-episode target set. ``air_date`` is ``None`` when TMDB hasn't dated the
    episode yet -- treated by the domain as "not yet aired", never guessed.
    """

    model_config = ConfigDict(frozen=True)

    episode_number: int
    air_date: date | None = None


@runtime_checkable
class MetadataPort(Protocol):
    """Search for media and resolve movie / TV details by tmdb id."""

    async def search(self, query: str, year: int | None = None) -> list[MediaSearchResult]:
        """Search by free text, optionally constrained to ``year``."""
        raise NotImplementedError

    async def get_movie(self, tmdb_id: int) -> MovieMetadata | None:
        """Resolve a movie by tmdb id, or ``None`` if not found."""

    async def get_tv_show(self, tmdb_id: int) -> TvMetadata | None:
        """Resolve a TV show by tmdb id, or ``None`` if not found."""

    async def trending_movies(self, page: int = 1) -> MediaPage:
        """List the week's trending movies, one page at a time."""
        raise NotImplementedError

    async def popular_movies(self, page: int = 1) -> MediaPage:
        """List currently popular movies, one page at a time."""
        raise NotImplementedError

    async def upcoming_movies(self, page: int = 1) -> MediaPage:
        """List upcoming movie releases, one page at a time."""
        raise NotImplementedError

    async def trending_tv(self, page: int = 1) -> MediaPage:
        """List the week's trending TV shows, one page at a time."""
        raise NotImplementedError

    async def popular_tv(self, page: int = 1) -> MediaPage:
        """List currently popular TV shows, one page at a time.

        No TV equivalent of ``upcoming_movies`` -- TMDB has no "upcoming" TV
        endpoint comparable to its movie release-date listing.
        """
        raise NotImplementedError

    async def season_episodes(self, tmdb_id: int, season_number: int) -> list[EpisodeInfo]:
        """Episodes of one TV season (episode number + air date).

        Raises on a TMDB outage/error (never returns a silently-empty list to mean
        "unreachable") -- the caller treats a raise as "target unknown this
        cycle" and retries later; it must never guess an empty target (ADR-0020).
        """
        raise NotImplementedError
