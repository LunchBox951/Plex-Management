"""Discovery — free-text media search delegated to the metadata port (TMDB)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from plex_manager.ports.metadata import MediaSearchResult, MetadataPort

__all__ = ["search"]


async def search(
    tmdb: MetadataPort,
    query: str,
    year: int | None = None,
) -> list[MediaSearchResult]:
    """Return discovery results for ``query`` (optionally constrained to ``year``).

    A thin pass-through: the adapter owns the TMDB mapping; this service exists so
    the router depends on a service, not directly on the port, keeping a single
    seam the tests fake.
    """
    return await tmdb.search(query, year)
