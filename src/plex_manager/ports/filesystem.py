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

    def delete(self, path: str) -> None:
        """Delete ``path`` (a file or a whole directory tree) from local disk.

        The disk-pressure eviction sweep's ONLY write operation (ADR-0012): it is
        the sole caller, always with a title's/season's stored ``library_path``
        breadcrumb, never a reconstructed-from-naming guess. Implementations MUST
        refuse (raise, never silently ignore) a ``path`` that does not resolve
        within one of the app's configured library roots -- eviction must never
        be able to delete an arbitrary filesystem path, mirroring the symlink-
        escape containment ``LocalFileSystem`` already applies to imports. A
        ``path`` that does not exist is a no-op, not an error: an eviction retried
        after a previous partial success (or a breadcrumb pointing at something
        already removed out-of-band) must not fail honestly-idempotent cleanup.
        """
        raise NotImplementedError

    def delete_guard_refuses(self, path: str) -> bool:
        """Whether :meth:`delete` would REFUSE ``path`` -- the pure containment
        predicate, WITHOUT attempting the delete.

        :meth:`delete` MUST refuse (raise) a path that does not resolve within a
        configured library root; this exposes that exact same refusal decision as
        a read-only query so a would-evict SIMULATION (the retention-telemetry
        sweep) can pre-filter the very paths a real sweep's delete would refuse
        -- never counting, as freeable, bytes a real delete would decline to touch
        -- and can never drift from ``delete``'s own guard. Implementations that
        fence ``delete`` to configured roots MUST resolve symlinked components the
        same way ``delete`` does and fail closed (no roots / empty path -> refuse);
        an implementation whose ``delete`` is unfenced returns ``False``.
        """
        raise NotImplementedError

    def reclaimable_bytes(self, path: str) -> int:
        """Return how many bytes deleting ``path`` would ACTUALLY reclaim right now.

        Hardlink-aware (ADR-0012's eviction freed-bytes accounting): for a
        same-filesystem import, ``hardlink_or_copy`` prefers a hardlink over a
        copy, and the import finalizes WITHOUT removing the download-client's
        seed source -- so the placed library file often has ``st_nlink > 1``.
        Deleting only the library copy in that case frees NOTHING (the inode's
        bytes stay allocated via the other link), so such a file MUST report
        ``0``, never its full size. A file with no other link (``st_nlink <= 1``)
        reports its real size. For a directory (a TV season), this walks it and
        sums only the files whose OWN link count is ``<= 1`` -- a season can mix
        hardlinked and not-yet-shared files. A missing ``path``, or any
        underlying stat error, contributes ``0`` (best-effort honesty, mirroring
        the eviction service's own "unknown size" fallback) rather than raising.
        Read-only: never deletes anything itself, and (unlike :meth:`delete`) is
        not fenced to a configured library root -- callers only ever pass an
        already-trusted, stored breadcrumb.
        """
        raise NotImplementedError
