"""FileSystemPort — the local-filesystem interface for the import step.

Defined now, used in v1: the import pipeline (validate -> rename -> route) calls
these. Operations are synchronous (local disk). ``hardlink_or_copy`` hardlinks
when possible and falls back to a copy across devices.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = ["VIDEO_EXTENSIONS", "FileSystemPort"]

#: Lowercased file suffixes (with the leading dot) that count as a video file
#: when scanning a downloaded release for the main feature. Mirrors the common
#: container set Radarr/Sonarr treat as video; sample and extras files are
#: filtered by name, not by extension.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mkv",
        ".mp4",
        ".avi",
        ".m4v",
        ".mov",
        ".wmv",
        ".mpg",
        ".mpeg",
        ".ts",
        ".m2ts",
        ".webm",
        ".flv",
        ".vob",
        ".ogv",
        ".divx",
    }
)


@runtime_checkable
class FileSystemPort(Protocol):
    """Disk-space queries and move / hardlink-or-copy operations."""

    def available_bytes(self, path: Path) -> int:
        """Return free bytes on the filesystem containing ``path``."""
        raise NotImplementedError

    def move(self, src: Path, dst: Path) -> None:
        """Move ``src`` to ``dst`` (atomic rename when on the same device)."""

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:
        """Hardlink ``src`` to ``dst``, falling back to a copy across devices."""

    def largest_video_file(self, root: str) -> str | None:
        """Return the absolute path of the largest video file under ``root``.

        Sample files and extras folders (featurettes / extras / trailers) are
        skipped so the *main feature* is selected. Returns ``None`` when no
        eligible video is found. If ``root`` is itself a video file, it is
        returned.
        """
        raise NotImplementedError

    def list_video_files(self, root: str) -> list[tuple[str, int, str]]:
        """Return every eligible video file under ``root``, for TV imports.

        Each entry is ``(absolute_path, size_bytes, relative_path)``, where
        ``relative_path`` is folder-qualified relative to ``root`` (e.g.
        ``"Season 01/Show.S01E01.mkv"``) -- needed to parse the season/episode
        out of a season-pack's directory structure, not just the filename.
        Sample files and extras folders are skipped, mirroring
        :meth:`largest_video_file`. Returns an empty list when no eligible video is
        found. Unlike :meth:`largest_video_file`, ``root`` being itself a single
        video file is not a case this method handles -- a TV import always walks a
        directory (a season pack or a whole-show download).
        """
        raise NotImplementedError
