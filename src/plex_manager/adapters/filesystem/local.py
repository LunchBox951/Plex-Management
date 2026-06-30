"""LocalFileSystem — the :class:`FileSystemPort` implementation for local disk.

Unlike the Plex stub, this is a *real, safe* implementation: shipping it is
harmless because nothing imports it into a running pipeline yet (the import step
is deferred), and it is fully unit-testable against ``tmp_path``. Operations are
synchronous (local disk) per the port contract.

``hardlink_or_copy`` prefers a hardlink (instant, zero extra space) and falls
back to a content copy when the destination is on a different device — the
classic seedbox/library cross-mount case.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from plex_manager.ports.filesystem import VIDEO_EXTENSIONS

__all__ = ["LocalFileSystem"]

#: Lowercased directory names whose contents are bonus material, not the main
#: feature — skipped entirely when picking the largest video.
_EXTRAS_DIR_NAMES: frozenset[str] = frozenset(
    {"featurettes", "extras", "trailers", "behind the scenes", "deleted scenes"}
)


class LocalFileSystem:
    """Disk-space queries and move / hardlink-or-copy operations on local disk."""

    def available_bytes(self, path: Path) -> int:
        """Return free bytes on the filesystem containing ``path``.

        ``path`` need not exist yet (a planned destination); the nearest existing
        ancestor is queried, so callers can size up a download before its target
        directory is created.
        """
        probe = path
        while not probe.exists():
            parent = probe.parent
            if parent == probe:  # reached the filesystem root
                break
            probe = parent
        return shutil.disk_usage(probe).free

    def move(self, src: Path, dst: Path) -> None:
        """Move ``src`` to ``dst`` (atomic rename when on the same device)."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(os.fspath(src), os.fspath(dst))

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:
        """Hardlink ``src`` to ``dst``, falling back to a copy across devices.

        A cross-device link raises ``OSError`` (``EXDEV``); some filesystems also
        reject hardlinks with ``EPERM``. Either way we fall back to a metadata-
        preserving copy rather than failing the import.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(os.fspath(src), os.fspath(dst))
        except OSError:
            # Cross-device (or hardlink-refusing) filesystem: copy instead. A
            # copy actually consumes space, so preflight that the destination
            # filesystem can hold the source before writing a partial file.
            src_size = src.stat().st_size
            free = self.available_bytes(dst.parent)
            if free < src_size:
                raise OSError(
                    f"insufficient space to copy {src.name}: need {src_size} bytes, "
                    f"{free} available on destination filesystem"
                ) from None
            shutil.copy2(os.fspath(src), os.fspath(dst))
            # Verify the copy is complete; a short write means a truncated /
            # corrupt import, so roll back the partial file and surface it.
            copied_size = dst.stat().st_size
            if copied_size != src_size:
                os.unlink(os.fspath(dst))
                raise OSError(
                    f"copy of {src.name} is incomplete: expected {src_size} bytes, "
                    f"wrote {copied_size}; partial destination removed"
                ) from None

    def largest_video_file(self, root: str) -> str | None:
        """Return the absolute path of the largest video file under ``root``.

        Walks ``root`` keeping files whose suffix is in :data:`VIDEO_EXTENSIONS`,
        skipping sample files and extras folders (featurettes / extras /
        trailers). Returns the path with the greatest size, or ``None`` when no
        eligible video exists. If ``root`` is itself a video file, it is
        returned.
        """
        root_path = Path(root)
        if root_path.is_file():
            if root_path.suffix.lower() in VIDEO_EXTENSIONS:
                return os.fspath(root_path.resolve())
            return None

        best_path: str | None = None
        best_size = -1
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune extras / sample directories in place so os.walk skips them.
            dirnames[:] = [
                name
                for name in dirnames
                if name.lower() not in _EXTRAS_DIR_NAMES and "sample" not in name.lower()
            ]
            for filename in filenames:
                if "sample" in filename.lower():
                    continue
                if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
                    continue
                candidate = Path(dirpath) / filename
                try:
                    size = candidate.stat().st_size
                except OSError:
                    # A broken symlink or vanished file: skip it honestly rather
                    # than letting it abort the whole scan.
                    continue
                if size > best_size:
                    best_size = size
                    best_path = os.fspath(candidate.resolve())
        return best_path
