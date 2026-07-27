"""Local filesystem adapter package."""

from __future__ import annotations

from plex_manager.adapters.filesystem.local import (
    LocalFileSystem,
    LocalFileSystemError,
    PartialDeleteError,
)

__all__ = ["LocalFileSystem", "LocalFileSystemError", "PartialDeleteError"]
