"""Repository ports — async persistence interfaces for the domain.

The domain depends on these Protocols, never on SQLAlchemy. The records here are
the cross-boundary read-models the engine / reconciler / web layer consume; the
P2 SQLAlchemy implementations map ORM rows to and from them. Status fields are
plain ``str`` to avoid coupling to the (separately owned) state-machine enum.

Method sets are intentionally minimal — sufficient for the alpha pipeline
(create request -> grab -> reconcile -> blocklist) and nothing more.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = [
    "BlocklistRecord",
    "BlocklistRepository",
    "DownloadRecord",
    "DownloadRepository",
    "RequestRecord",
    "RequestRepository",
]


class RequestRecord(BaseModel):
    """A media request as the domain reads it."""

    model_config = ConfigDict(frozen=True)

    id: int
    tmdb_id: int
    media_type: str
    title: str
    status: str
    year: int | None = None
    is_anime: bool = False
    user_id: int | None = None


class DownloadRecord(BaseModel):
    """A tracked download as the domain reads it."""

    model_config = ConfigDict(frozen=True)

    id: int
    torrent_hash: str
    status: str
    media_request_id: int | None = None
    magnet_link: str | None = None
    progress: float = 0.0
    seed_ratio: float = 0.0
    tmdb_id: int | None = None
    year: int | None = None
    season: int | None = None
    failed_reason: str | None = None
    first_seen_at: datetime | None = None
    download_path: str | None = None


class BlocklistRecord(BaseModel):
    """A blocklist entry as the domain reads it."""

    model_config = ConfigDict(frozen=True)

    id: int
    source_title: str
    reason: str
    tmdb_id: int | None = None
    torrent_hash: str | None = None
    indexer: str | None = None
    protocol: str | None = None
    media_type: str | None = None
    added_at: datetime | None = None


@runtime_checkable
class RequestRepository(Protocol):
    """Persistence for media requests."""

    async def get(self, request_id: int) -> RequestRecord | None:
        """Return the request by id, or ``None``."""

    async def list_by_status(self, status: str | None = None) -> list[RequestRecord]:
        """List requests, optionally filtered by ``status``."""
        raise NotImplementedError

    async def find_active(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        """Return an existing non-terminal request for this media, for dedup."""

    async def create(
        self,
        *,
        tmdb_id: int,
        media_type: str,
        title: str,
        status: str,
        year: int | None = None,
        is_anime: bool = False,
        user_id: int | None = None,
    ) -> RequestRecord:
        """Insert a new request and return the persisted record."""
        raise NotImplementedError

    async def set_status(self, request_id: int, status: str) -> None:
        """Update a request's status."""


@runtime_checkable
class DownloadRepository(Protocol):
    """Persistence for tracked downloads."""

    async def get_by_hash(self, torrent_hash: str) -> DownloadRecord | None:
        """Return the download for ``torrent_hash``, or ``None``."""

    async def list_active(self) -> list[DownloadRecord]:
        """List downloads in a non-terminal state (for the reconcile loop)."""
        raise NotImplementedError

    async def create(
        self,
        *,
        torrent_hash: str,
        status: str,
        media_request_id: int | None = None,
        magnet_link: str | None = None,
        tmdb_id: int | None = None,
        year: int | None = None,
        season: int | None = None,
    ) -> DownloadRecord:
        """Insert a new download and return the persisted record."""
        raise NotImplementedError

    async def update_status(
        self,
        download_id: int,
        status: str,
        *,
        progress: float | None = None,
        seed_ratio: float | None = None,
        failed_reason: str | None = None,
        download_path: str | None = None,
        first_seen_at: datetime | None = None,
    ) -> None:
        """Update a download's status and optional progress fields.

        ``first_seen_at`` stamps the missing-grace anchor: the caller passes
        ``now`` when persisting a ``StateTransition`` whose ``set_first_seen_at``
        flag is set, so the reconciler's grace window can actually start.
        """


@runtime_checkable
class BlocklistRepository(Protocol):
    """Persistence for the failed / reported-bad release blocklist."""

    async def is_blocklisted(
        self,
        tmdb_id: int | None,
        torrent_hash: str | None,
        source_title: str,
        indexer: str | None,
    ) -> bool:
        """Two-tier identity check: hash first, then title/indexer fallback."""
        raise NotImplementedError

    async def list_for_media(self, tmdb_id: int | None = None) -> list[BlocklistRecord]:
        """List blocklist entries, optionally scoped to one media item."""
        raise NotImplementedError

    async def create(
        self,
        *,
        source_title: str,
        reason: str,
        tmdb_id: int | None = None,
        torrent_hash: str | None = None,
        indexer: str | None = None,
        protocol: str | None = None,
        media_type: str | None = None,
    ) -> BlocklistRecord:
        """Insert a new blocklist entry and return the persisted record."""
        raise NotImplementedError

    async def delete(self, blocklist_id: int) -> None:
        """Remove a blocklist entry (operator un-blocklist)."""
