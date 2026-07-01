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
        self, tmdb_id: int, media_type: Literal["movie", "tv"], *, use_cache: bool = True
    ) -> bool:
        """Return whether the item is already present in the library.

        ``use_cache=False`` forces a fresh read of the server, bypassing any
        cached-presence fast path. The request-dedup path passes it so a title just
        REMOVED from the library is seen as absent immediately, instead of a stale
        "present" answer held for the cache TTL.
        """
        raise NotImplementedError

    async def trigger_scan(self, path: str) -> None:
        """Ask the media server to scan ``path`` (partial-scan when supported)."""

    async def list_sections(self) -> list[LibrarySection]:
        """Return the configured library sections."""
        raise NotImplementedError
