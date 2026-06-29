"""DownloadClientPort — the torrent-client interface (qBittorrent in the alpha).

The port returns a client-neutral :class:`DownloadStatus` DTO; the adapter maps
raw qBittorrent state strings into it (the domain never sees a raw client
string). ``add`` returns the lowercased info-hash. All methods are async — the
adapter uses ``httpx.AsyncClient``.

This DTO lives in the port (not the adapter) so it is the stable cross-boundary
contract the reconciler can depend on without importing an adapter.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

__all__ = ["DownloadClientPort", "DownloadStatus"]


class DownloadStatus(BaseModel):
    """A point-in-time snapshot of one torrent in the download client.

    ``raw_state`` is the client's own state string (e.g. ``downloading``,
    ``stoppedUP``); the reconciler maps it to a domain ``DownloadState``. The
    ``ratio_limit`` / ``*_limit_minutes`` defaults of ``-2`` mean "use the
    client global" (qBittorrent convention); ``-1`` means unlimited.
    """

    model_config = ConfigDict(frozen=True)

    info_hash: str
    name: str
    raw_state: str
    progress: float = 0.0
    ratio: float = 0.0
    save_path: str = ""
    content_path: str | None = None
    eta_seconds: int | None = None
    ratio_limit: float = -2.0
    seeding_time_limit_minutes: int = -2
    inactive_seeding_time_limit_minutes: int = -2
    last_activity_unix: int = 0


@runtime_checkable
class DownloadClientPort(Protocol):
    """Add, monitor, and control torrents in the download client."""

    async def add(self, magnet_or_url: str, save_path: str, category: str) -> str:
        """Add a torrent; return its lowercased info-hash.

        A 409 (already present) resolves to the existing hash, never an error.
        """
        ...

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        """Return the status for ``info_hash``, or ``None`` if absent."""
        ...

    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        """Return statuses for all torrents, optionally filtered by category."""
        ...

    async def pause(self, info_hash: str) -> None:
        """Pause the torrent identified by ``info_hash``."""
        ...

    async def resume(self, info_hash: str) -> None:
        """Resume the torrent identified by ``info_hash``."""
        ...

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        """Remove the torrent, deleting its files when ``delete_files`` is set."""
        ...

    async def set_category(self, info_hash: str, category: str) -> None:
        """Set the torrent's category (used to mark imported items)."""
        ...

    async def get_save_path(self, info_hash: str) -> str | None:
        """Return the torrent's current save path, re-read from the client."""
        ...
