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
from collections.abc import Iterable, Iterator
from pathlib import Path

from plex_manager.ports.filesystem import VIDEO_EXTENSIONS

__all__ = ["LocalFileSystem", "LocalFileSystemError"]

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


class LocalFileSystemError(RuntimeError):
    """Raised when :meth:`LocalFileSystem.delete` is asked to remove a path that
    does not resolve within any of the instance's configured library roots.

    A surfaced, honest refusal (ADR-0012's disk-pressure eviction): the message
    names the offending path only (never a root's real filesystem layout beyond
    what the caller already supplied), and — critically — is RAISED rather than
    swallowed even though the path might not exist. Letting a misconfigured or
    mismatched breadcrumb silently no-op would defeat the whole point of the
    guard, which is to make it structurally impossible for eviction to delete
    anything outside a configured library root.
    """


def _publish_temp_no_overwrite(tmp_path: str, dst: Path) -> None:
    """Publish a complete temp copy under a per-destination lock."""
    lock_path = dst.parent / f".{dst.name}.publish.lock"
    lock_fd = os.open(os.fspath(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        if dst.exists():
            raise FileExistsError(os.fspath(dst))
        os.replace(tmp_path, os.fspath(dst))
    finally:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            os.unlink(lock_path)


def _is_within(root_real: str, candidate_real: str) -> bool:
    """True if ``candidate_real`` is ``root_real`` or sits under it (both realpaths)."""
    return candidate_real == root_real or candidate_real.startswith(root_real + os.sep)


def _iter_video_files(root: str) -> Iterator[tuple[str, int, str]]:
    """Walk directory ``root``, yielding every eligible video file: ``(abs, size, rel)``.

    Shared by :meth:`LocalFileSystem.largest_video_file` (directory case) and
    :meth:`LocalFileSystem.list_video_files` -- the symlink/mount containment
    checks and the extras/sample pruning are identical for both callers. ``abs``
    is the realpath-resolved file (the actual bytes an import copies); ``rel`` is
    the LITERAL (unresolved) path relative to ``root``, preserving the download's
    own directory names (e.g. ``"Season 01/Show.S01E01.mkv"``) for token parsing.
    Yields nothing when ``root`` itself is a symlink escaping its own parent
    directory, or when ``root`` does not exist / is not a directory.
    """
    root_path = Path(root)
    # Containment anchor: a symlink (or nested mount) inside the download tree
    # must never let a yielded file resolve OUTSIDE it, or the importer would
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
        return
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
            literal_path = Path(dirpath) / filename
            candidate = os.path.realpath(literal_path)
            if not _is_within(root_real, candidate):
                # Symlink (or mount) escaping the download tree — skip honestly.
                continue
            try:
                size = os.path.getsize(candidate)
            except OSError:
                # A broken symlink or vanished file: skip it honestly rather
                # than letting it abort the whole scan.
                continue
            rel = os.path.relpath(literal_path, root)
            yield candidate, size, rel


class LocalFileSystem:
    """Disk-space queries and move / hardlink-or-copy operations on local disk."""

    def __init__(self, library_roots: Iterable[str] = ()) -> None:
        """``library_roots`` bounds :meth:`delete` to ONLY ever remove content
        inside one of these directories -- e.g. the configured ``movies_root``/
        ``tv_root`` (ADR-0012's disk-pressure eviction, the method's sole
        caller). Every other method on this adapter is root-agnostic (the import
        pipeline resolves its own absolute destinations and passes them
        directly), so this defaults to empty and every existing caller
        (``LocalFileSystem()``) is unaffected. With no roots configured, ``delete``
        refuses every path -- an unconfigured guard fails closed, never open.
        Blank entries are dropped and each root is resolved to its realpath once,
        up front, so a later symlinked root is compared consistently with the
        resolved candidate path in :meth:`delete`.
        """
        self._library_roots: tuple[str, ...] = tuple(
            os.path.realpath(root) for root in library_roots if root
        )

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

        best_path: str | None = None
        best_size = -1
        for candidate, size, _rel in _iter_video_files(root):
            if size > best_size:
                best_size = size
                best_path = candidate
        return best_path

    def list_video_files(self, root: str) -> list[tuple[str, int, str]]:
        """Return every eligible video file under ``root``, for TV imports.

        Each entry is ``(absolute_path, size_bytes, relative_path)``, where
        ``relative_path`` is folder-qualified relative to ``root`` (e.g.
        ``"Season 01/Show.S01E01.mkv"``) -- needed to parse the season/episode out
        of a season-pack's directory structure, not just the filename. Sample
        files and extras folders are skipped, mirroring
        :meth:`largest_video_file`. Returns an empty list when no eligible video is
        found. Unlike :meth:`largest_video_file`, ``root`` being itself a single
        video file is not handled here -- a TV import always walks a directory.
        """
        return list(_iter_video_files(root))

    def delete(self, path: str) -> None:
        """Delete ``path`` (a file, a symlink, or a whole directory tree) from local disk.

        ``path`` is resolved to its realpath (dereferencing every symlink in the
        chain, mirroring :func:`_iter_video_files`'s containment check) and that
        RESOLVED target MUST sit within one of this instance's ``library_roots``
        (constructor arg) -- an unconfigured or non-covering root is a refusal,
        always, RAISED as :class:`LocalFileSystemError` rather than silently
        skipped: eviction must never be able to reach outside a configured
        library root, and a caller passing a wrong path is a bug worth surfacing
        loudly even if that wrong path happens not to exist.

        Containment is checked against the RESOLVED path, but the actual REMOVAL
        never dereferences a symlink: when ``path`` ITSELF is a symlink (e.g. a
        breadcrumb that turned out to be a link rather than the real placed
        file), only that link entry is unlinked -- never ``shutil.rmtree``/
        ``os.remove`` on whatever it points at, even though that target already
        passed the containment check above. The target may be OTHER library
        content (a different title/season) that some other request still
        references directly; eviction owns the breadcrumb it was given, never
        transitively whatever that breadcrumb happens to point to. A REAL file
        or directory (what every import actually places) is entirely unaffected
        by this: it is not a symlink, so it still falls through to the ordinary
        file-or-tree removal below, exactly as before.

        ONLY once containment passes is existence checked (on ``path`` itself,
        via ``lexists`` -- a dangling symlink still "exists" as a link entry
        even though its target does not): a path that does not exist there at
        all is a no-op, not an error, so a retried eviction (a previous partial
        success, or a breadcrumb pointing at something already removed
        out-of-band) sees a clean, idempotent success.
        """
        real = os.path.realpath(path) if path else ""
        if not real or not any(_is_within(root, real) for root in self._library_roots):
            raise LocalFileSystemError(
                f"refusing to delete {path!r}: outside every configured library root"
            )
        if not os.path.lexists(path):
            return  # already gone -- idempotent no-op, not an error
        if os.path.islink(path):
            # Remove ONLY the link entry -- never follow it into its target,
            # and never shutil.rmtree a symlinked directory's contents.
            os.remove(path)
            return
        if os.path.isdir(real):
            shutil.rmtree(real)
        else:
            os.remove(real)

    def reclaimable_bytes(self, path: str) -> int:
        """Return how many bytes deleting ``path`` would ACTUALLY reclaim, hardlink-aware.

        A file whose link count (``st_nlink``) is greater than 1 has at least one
        OTHER directory entry pointing at the same inode -- e.g. the download
        client's own seed copy, when :meth:`hardlink_or_copy` linked rather than
        copied at import time (the common same-filesystem case) and the import
        finalizes WITHOUT removing that seed source. Deleting only THIS path in
        that case frees NOTHING -- the inode's bytes stay allocated via the other
        link -- so it reports ``0``, never the file's full size, which is what
        keeps the eviction sweep's freed-bytes accounting truthful (ADR-0012). A
        genuinely single-linked file reports its real size. A directory (a TV
        season) is walked, summing only the files whose OWN link count is
        ``<= 1`` -- a season can mix hardlinked and not-yet-shared files. A
        missing path, or any per-file stat error while walking, contributes
        ``0`` (best-effort, mirroring :func:`~plex_manager.services.
        eviction_service._size_bytes`'s honest "unknown" fallback) rather than
        aborting the whole computation or raising.

        A SYMLINK entry -- whether ``path`` itself or a file found while walking
        a directory -- ALWAYS contributes ``0``, matching :meth:`delete`'s own
        contract: ``delete`` unlinks only the link entry and never dereferences
        it, so nothing about the target's bytes is actually freed. ``os.stat``
        (unlike ``os.lstat``) follows a symlink, so checking ``os.path.isfile``/
        ``os.stat`` on a symlink would otherwise report the TARGET's size --
        inflating a pressure sweep's ``freed_bytes`` for content that was never
        touched. The symlink check happens BEFORE the ``isfile`` check (both
        follow symlinks identically) so a symlinked file is caught here rather
        than falling through to the size-reporting branch below.
        """
        try:
            if os.path.islink(path):
                return 0
            if os.path.isfile(path):
                stat = os.stat(path)
                return stat.st_size if stat.st_nlink <= 1 else 0
            if not os.path.isdir(path):
                return 0
            total = 0
            for dirpath, _dirnames, filenames in os.walk(path):
                for filename in filenames:
                    full = os.path.join(dirpath, filename)
                    if os.path.islink(full):
                        continue
                    with contextlib.suppress(OSError):
                        stat = os.stat(full)
                        if stat.st_nlink <= 1:
                            total += stat.st_size
            return total
        except OSError:
            return 0
