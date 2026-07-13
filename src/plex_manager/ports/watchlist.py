"""Per-account Plex cloud watchlist boundary."""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = ["WatchlistEntry", "WatchlistPort"]


class WatchlistEntry(BaseModel):
    """Canonical request identity resolved at the Plex adapter boundary."""

    model_config = ConfigDict(frozen=True)

    tmdb_id: int
    media_type: Literal["movie", "tv"]


@runtime_checkable
class WatchlistPort(Protocol):
    async def list_entries(self) -> tuple[WatchlistEntry, ...]:
        """Return the complete current account watchlist or raise."""
        raise NotImplementedError
