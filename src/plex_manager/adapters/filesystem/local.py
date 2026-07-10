"""LocalFileSystem — the :class:`FileSystemPort` implementation for local disk.

Unlike the Plex stub, this is a *real, safe* implementation used by the import
pipeline and fully unit-testable against ``tmp_path``. Operations are synchronous
(local disk) per the port contract.

``hardlink_or_copy`` prefers a hardlink (instant, zero extra space) and falls
back to a content copy when the destination is on a different device — the
classic seedbox/library cross-mount case.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import secrets
import shutil
import stat
import tempfile
import time
from collections.abc import Generator, Iterable, Iterator
from pathlib import Path
from typing import Literal, NamedTuple

from plex_manager.ports.filesystem import VIDEO_EXTENSIONS, FilePlacementIdentity

__all__ = [
    "LocalFileSystem",
    "LocalFileSystemError",
    "clear_stale_publish_locks",
    "rename_exchange",
    "rename_no_replace",
]

# os.link failures that genuinely warrant a content-copy fallback (cross-device,
# hardlink-refusing / unsupported filesystem). Any OTHER errno (notably EEXIST —
# the destination already exists) must NOT be masked as cross-device, or a copy
# could overwrite a file another import just placed.
_COPY_FALLBACK_ERRNOS: frozenset[int] = frozenset(
    {
        errno.EXDEV,
        errno.EPERM,
        errno.EMLINK,
        errno.EOPNOTSUPP,
        errno.EACCES,
        errno.ENOSYS,
    }
)

_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_LIBC = ctypes.CDLL(None, use_errno=True)


def rename_no_replace(
    src: str | bytes,
    dst: str | bytes,
    *,
    src_dir_fd: int | None = None,
    dst_dir_fd: int | None = None,
) -> None:
    """Atomically rename ``src`` without ever replacing ``dst`` (Linux)."""
    renameat2 = getattr(_LIBC, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
    src_bytes = os.fsencode(src)
    dst_bytes = os.fsencode(dst)
    result = renameat2(
        _AT_FDCWD if src_dir_fd is None else src_dir_fd,
        ctypes.c_char_p(src_bytes),
        _AT_FDCWD if dst_dir_fd is None else dst_dir_fd,
        ctypes.c_char_p(dst_bytes),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), os.fsdecode(dst_bytes))
    raise OSError(error, os.strerror(error), os.fsdecode(src_bytes))


def rename_exchange(
    left: str | bytes,
    right: str | bytes,
    *,
    left_dir_fd: int | None = None,
    right_dir_fd: int | None = None,
) -> None:
    """Atomically exchange two existing directory entries (Linux)."""
    renameat2 = getattr(_LIBC, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOTSUP, "atomic exchange rename is unavailable")
    left_bytes = os.fsencode(left)
    right_bytes = os.fsencode(right)
    result = renameat2(
        _AT_FDCWD if left_dir_fd is None else left_dir_fd,
        ctypes.c_char_p(left_bytes),
        _AT_FDCWD if right_dir_fd is None else right_dir_fd,
        ctypes.c_char_p(right_bytes),
        _RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error), os.fsdecode(left_bytes))


def _placement_identity(observed: os.stat_result) -> FilePlacementIdentity:
    return FilePlacementIdentity(
        device=observed.st_dev,
        inode=observed.st_ino,
        size=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
        ctime_ns=observed.st_ctime_ns,
        mode=observed.st_mode,
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


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# How long an EMPTY / unparseable publish lock must sit untouched before it is
# presumed poisoned (a crash between creating the lock and writing its pid) rather
# than a concurrent creator still mid-write. Small enough that recovery is prompt,
# large enough to never race a healthy publisher that has the fd open.
_EMPTY_LOCK_STALE_SECONDS = 60.0


def _lock_is_expired(lock_path: Path) -> bool:
    """Whether an empty/unparseable lock is old enough (by mtime) to reclaim."""
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return False
    return age > _EMPTY_LOCK_STALE_SECONDS


def _lock_is_stale(lock_path: Path) -> bool:
    """Whether a publish lock can be reclaimed.

    A parseable pid is authoritative: the lock is stale iff that process is gone.
    An empty or unparseable lock is the poisoning hazard -- ``_publish_lock``
    creates the lock file and writes its pid in two separate steps, so a crash in
    between leaves a zero-byte lock ``int('')`` can never parse. Rather than block
    the destination FOREVER (a terminal-only dead end -- violates north-star #1),
    reclaim such a lock once it is older than a short threshold; a younger empty
    lock is presumed to be a concurrent creator mid-write and is left untouched.
    """
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not raw:
        return _lock_is_expired(lock_path)
    try:
        pid = int(raw)
    except ValueError:
        return _lock_is_expired(lock_path)
    return not _pid_is_running(pid)


def clear_stale_publish_locks(
    directory: Path,
) -> Literal["cleared", "pending", "protected"]:
    """Remove only provably stale LocalFileSystem publish locks from a directory.

    ``cleared`` means every entry was a regular stale ``*.publish.lock`` and was
    removed (an already-empty directory also qualifies). ``pending`` means every
    entry is a regular lock but at least one is live/young and may become stale.
    Media, temp files, symlinks, unreadable entries, or races are ``protected``.
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return "protected"
    locks: list[tuple[Path, int, int]] = []
    has_pending = False
    for entry in entries:
        if not entry.name.startswith(".") or not entry.name.endswith(".publish.lock"):
            return "protected"
        try:
            observed = entry.lstat()
        except OSError:
            return "protected"
        if not stat.S_ISREG(observed.st_mode):
            return "protected"
        if not _lock_is_stale(entry):
            has_pending = True
            continue
        locks.append((entry, observed.st_dev, observed.st_ino))
    if has_pending:
        return "pending"
    for lock, expected_device, expected_inode in locks:
        try:
            current = lock.lstat()
            if (
                current.st_dev != expected_device
                or current.st_ino != expected_inode
                or not _lock_is_stale(lock)
            ):
                return "protected"
            lock.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            return "protected"
    return "cleared"


@contextlib.contextmanager
def _publish_lock(dst: Path):
    lock_path = dst.parent / f".{dst.name}.publish.lock"
    while True:
        try:
            lock_fd = os.open(os.fspath(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # lexists, not exists: a DANGLING symlink at dst must still refuse (GHSA-8fj8)
            # -- exists() follows the link and reads a dangling one as absent, which
            # would let a stale/planted symlink fall through as if dst were free.
            if os.path.lexists(os.fspath(dst)):
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
    """Publish a complete temp copy under a per-destination lock.

    The hardlink is the preferred publish (an atomic exclusive create — it fails
    ``EEXIST`` on its own, catching even a non-cooperating writer). On a
    filesystem that refuses hardlinks outright (SMB / FAT — ``EPERM`` /
    ``EOPNOTSUPP``, the same refusal that routed the caller here in the first
    place) the temp file is RENAMED into place instead: it already holds the
    fully verified bytes and already lives in ``dst.parent``, so the rename is a
    same-directory atomic move that costs no second content copy — previously
    this fell back to re-copying the temp's bytes into the final path, needing
    ~2x the title's size transiently and failing with a spurious ENOSPC on a
    barely-fitting disk. ``RENAME_NOREPLACE`` preserves the exclusive-create
    guarantee even against a writer that does not honor the cooperative lock.
    """
    with _publish_lock(dst):
        # lexists, not exists: on a hardlink-refusing filesystem the copy fallback
        # below is a rename publish, which could replace a dangling symlink's
        # entry (exists() reads a dangling link as absent) -- GHSA-8fj8. This is
        # the critical backstop, immediately before the link/rename attempt, under
        # the lock every publisher in this module takes before touching dst.
        if os.path.lexists(os.fspath(dst)):
            raise FileExistsError(os.fspath(dst))
        try:
            os.link(tmp_path, os.fspath(dst))
        except OSError as exc:
            if exc.errno not in _COPY_FALLBACK_ERRNOS:
                raise
            # The rename consumes the temp — nothing left to unlink. Linux's
            # RENAME_NOREPLACE closes the gap against writers that do not honor
            # our cooperative publish lock.
            rename_no_replace(tmp_path, os.fspath(dst))
            return
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _publish_link_no_overwrite(src: Path, dst: Path) -> None:
    """Publish ``src`` at ``dst`` via an exclusive hardlink under the destination lock."""
    with _publish_lock(dst):
        os.link(os.fspath(src), os.fspath(dst))


class _AnchoredDestination(NamedTuple):
    path: Path
    root_fd: int
    parent_fd: int
    name: str
    root_abs: str
    parent_parts: tuple[str, ...]


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _same_regular_file_after_rename(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare every regular-file field that rename(2) itself leaves stable."""
    return (
        _same_inode(left, right)
        and stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_mode == right.st_mode
    )


def _anchor_is_current(anchor: _AnchoredDestination) -> bool:
    """Whether held root/parent descriptors still occupy their configured path."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    opened: list[int] = []
    try:
        filesystem_prefix = os.path.realpath(os.sep)
        if (
            anchor.root_abs == filesystem_prefix
            or not anchor.root_abs.startswith(filesystem_prefix)
            or os.path.realpath(anchor.root_abs) != anchor.root_abs
        ):
            return False
        current_fd = os.open(
            anchor.root_abs,
            os.O_RDONLY | directory | nofollow | cloexec,
        )
        opened.append(current_fd)
        if not _same_inode(os.fstat(current_fd), os.fstat(anchor.root_fd)):
            return False
        if os.path.realpath(f"/proc/self/fd/{current_fd}") != anchor.root_abs:
            return False
        for component in anchor.parent_parts:
            current_fd = os.open(
                component,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=current_fd,
            )
            opened.append(current_fd)
        return _same_inode(os.fstat(current_fd), os.fstat(anchor.parent_fd))
    except (OSError, ValueError):
        return False
    finally:
        for fd in reversed(opened):
            with contextlib.suppress(OSError):
                os.close(fd)


def _quarantine_unlink_placement(
    parent_fd: int,
    name: str,
    expected: FilePlacementIdentity,
) -> bool:
    """Remove only the published inode, leaving a crash-visible placeholder.

    The mode-0700 quarantine is an authority boundary against other UIDs. A
    process already running as this service UID is trusted; Linux has no atomic
    compare-inode-and-unlink primitive that could defend against that peer.
    """
    quarantine_dir = f".plex-manager-rollback-{secrets.token_hex(16)}"
    held_fd: int | None = None
    quarantine_fd: int | None = None
    captured_fd: int | None = None
    placeholder_fd: int | None = None
    exchanged = False
    captured_removed = False
    try:
        held_fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        before = os.fstat(held_fd)
        if _placement_identity(before) != expected:
            return False
        os.mkdir(quarantine_dir, mode=0o700, dir_fd=parent_fd)
        quarantine_fd = os.open(
            quarantine_dir,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        os.fchmod(quarantine_fd, 0o700)
        placeholder_fd = os.open(
            "slot",
            os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=quarantine_fd,
        )
        placeholder_before = os.fstat(placeholder_fd)
        rename_exchange(
            name,
            "slot",
            left_dir_fd=parent_fd,
            right_dir_fd=quarantine_fd,
        )
        exchanged = True
        captured_fd = os.open(
            "slot",
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=quarantine_fd,
        )
        observed = os.fstat(captured_fd)
        if (
            not _same_regular_file_after_rename(before, observed)
            or _placement_identity(os.fstat(captured_fd)) != _placement_identity(observed)
            or _placement_identity(os.stat("slot", dir_fd=quarantine_fd, follow_symlinks=False))
            != _placement_identity(observed)
        ):
            rename_exchange(
                name,
                "slot",
                left_dir_fd=parent_fd,
                right_dir_fd=quarantine_fd,
            )
            exchanged = False
            # The successful restore puts our private placeholder back in the
            # mode-0700 quarantine. Remove it so the temporary directory does
            # not leak on an identity-mismatch/race-loser path.
            os.unlink("slot", dir_fd=quarantine_fd)
            return False
        os.unlink("slot", dir_fd=quarantine_fd)
        captured_removed = True
        rename_no_replace(
            name,
            "placeholder",
            src_dir_fd=parent_fd,
            dst_dir_fd=quarantine_fd,
        )
        placeholder_after = os.stat(
            "placeholder",
            dir_fd=quarantine_fd,
            follow_symlinks=False,
        )
        if not _same_regular_file_after_rename(placeholder_before, placeholder_after):
            rename_no_replace(
                "placeholder",
                name,
                src_dir_fd=quarantine_fd,
                dst_dir_fd=parent_fd,
            )
            return False
        os.unlink("placeholder", dir_fd=quarantine_fd)
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        return False
    except (OSError, ValueError):
        return False
    finally:
        if exchanged and not captured_removed and quarantine_fd is not None:
            try:
                rename_exchange(
                    name,
                    "slot",
                    left_dir_fd=parent_fd,
                    right_dir_fd=quarantine_fd,
                )
                os.unlink("slot", dir_fd=quarantine_fd)
            except OSError:
                pass
        for fd in (placeholder_fd, captured_fd, quarantine_fd, held_fd):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        with contextlib.suppress(OSError):
            os.rmdir(quarantine_dir, dir_fd=parent_fd)


@contextlib.contextmanager
def _anchored_destination_path(root: Path, dst: Path) -> Generator[_AnchoredDestination]:
    """Yield ``dst`` through a no-follow parent dirfd rooted at ``root``."""
    root_abs = os.path.normcase(os.path.abspath(os.path.normpath(root)))
    dst_abs = os.path.normcase(os.path.abspath(os.path.normpath(dst)))
    prefix = root_abs.rstrip(os.sep) + os.sep
    if dst_abs == root_abs or not dst_abs.startswith(prefix):
        raise OSError(errno.EPERM, "destination is outside configured library root")
    relative_parts = Path(os.path.relpath(dst_abs, root_abs)).parts
    if not relative_parts or any(part in {"", ".", ".."} for part in relative_parts):
        raise OSError(errno.EPERM, "invalid destination beneath configured library root")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    filesystem_prefix = os.path.realpath(os.sep)
    if (
        not nofollow
        or not directory
        or not os.path.isdir("/proc/self/fd")
        or root_abs == filesystem_prefix
        or not root_abs.startswith(filesystem_prefix)
        or os.path.realpath(root_abs) != root_abs
    ):
        raise OSError(errno.ENOTSUP, "root-anchored publication is unavailable")

    opened: list[int] = []
    try:
        current_fd = os.open(root_abs, os.O_RDONLY | directory | nofollow | cloexec)
        opened.append(current_fd)
        for component in relative_parts[:-1]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow | cloexec,
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, dir_fd=current_fd)
                next_fd = os.open(
                    component,
                    os.O_RDONLY | directory | nofollow | cloexec,
                    dir_fd=current_fd,
                )
            opened.append(next_fd)
            current_fd = next_fd
        anchor = _AnchoredDestination(
            path=Path(f"/proc/self/fd/{current_fd}") / relative_parts[-1],
            root_fd=opened[0],
            parent_fd=current_fd,
            name=relative_parts[-1],
            root_abs=root_abs,
            parent_parts=tuple(relative_parts[:-1]),
        )
        if not _anchor_is_current(anchor):
            raise OSError(errno.ESTALE, "library destination authority changed")
        yield anchor
    finally:
        for fd in reversed(opened):
            with contextlib.suppress(OSError):
                os.close(fd)


def _remove_acl_xattrs_from_fd(file_fd: int) -> None:
    """Strip inherited ACL authority from one descriptor-held inode."""
    try:
        names = os.listxattr(file_fd)
    except OSError as exc:
        if exc.errno not in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
        return
    for name in names:
        if "acl" in name.lower():
            os.removexattr(file_fd, name)


def _copy_exact_fd(source_fd: int, destination_fd: int, expected_size: int) -> None:
    """Copy exactly one captured source size without reopening either pathname."""
    offset = 0
    while offset < expected_size:
        chunk = os.pread(source_fd, min(1024 * 1024, expected_size - offset), offset)
        if not chunk:
            raise OSError(
                errno.EIO,
                f"snapshot source became short: expected {expected_size} bytes, read {offset}",
            )
        written = 0
        while written < len(chunk):
            count = os.pwrite(destination_fd, chunk[written:], offset + written)
            if count <= 0:
                raise OSError(errno.EIO, "snapshot destination made no write progress")
            written += count
        offset += len(chunk)
    if os.pread(source_fd, 1, expected_size):
        raise OSError(errno.EIO, "snapshot source grew during copy")


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
        """Move ``src`` to ``dst`` without replacing an existing destination file."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            _publish_link_no_overwrite(src, dst)
        except OSError as exc:
            if exc.errno not in _COPY_FALLBACK_ERRNOS:
                raise
            self._copy_no_overwrite(src, dst)
        src.unlink()

    def hardlink_or_copy_from_fd_beneath(
        self,
        source_fd: int,
        source_name: str,
        dst: Path,
        *,
        destination_root: Path,
    ) -> FilePlacementIdentity:
        """Snapshot an open source into a distinct inode beneath the root.

        Torrent content remains writable by the download client after import.
        A hardlink would permanently share that mutable inode with the public
        library, so this security-boundary API always creates an independent
        copy. The generic trusted-path helper retains its hardlink optimization.
        """
        del source_name
        with _anchored_destination_path(destination_root, dst) as anchor:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise OSError(errno.EPERM, "import source is not a regular file")
            free = self.available_bytes(anchor.path.parent)
            if free < source_stat.st_size:
                raise OSError(
                    "insufficient space to snapshot import source: need "
                    f"{source_stat.st_size} bytes, {free} available on destination filesystem"
                ) from None
            identity = self._snapshot_fd_no_overwrite(source_fd, source_stat.st_size, anchor)
            try:
                observed = os.stat(
                    anchor.name,
                    dir_fd=anchor.parent_fd,
                    follow_symlinks=False,
                )
            except OSError:
                observed = None
            if (
                observed is None
                or _placement_identity(observed) != identity
                or not _anchor_is_current(anchor)
            ):
                _quarantine_unlink_placement(anchor.parent_fd, anchor.name, identity)
                raise OSError(errno.ESTALE, "library destination authority changed")
            return identity

    def _snapshot_fd_no_overwrite(
        self,
        source_fd: int,
        source_size: int,
        anchor: _AnchoredDestination,
    ) -> FilePlacementIdentity:
        """Create and atomically publish a sanitized snapshot through held fds.

        The temporary inode lives in a private mode-0700 directory opened from
        the already-anchored destination parent.  No copy, metadata operation,
        or publication reopens a writable-parent pathname, so replacing a temp
        entry cannot redirect writes outside the library root.
        """
        quarantine_name = f".plex-manager-snapshot-{secrets.token_hex(16)}"
        quarantine_fd: int | None = None
        snapshot_fd: int | None = None
        quarantine_identity: tuple[int, int] | None = None
        published_identity: FilePlacementIdentity | None = None
        snapshot_name = "payload"
        try:
            os.mkdir(quarantine_name, mode=0o700, dir_fd=anchor.parent_fd)
            quarantine_before = os.stat(
                quarantine_name,
                dir_fd=anchor.parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(quarantine_before.st_mode)
                or quarantine_before.st_uid != os.geteuid()
            ):
                raise OSError(errno.EPERM, "snapshot quarantine authority changed")
            quarantine_identity = (
                quarantine_before.st_dev,
                quarantine_before.st_ino,
            )
            quarantine_fd = os.open(
                quarantine_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=anchor.parent_fd,
            )
            quarantine_opened = os.fstat(quarantine_fd)
            if (
                not stat.S_ISDIR(quarantine_opened.st_mode)
                or quarantine_opened.st_uid != os.geteuid()
                or not _same_inode(quarantine_before, quarantine_opened)
            ):
                raise OSError(errno.EPERM, "snapshot quarantine authority changed")
            # A parent default ACL may be inherited even when mkdir's mode masks
            # it. Remove it before the directory becomes the trust boundary.
            _remove_acl_xattrs_from_fd(quarantine_fd)
            os.fchmod(quarantine_fd, 0o700)
            quarantine_hardened = os.fstat(quarantine_fd)
            quarantine_current = os.stat(
                quarantine_name,
                dir_fd=anchor.parent_fd,
                follow_symlinks=False,
            )
            if stat.S_IMODE(quarantine_hardened.st_mode) != 0o700 or not _same_inode(
                quarantine_hardened, quarantine_current
            ):
                raise OSError(errno.ESTALE, "snapshot quarantine authority changed")

            snapshot_fd = os.open(
                snapshot_name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=quarantine_fd,
            )
            _copy_exact_fd(source_fd, snapshot_fd, source_size)
            _remove_acl_xattrs_from_fd(snapshot_fd)
            os.fchmod(snapshot_fd, 0o644)
            snapshot_before = os.fstat(snapshot_fd)
            snapshot_current = os.stat(
                snapshot_name,
                dir_fd=quarantine_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(snapshot_before.st_mode)
                or snapshot_before.st_uid != os.geteuid()
                or snapshot_before.st_nlink != 1
                or snapshot_before.st_size != source_size
                or stat.S_IMODE(snapshot_before.st_mode) != 0o644
                or _placement_identity(snapshot_before) != _placement_identity(snapshot_current)
            ):
                raise OSError(errno.ESTALE, "snapshot temporary identity changed")

            # The temp and final entry share the anchored destination filesystem;
            # renameat2(RENAME_NOREPLACE) publishes the exact held inode without
            # exposing an overwrite window or requiring a pathname reopen.
            rename_no_replace(
                snapshot_name,
                anchor.name,
                src_dir_fd=quarantine_fd,
                dst_dir_fd=anchor.parent_fd,
            )
            published_identity = _placement_identity(os.fstat(snapshot_fd))
            return published_identity
        except Exception:
            if published_identity is not None:
                _quarantine_unlink_placement(
                    anchor.parent_fd,
                    anchor.name,
                    published_identity,
                )
            raise
        finally:
            if quarantine_fd is not None:
                with contextlib.suppress(OSError):
                    os.unlink(snapshot_name, dir_fd=quarantine_fd)
            for fd in (snapshot_fd, quarantine_fd):
                if fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(fd)
            if quarantine_identity is not None:
                try:
                    current = os.stat(
                        quarantine_name,
                        dir_fd=anchor.parent_fd,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) == quarantine_identity:
                        os.rmdir(quarantine_name, dir_fd=anchor.parent_fd)
                except OSError:
                    pass

    def hardlink_or_copy(self, src: Path, dst: Path) -> FilePlacementIdentity:
        """Hardlink ``src`` to ``dst``, falling back to a copy across devices.

        A cross-device link raises ``OSError`` (``EXDEV``); some filesystems also
        reject hardlinks with ``EPERM``. Either way we fall back to a metadata-
        preserving copy rather than failing the import.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Hold a descriptor to the exact inode being published. A pathname
            # replacement after link(2), but before this method returns, cannot
            # change the identity token returned to the importer.
            with src.open("rb") as published:
                _publish_link_no_overwrite(src, dst)
                return _placement_identity(os.fstat(published.fileno()))
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
            return self._copy_no_overwrite(src, dst)

    def _copy_no_overwrite(
        self,
        src: Path,
        dst: Path,
    ) -> FilePlacementIdentity:
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
            # Keep the completed temp inode open through publication. Whether the
            # primitive links then unlinks the temp or renames it into place, fstat
            # remains bound to the object actually published rather than whatever
            # may subsequently appear at ``dst``.
            with open(tmp_path, "rb") as published:
                _publish_temp_no_overwrite(tmp_path, dst)
                placement_identity = _placement_identity(os.fstat(published.fileno()))
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
        return placement_identity

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

    def resolve_guarded(self, path: str) -> str | None:
        """Resolve ``path`` to its realpath, returning it ONLY if BOTH ``path``'s
        own entry location AND that resolved target sit within a configured
        library root -- else ``None`` (a refusal).

        The SINGLE resolve-and-check that both :meth:`delete` and
        :meth:`delete_guard_refuses` share, so ``path``'s symlink chain is resolved
        EXACTLY ONCE. That is the whole point: :meth:`delete` removes the very path
        this returned and never re-resolves ``path`` afterwards, so a symlinked path
        COMPONENT repointed AFTER the containment check can no longer redirect the
        removal outside every root (the guard/delete TOCTOU) -- there is no second
        resolution left to disagree with the checked one.

        TWO containment checks, both required (issue #141) -- a single realpath
        check is not enough:

        1. The ENTRY's own location must sit within a root. Computed by
           resolving every ANCESTOR directory component (dereferencing a
           symlinked ancestor dir, matching this method's existing containment
           semantics for intermediate components) while leaving the FINAL
           component un-dereferenced -- i.e. where ``path`` itself, as a
           directory entry, actually lives. Without this, an outside-root
           symlink whose TARGET resolves inside a root (``/tmp/link.mkv ->
           /library/movie.mkv``) would pass containment on the target alone,
           and :meth:`delete` -- which unlinks the SYMLINK ENTRY, never its
           target, when ``path`` is a symlink -- would then unlink
           ``/tmp/link.mkv``, an entry outside every configured root.
        2. The fully-resolved target (:func:`os.path.realpath`, dereferencing
           EVERY symlink in the chain including the final component) must also
           sit within a root -- the pre-existing check, which still refuses an
           INSIDE-root symlink whose target escapes (``/library/link.mkv ->
           /etc/passwd``): its entry location passes check 1, but its resolved
           target fails check 2.

        Fails CLOSED on either check -- an empty ``path``, or no configured
        roots, returns ``None``.
        """
        if not path:
            return None
        entry_dir = os.path.dirname(path) or "."
        entry_location = os.path.join(os.path.realpath(entry_dir), os.path.basename(path))
        if not any(_is_within(root, entry_location) for root in self._library_roots):
            return None
        real = os.path.realpath(path)
        if not any(_is_within(root, real) for root in self._library_roots):
            return None
        return real

    def delete_guard_refuses(self, path: str) -> bool:
        """Whether :meth:`delete` would REFUSE ``path`` as outside every configured
        library root -- the pure containment predicate, no delete attempted.

        A thin boolean view over :meth:`resolve_guarded` (the single shared
        resolve-and-check ``delete`` itself now uses), so a read-only caller (the
        retention-telemetry would-evict SIMULATION) can pre-filter the same paths a
        real sweep's ``delete`` would refuse WITHOUT deleting anything and WITHOUT
        reimplementing the check -- so its would-evict count/bytes can never count
        space a real sweep would refuse to free, and can never drift from
        ``delete``'s own guard. Mirrors ``delete``: ``path`` is resolved to its
        realpath (dereferencing a symlinked COMPONENT that would escape the root,
        not just a symlink final entry), and it fails CLOSED -- an empty path, or no
        configured roots, refuses.
        """
        return self.resolve_guarded(path) is None

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

        ``path`` is resolved EXACTLY ONCE (via :meth:`resolve_guarded`) and it is
        that single resolved target -- the one whose containment was checked -- that
        is removed below; ``path`` is never re-resolved afterwards. This closes a
        guard/delete TOCTOU: were the containment check and the removal to resolve
        ``path`` independently, a symlinked path COMPONENT repointed between them
        could send the removal outside every configured root even though the check
        passed. There is no such second resolution here.

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
        # Resolve-and-check ONCE: the returned ``real`` is the path whose containment
        # was just affirmed, and it -- never a fresh re-resolution of ``path`` -- is
        # what the real-file/tree removal below acts on (closes the guard/delete
        # TOCTOU; see the docstring).
        real = self.resolve_guarded(path)
        if real is None:
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
