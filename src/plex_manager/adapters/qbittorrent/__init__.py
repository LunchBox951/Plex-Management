"""qBittorrent adapter package — the live :class:`DownloadClientPort` impl."""

from __future__ import annotations

from plex_manager.adapters.qbittorrent.adapter import (
    QbittorrentAuthError,
    QbittorrentClient,
)

__all__ = ["QbittorrentAuthError", "QbittorrentClient"]
