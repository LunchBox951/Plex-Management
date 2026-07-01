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

import contextlib
import errno
import os
import shutil
import tempfile
from pathlib import Path

from plex_manager.ports.filesystem import VIDEO_EXTENSIONS

__all__ = ["LocalFileSystem"]

# os.link failures that genuinely warrant a content-copy fallback (cross-device,
# hardlink-refusing / unsupported filesystem). Any OTHER errno (notably EEXIST —
# the destination already exists) must NOT be masked as cross-device, or a copy
# could overwrite a file another import just placed.
_COPY_FALLBACK_ERRNOS: frozenset[int] = frozenset(
    {errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EOPNOTSUPP, errno.EACCES}
)

#: Lowercased directory names whose contents are bonus material, not the main
#: feature — skipped entirely when picking the largest video.
_EXTRAS_DIR_NAMES: frozenset[str] = frozenset(
    {"featurettes", "extras", "trailers", "behind the scenes", "deleted scenes"}
)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_stale(lock_path: Path) -> bool:
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return not _pid_is_running(pid)


@contextlib.contextmanager
def _publish_lock(dst: Path):
    lock_path = dst.parent / f".{dst.name}.publish.lock"
    while True:
        try:
            lock_fd = os.open(os.fspath(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if dst.exists():
                raise FileExistsError(os.fspath(dst)) from None
            if _lock_is_stale(lock_path):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(lock_path)
                continue
            raise
        break
    try:
        os.write(lock_fd, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            os.unlink(lock_path)


def _publish_temp_no_overwrite(tmp_path: str, dst: Path) -> None:
    """Publish a complete temp copy under a per-destination lock."""
    with _publish_lock(dst):
        if dst.exists():
            raise FileExistsError(os.fspath(dst))
        os.replace(tmp_path, os.fspath(dst))


def _is_within(root_real: str, candidate_real: str) -> bool:
    """True if ``candidate_real`` is ``root_real`` or sits under it (both realpaths)."""
    return candidate_real == root_real or candidate_real.startswith(root_real + os.sep)


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
        """Move ``src`` to ``dst`` without replacing an existing destination file."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(os.fspath(src), os.fspath(dst))
        except OSError as exc:
            if exc.errno not in _COPY_FALLBACK_ERRNOS:
                raise
            self._copy_no_overwrite(src, dst)
        src.unlink()

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:
        """Hardlink ``src`` to ``dst``, falling back to a copy across devices.

        A cross-device link raises ``OSError`` (``EXDEV``); some filesystems also
        reject hardlinks with ``EPERM``. Either way we fall back to a metadata-
        preserving copy rather than failing the import.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(os.fspath(src), os.fspath(dst))
        except OSError as exc:
            # Only a genuine cross-device / hardlink-unsupported failure warrants a
            # copy. EEXIST (the destination already exists — e.g. a concurrent import
            # won the race) or any other errno is surfaced, never silently masked as
            # cross-device into an overwriting copy.
            if exc.errno not in _COPY_FALLBACK_ERRNOS:
                raise
            # Cross-device (or hardlink-refusing) filesystem: copy instead. A
            # copy actually consumes space, so preflight that the destination
            # filesystem can hold the source before writing a partial file.
            self._copy_no_overwrite(src, dst)

    def _copy_no_overwrite(self, src: Path, dst: Path) -> None:
        src_size = src.stat().st_size
        free = self.available_bytes(dst.parent)
        if free < src_size:
            raise OSError(
                f"insufficient space to copy {src.name}: need {src_size} bytes, "
                f"{free} available on destination filesystem"
            ) from None
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{dst.name}.",
                suffix=".tmp",
                dir=dst.parent,
                delete=False,
            ) as tmp:
                tmp_path = tmp.name
            shutil.copy2(os.fspath(src), tmp_path)
            # Verify the copy is complete before exposing it at the final path.
            copied_size = Path(tmp_path).stat().st_size
            if copied_size != src_size:
                raise OSError(
                    f"copy of {src.name} is incomplete: expected {src_size} bytes, "
                    f"wrote {copied_size}; partial destination removed"
                )
            _publish_temp_no_overwrite(tmp_path, dst)
            tmp_path = None
        except OSError:
            # The copy target is a temp file in dst.parent, never the final path,
            # so a process crash cannot leave a partial library file that blocks
            # every retry. Clean the temp best-effort and re-raise the original
            # error, unmasked (north-star #3: honesty).
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            raise
        else:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

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
            # Same containment as the walk below: a single-file content root that is
            # a symlink escaping its own directory must not be followed and copied
            # into the public library.
            resolved = os.path.realpath(root_path)
            if root_path.suffix.lower() in VIDEO_EXTENSIONS and _is_within(
                os.path.realpath(root_path.parent), resolved
            ):
                return resolved
            return None

        # Containment anchor: a symlink (or nested mount) inside the download tree
        # must never let the chosen source resolve OUTSIDE it, or the importer would
        # copy an arbitrary file (e.g. /etc/passwd) into the public library.
        root_real = os.path.realpath(root)
        # Reject a content root that is ITSELF a symlink escaping its own parent
        # directory (e.g. /downloads/release -> /etc): root_real would become the
        # symlink target and every file beneath it would spuriously satisfy the
        # per-file containment check below, copying arbitrary files into the public
        # library. A legitimately symlinked *parent* (e.g. /downloads -> /mnt/store)
        # is unaffected, because realpath(parent) still contains root_real. At the
        # filesystem root the parent check is vacuous (everything is under it), so
        # skip it there rather than spuriously rejecting a top-level download dir.
        parent_real = os.path.realpath(root_path.parent)
        if parent_real != os.sep and not _is_within(parent_real, root_real):
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
                candidate = os.path.realpath(Path(dirpath) / filename)
                if not _is_within(root_real, candidate):
                    # Symlink (or mount) escaping the download tree — skip honestly.
                    continue
                try:
                    size = os.path.getsize(candidate)
                except OSError:
                    # A broken symlink or vanished file: skip it honestly rather
                    # than letting it abort the whole scan.
                    continue
                if size > best_size:
                    best_size = size
                    best_path = candidate
        return best_path
