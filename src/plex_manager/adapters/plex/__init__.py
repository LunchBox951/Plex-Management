"""Plex library adapter package (deferred to v1 — stub in the alpha)."""

from __future__ import annotations

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibrary, PlexLibraryError
from plex_manager.adapters.plex.watchlist import (
    PlexWatchlist,
    PlexWatchlistAuthError,
    PlexWatchlistError,
)

__all__ = [
    "PlexAuthError",
    "PlexLibrary",
    "PlexLibraryError",
    "PlexWatchlist",
    "PlexWatchlistAuthError",
    "PlexWatchlistError",
]
