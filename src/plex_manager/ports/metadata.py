"""MetadataPort — the media-metadata interface (TMDB in the alpha).

Kept deliberately thin: the alpha only needs to search, and to resolve a movie or
TV show by tmdb id. The DTOs are the cross-boundary contract; the adapter maps
TMDB's JSON into them. All methods are async (``httpx.AsyncClient``).
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
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


class MovieMetadata(BaseModel):
    """Resolved movie details needed to drive a request/search."""

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    imdb_id: str | None = None
    title: str
    year: int | None = None
    overview: str | None = None
    poster_url: str | None = None
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
    season_count: int = 0
    is_anime: bool = False


@runtime_checkable
class MetadataPort(Protocol):
    """Search for media and resolve movie / TV details by tmdb id."""

    async def search(self, query: str, year: int | None = None) -> list[MediaSearchResult]:
        """Search by free text, optionally constrained to ``year``."""
        ...

    async def get_movie(self, tmdb_id: int) -> MovieMetadata | None:
        """Resolve a movie by tmdb id, or ``None`` if not found."""
        ...

    async def get_tv_show(self, tmdb_id: int) -> TvMetadata | None:
        """Resolve a TV show by tmdb id, or ``None`` if not found."""
        ...
