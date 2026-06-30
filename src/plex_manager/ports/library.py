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


@runtime_checkable
class LibraryPort(Protocol):
    """Query availability, trigger scans, and list sections on the media server."""

    async def is_available(self, tmdb_id: int, media_type: Literal["movie", "tv"]) -> bool:
        """Return whether the item is already present in the library."""
        raise NotImplementedError

    async def trigger_scan(self, path: str) -> None:
        """Ask the media server to scan ``path`` (partial-scan when supported)."""

    async def list_sections(self) -> list[LibrarySection]:
        """Return the configured library sections."""
        raise NotImplementedError
