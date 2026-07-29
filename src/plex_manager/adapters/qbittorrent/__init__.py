"""qBittorrent adapter package — the live :class:`DownloadClientPort` impl."""

from __future__ import annotations

from plex_manager.adapters.qbittorrent.adapter import (
    QbittorrentAddAmbiguousError,
    QbittorrentAddRejectedError,
    QbittorrentAuthError,
    QbittorrentClient,
    QbittorrentError,
    QbittorrentSourceError,
)

__all__ = [
    "QbittorrentAddAmbiguousError",
    "QbittorrentAddRejectedError",
    "QbittorrentAuthError",
    "QbittorrentClient",
    "QbittorrentError",
    "QbittorrentSourceError",
]
