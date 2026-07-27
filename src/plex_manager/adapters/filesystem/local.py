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
import stat
import time
from collections.abc import Generator, Iterable, Iterator
from pathlib import Path
from typing import IO, NoReturn

from plex_manager.domain.plex_video import is_plex_disc_structure_path, plex_video_extension

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
    """Raised when a containment guard refuses a path: :meth:`LocalFileSystem.delete`
    asked to remove something that does not resolve within any of the instance's
    configured library roots, or :meth:`LocalFileSystem.move` /
    :meth:`LocalFileSystem.hardlink_or_copy` asked to publish through a symlinked
    (or non-directory) ancestor beneath the selected library root.

    A surfaced, honest refusal (ADR-0012's disk-pressure eviction): the message
    names the offending path only (never a root's real filesystem layout beyond
    what the caller already supplied), and — critically — is RAISED rather than
    swallowed even though the path might not exist. Letting a misconfigured or
    mismatched breadcrumb silently no-op would defeat the whole point of the
    guard, which is to make it structurally impossible for eviction to delete
    anything outside a configured library root. The publication guard is raised
    for the mirror-image reason: a silent fall back to pathname publication would
    place media outside every configured root while the caller records an in-root
    breadcrumb, which the delete-side guard then correctly refuses to clean up
    (GHSA-r5vh) — an uncorrectable state, violating north-star #1.
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


def _lock_is_expired(dir_fd: int, lock_name: str) -> bool:
    """Whether an empty/unparseable lock is old enough (by mtime) to reclaim."""
    try:
        mtime = os.stat(lock_name, dir_fd=dir_fd, follow_symlinks=False).st_mtime
    except OSError:
        return False
    return time.time() - mtime > _EMPTY_LOCK_STALE_SECONDS


def _lock_is_stale(dir_fd: int, lock_name: str) -> bool:
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
        lock_fd = os.open(lock_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        raw = os.read(lock_fd, 64).decode("utf-8", errors="replace").strip()
    except OSError:
        return False
    finally:
        os.close(lock_fd)
    if not raw:
        return _lock_is_expired(dir_fd, lock_name)
    try:
        pid = int(raw)
    except ValueError:
        return _lock_is_expired(dir_fd, lock_name)
    return not _pid_is_running(pid)


def _entry_exists(dir_fd: int, name: str) -> bool:
    """``os.path.lexists`` semantics, resolved relative to ``dir_fd``.

    ``follow_symlinks=False``, not a plain stat: a DANGLING symlink at the
    destination must read as PRESENT (GHSA-8fj8) -- a following stat reads it as
    absent, which would let a stale/planted link fall through as if the
    destination were free.
    """
    try:
        os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _publish_lock(dir_fd: int, name: str, display: str) -> Generator[None, None, None]:
    lock_name = f".{name}.publish.lock"
    while True:
        try:
            lock_fd = os.open(
                lock_name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            if _entry_exists(dir_fd, name):
                raise FileExistsError(display) from None
            if _lock_is_stale(dir_fd, lock_name):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(lock_name, dir_fd=dir_fd)
                continue
            raise
        break
    try:
        os.write(lock_fd, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            os.unlink(lock_name, dir_fd=dir_fd)


def _publish_temp_no_overwrite(dir_fd: int, tmp_name: str, name: str, display: str) -> None:
    """Publish a complete temp copy under a per-destination lock.

    Every operation is resolved relative to ``dir_fd`` -- the verified destination
    directory descriptor :func:`_anchored_publication` handed the caller -- so a
    concurrent ancestor swap cannot redirect the publish (GHSA-r5vh).

    The hardlink is the preferred publish (an atomic exclusive create — it fails
    ``EEXIST`` on its own, catching even a non-cooperating writer). On a
    filesystem that refuses hardlinks outright (SMB / FAT — ``EPERM`` /
    ``EOPNOTSUPP``, the same refusal that routed the caller here in the first
    place) the temp file is RENAMED into place instead: it already holds the
    fully verified bytes and already lives in that same directory, so the rename
    is a same-directory atomic move that costs no second content copy —
    previously this fell back to re-copying the temp's bytes into the final path,
    needing ~2x the title's size transiently and failing with a spurious ENOSPC
    on a barely-fitting disk. The exclusive-create guarantee against a CONCURRENT
    PUBLISHER is preserved by the per-destination ``_publish_lock`` plus the
    ``_entry_exists`` check made under it — every publisher in this module takes
    that same lock before touching the destination entry.
    """
    with _publish_lock(dir_fd, name, display):
        # lexists, not exists: on a hardlink-refusing filesystem the copy fallback
        # below is os.rename, which WOULD silently replace a dangling symlink's
        # entry (exists() reads a dangling link as absent) -- GHSA-8fj8. This is
        # the critical backstop, immediately before the link/rename attempt, under
        # the lock every publisher in this module takes before touching dst.
        if _entry_exists(dir_fd, name):
            raise FileExistsError(display)
        try:
            os.link(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno not in _COPY_FALLBACK_ERRNOS:
                raise
            # The rename consumes the temp — nothing left to unlink.
            os.rename(tmp_name, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return
        with contextlib.suppress(OSError):
            os.unlink(tmp_name, dir_fd=dir_fd)


def _publish_link_no_overwrite(src: Path, dir_fd: int, name: str, display: str) -> None:
    """Publish ``src`` at ``name`` (relative to ``dir_fd``) via an exclusive hardlink."""
    with _publish_lock(dir_fd, name, display):
        os.link(os.fspath(src), name, dst_dir_fd=dir_fd)


def _copy_contents(src: Path, target: IO[bytes]) -> None:
    """Stream ``src``'s bytes into the already-open ``target``, preserving mode and
    timestamps -- the ``shutil.copy2`` equivalent for a destination that exists only
    as a descriptor inside a verified directory (it has no pathname a second lookup
    could re-resolve).
    """
    with open(src, "rb") as source:
        shutil.copyfileobj(source, target)
        source_stat = os.fstat(source.fileno())
    target.flush()
    os.fchmod(target.fileno(), stat.S_IMODE(source_stat.st_mode))
    os.utime(target.fileno(), ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))


#: Flags for every directory descriptor the publication walk holds. ``O_PATH``
#: (Linux) where available, exactly as the delete-side walk does: it needs only
#: SEARCH (execute) permission on the destination directories -- what pathname
#: publication demanded of them -- where ``O_RDONLY`` would newly demand READ on
#: every one and spuriously ``EACCES`` on a search-only library tree. An ``O_PATH``
#: descriptor is valid as the ``dir_fd`` of the whole ``openat``/``mkdirat``/
#: ``linkat``/``renameat``/``unlinkat``/``fstatat`` family this module publishes
#: with, and still fails ``ENOTDIR`` on a swapped-in symlink under ``O_NOFOLLOW``.
_PUBLISH_DIR_FLAGS: int = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_CLOEXEC


#: Whether this platform can guarantee fd-anchored, no-follow publication. The
#: write-side counterpart of :func:`_delete_containment_supported`: without
#: ``O_NOFOLLOW``/``O_DIRECTORY`` and ``dir_fd``-relative ``open``/``mkdir``/
#: ``link``/``rename``/``unlink``/``stat``, a destination's ancestors can only be
#: traversed by pathname -- exactly the traversal GHSA-r5vh exploits -- so
#: publication refuses every path rather than degrade silently (north-star #3).
#: The deployment target is Linux/Docker, where all of these exist. Resolved at
#: import, against the interpreter's own os functions: ``os.supports_dir_fd``
#: holds those objects by identity, so a later reassignment of ``os.link`` (a test
#: double) must not be read as a platform that lost the capability.
_PUBLICATION_CONTAINMENT_SUPPORTED: bool = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and {os.open, os.mkdir, os.link, os.rename, os.unlink, os.stat} <= os.supports_dir_fd
)


def _open_or_create_child_dir(parent_fd: int, component: str, display: str) -> int:
    """Open (creating it if absent) ``component`` inside ``parent_fd``, no-follow.

    ``O_NOFOLLOW | O_DIRECTORY`` is what makes this a containment primitive: an
    existing symlink -- or any non-directory -- at ``component`` fails
    ``ELOOP``/``ENOTDIR`` in the kernel instead of being traversed, and is
    SURFACED as a refusal. The ``mkdir`` is allowed to lose to a concurrent import
    creating the same season directory (``EEXIST``); the open that follows is what
    decides whether what now sits there is trustworthy.
    """
    flags = _PUBLISH_DIR_FLAGS | os.O_NOFOLLOW
    try:
        return os.open(component, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _reraise_ancestor_failure(component, display, exc)
    with contextlib.suppress(FileExistsError):
        os.mkdir(component, dir_fd=parent_fd)
    try:
        return os.open(component, flags, dir_fd=parent_fd)
    except OSError as exc:
        _reraise_ancestor_failure(component, display, exc)


def _reraise_ancestor_failure(component: str, display: str, exc: OSError) -> NoReturn:
    """Re-raise a destination ancestor's open failure: a symlink / non-directory is a
    containment breach and becomes a :class:`LocalFileSystemError`; anything else (a
    permission problem, a vanished mount) is surfaced unchanged."""
    if exc.errno in (errno.ELOOP, errno.ENOTDIR):
        raise LocalFileSystemError(
            f"refusing to publish {display!r}: destination ancestor {component!r} is a "
            "symlink or non-directory (containment could not be guaranteed)"
        ) from exc
    raise exc


@contextlib.contextmanager
def _anchored_publication(root: Path, dst: Path) -> Generator[tuple[int, str], None, None]:
    """Yield ``(parent_fd, leaf_name)`` for publishing ``dst`` beneath ``root``.

    The enforcement layer for GHSA-r5vh. ``root`` -- the library root the caller
    SELECTED for this title (the anime root when ADR-0015 routed there, otherwise
    the normal one) -- is admin-configured and may legitimately be reached through
    symlinks (``/data -> /mnt/store``), so it is resolved by pathname exactly
    once, here, and opened as a directory descriptor. EVERY component below it is
    then opened relative to the PREVIOUS component's descriptor with
    ``O_NOFOLLOW | O_DIRECTORY``, creating what is missing with ``mkdir`` relative
    to that same descriptor. Nothing beneath the root is ever trusted by pathname,
    so a symlinked movie-title / show / season directory -- planted beforehand or
    swapped in mid-walk -- cannot redirect the publish outside the root: the
    kernel refuses the open, and the caller gets a raised
    :class:`LocalFileSystemError` rather than a silent traversal.

    The yielded descriptor is what the final link/copy/rename must be anchored to.
    A pathname re-check before publishing by name would reopen the very TOCTOU
    this closes -- the descriptor keeps pointing at the directory that was
    verified even if its name is later swapped underneath.

    ``dst`` must lie beneath ``root``; a lexically escaping destination (``..``)
    is refused before any descriptor is opened.
    """
    display = os.fspath(dst)
    if not _PUBLICATION_CONTAINMENT_SUPPORTED:
        raise LocalFileSystemError(
            f"refusing to publish {display!r}: this platform cannot guarantee "
            "fd-anchored, no-follow publication containment"
        )
    components = os.path.relpath(display, os.fspath(root)).split(os.sep)
    if os.pardir in components or components == [os.curdir]:
        raise LocalFileSystemError(
            f"refusing to publish {display!r}: outside the library root {os.fspath(root)!r}"
        )
    dir_fd = os.open(os.fspath(root), _PUBLISH_DIR_FLAGS)
    try:
        for component in components[:-1]:
            next_fd = _open_or_create_child_dir(dir_fd, component, display)
            os.close(dir_fd)
            dir_fd = next_fd
        yield dir_fd, components[-1]
    finally:
        os.close(dir_fd)


def _is_within(root_real: str, candidate_real: str) -> bool:
    """True if ``candidate_real`` is ``root_real`` or sits under it (both realpaths)."""
    return candidate_real == root_real or candidate_real.startswith(root_real + os.sep)


def _open_parent_nofollow(start_dir: str, components: list[str], original_path: str) -> int | None:
    """Open the delete leaf's PARENT directory via a no-follow ``openat`` walk,
    anchored at ``start_dir`` -- the process filesystem root (``os.sep``), the
    only anchor an unprivileged actor cannot rename or swap.

    This is the enforcement layer that closes the ancestor-symlink-swap TOCTOU a
    pathname re-check cannot. ``start_dir`` (``/``) is opened by pathname -- safe,
    because the filesystem root cannot be replaced with a symlink -- and then
    EVERY component leading down to the leaf's parent (``components[:-1]`` -- the
    last component is the leaf itself, left for the caller to inspect and remove)
    is opened relative to the PREVIOUS component's already-open file descriptor
    with ``O_NOFOLLOW | O_DIRECTORY``. That includes the directory CONTAINING the
    configured root and the root itself: nothing between ``/`` and the leaf is
    trusted by pathname, so a concurrent actor who renames ANY ancestor -- at any
    depth, including the root's own parent -- and replaces it with a symlink (or a
    non-directory) between the containment check and this walk cannot redirect it.
    The kernel refuses that open (``ELOOP``/``ENOTDIR``) rather than following it,
    so the swap is SURFACED as a refusal (north-star #3: honesty), never silently
    traversed. Contrast a second ``os.path.realpath``/``os.path.lexists`` call, or
    anchoring the walk at ``dirname(root_real)`` opened by pathname, either of
    which would re-resolve through a swapped ancestor and hand back a DIFFERENT
    real path than the one already checked.

    Ancestors are opened with ``O_PATH`` where the platform provides it (Linux):
    an ``O_PATH`` descriptor requires only SEARCH (execute) permission on the
    directory -- matching what plain pathname ``unlink`` demands of ancestors --
    where ``O_RDONLY`` would demand READ permission on every ancestor and
    spuriously ``EACCES`` on a locked-down, execute-only mount parent. The
    no-follow guarantee is preserved: ``O_PATH | O_NOFOLLOW | O_DIRECTORY`` on a
    swapped-in symlink fails ``ENOTDIR`` (the ``O_PATH | O_NOFOLLOW`` fd would
    refer to the link itself, which is not a directory) -- still a surfaced
    refusal, never a traversal -- and an ``O_PATH`` fd is valid as the ``dir_fd``
    of the ``openat``/``fstatat``/``unlinkat`` family this walk and its caller
    use. Without ``O_PATH`` the walk falls back to ``O_RDONLY`` (read permission
    on ancestors -- the pre-existing requirement on such platforms).

    Returns the parent directory's fd (the caller must ``os.close`` it), or
    ``None`` when an intermediate ancestor no longer exists at all -- an
    idempotent no-op, matching :meth:`LocalFileSystem.delete`'s existing
    "already gone" contract for a path that vanished out-of-band (e.g. a
    configured root whose mount disappeared -- every component below ``/`` is
    walked here, so an ENOENT anywhere along it, including at the root's own
    parent, is caught, not raised).
    """
    open_mode = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY
    dir_fd = os.open(start_dir, open_mode)
    try:
        for component in components[:-1]:
            try:
                next_fd = os.open(
                    component,
                    open_mode | os.O_NOFOLLOW,
                    dir_fd=dir_fd,
                )
            except FileNotFoundError:
                # An intermediate ancestor is already gone -- idempotent no-op
                # for the caller, but `dir_fd` is still OPEN right here: it is
                # not the BaseException handler below (a `return` is not an
                # exception) and there is no other cleanup on this path, so it
                # must be closed explicitly before returning or it leaks for
                # the life of the process -- on a long-running daemon retrying
                # this exact idempotent path repeatedly, that walks toward
                # EMFILE and takes down every other file operation.
                os.close(dir_fd)
                return None
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise LocalFileSystemError(
                        f"refusing to delete {original_path!r}: an ancestor changed to a "
                        "symlink or non-directory during deletion (containment could not "
                        "be guaranteed)"
                    ) from exc
                raise
            os.close(dir_fd)
            dir_fd = next_fd
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _delete_containment_supported() -> bool:
    """Whether this platform can guarantee fd-anchored, no-follow delete containment.

    :meth:`LocalFileSystem.delete` cannot safely remove anything without
    ``O_NOFOLLOW``, ``dir_fd``-relative ``os.unlink``/``os.rmdir``, and a
    ``shutil.rmtree`` that resists symlink attacks -- the primitives the
    ancestor-swap-resistant walk is built on. When any is missing, ``delete``
    refuses every path rather than fall back to the unsafe pathname re-check it
    exists to avoid. This predicate is SHARED with
    :meth:`LocalFileSystem.delete_guard_refuses` so the read-only refusal decision
    (purge, retention telemetry) matches what ``delete`` actually does on such a
    platform (north-star #3: honesty) -- a would-evict simulation must not report
    a breadcrumb as evictable and walk its bytes when the real delete would refuse
    it up front.
    """
    return (
        hasattr(os, "O_NOFOLLOW")
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and shutil.rmtree.avoids_symlink_attacks
    )


def _iter_video_files(root: str) -> Iterator[tuple[str, int, str]]:
    """Walk directory ``root``, yielding every eligible video file: ``(abs, size, rel)``.

    Shared by :meth:`LocalFileSystem.largest_video_file` (directory case) and
    :meth:`LocalFileSystem.list_video_files` -- the symlink/mount containment
    checks and the extras/sample/disc-structure pruning are identical for both
    callers. ``abs`` is the realpath-resolved file (the actual bytes an import
    copies); ``rel`` is the LITERAL (unresolved) path relative to ``root``,
    preserving the download's own directory names (e.g.
    ``"Season 01/Show.S01E01.mkv"``) for token parsing. Yields nothing when
    ``root`` itself is inside a ``BDMV``/``VIDEO_TS`` structure, is a symlink
    escaping its own parent directory, or does not exist / is not a directory.
    """
    root_path = Path(root)
    if is_plex_disc_structure_path(os.fspath(root_path)):
        # Catch both a content root named BDMV/VIDEO_TS and a client path rooted
        # at one of its descendants (e.g. BDMV/STREAM). Without this root guard,
        # pruning only ``dirnames`` below would be one level too late.
        return
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
        # Prune extras / sample / optical-disc directories in place so os.walk
        # never offers their component streams as standalone import candidates.
        dirnames[:] = [
            name
            for name in dirnames
            if name.casefold() not in _EXTRAS_DIR_NAMES
            and not is_plex_disc_structure_path(name)
            and "sample" not in name.casefold()
        ]
        for filename in filenames:
            if "sample" in filename.lower():
                continue
            if plex_video_extension(os.fspath(Path(dirpath) / filename)) is None:
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

    def move(self, src: Path, dst: Path, *, root: Path) -> None:
        """Move ``src`` to ``dst`` (beneath ``root``) without replacing an existing
        destination file."""
        self.hardlink_or_copy(src, dst, root=root)
        src.unlink()

    def hardlink_or_copy(self, src: Path, dst: Path, *, root: Path) -> None:
        """Hardlink ``src`` to ``dst``, falling back to a copy across devices.

        ``dst`` must lie beneath the library ``root`` the caller selected for this
        title, and every destination component below that root is created and
        opened no-follow relative to a held directory descriptor
        (:func:`_anchored_publication`) — a symlinked movie-title / show / season
        ancestor is refused, never followed outside the root (GHSA-r5vh).

        A cross-device link raises ``OSError`` (``EXDEV``); some filesystems also
        reject hardlinks with ``EPERM``. Either way we fall back to a metadata-
        preserving copy rather than failing the import.
        """
        with _anchored_publication(root, dst) as (parent_fd, name):
            try:
                _publish_link_no_overwrite(src, parent_fd, name, os.fspath(dst))
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
                self._copy_no_overwrite(src, dst, parent_fd, name)

    def _copy_no_overwrite(self, src: Path, dst: Path, parent_fd: int, name: str) -> None:
        src_size = src.stat().st_size
        free = self.available_bytes(dst.parent)
        if free < src_size:
            raise OSError(
                f"insufficient space to copy {src.name}: need {src_size} bytes, "
                f"{free} available on destination filesystem"
            ) from None
        # The temp is created relative to the SAME verified descriptor as the final
        # entry, so the bytes cannot land outside the root even if the destination
        # directory's name is swapped mid-copy; a random suffix keeps the exclusive
        # create from colliding with a concurrent import of another title.
        tmp_name = f".{name}.{os.urandom(8).hex()}.tmp"
        tmp_fd = os.open(
            tmp_name,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        published = False
        try:
            with os.fdopen(tmp_fd, "wb") as target:
                _copy_contents(src, target)
            # Verify the copy is complete before exposing it at the final path.
            copied_size = os.stat(tmp_name, dir_fd=parent_fd, follow_symlinks=False).st_size
            if copied_size != src_size:
                raise OSError(
                    f"copy of {src.name} is incomplete: expected {src_size} bytes, "
                    f"wrote {copied_size}; partial destination removed"
                )
            _publish_temp_no_overwrite(parent_fd, tmp_name, name, os.fspath(dst))
            published = True
        finally:
            # The copy target is a temp entry in the destination directory, never
            # the final name, so a process crash cannot leave a partial library file
            # that blocks every retry. Clean it best-effort and let the original
            # error propagate unmasked (north-star #3: honesty).
            if not published:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name, dir_fd=parent_fd)

    def largest_video_file(self, root: str) -> str | None:
        """Return the absolute path of the largest video file under ``root``.

        Walks ``root`` keeping files whose suffix is in
        :data:`~plex_manager.domain.plex_video.PLEX_VIDEO_EXTENSIONS`,
        skipping sample files, extras folders (featurettes / extras / trailers),
        and ``BDMV``/``VIDEO_TS`` optical-disc structures. Returns the path with
        the greatest size, or ``None`` when no eligible video exists. If ``root``
        is itself a video file, it is returned.
        """
        root_path = Path(root)
        if root_path.is_file():
            # Same containment as the walk below: a single-file content root that is
            # a symlink escaping its own directory must not be followed and copied
            # into the public library.
            resolved = os.path.realpath(root_path)
            if plex_video_extension(os.fspath(root_path)) is not None and _is_within(
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
        files, extras folders, and ``BDMV``/``VIDEO_TS`` optical-disc structures
        are skipped, mirroring :meth:`largest_video_file`. Returns an empty list
        when no eligible video is found. Unlike :meth:`largest_video_file`,
        ``root`` being itself a single video file is not handled here -- a TV
        import always walks a directory.
        """
        return list(_iter_video_files(root))

    def _guarded_resolution(self, path: str) -> tuple[str, str] | None:
        """The shared resolve-and-check behind :meth:`resolve_guarded`,
        :meth:`delete_guard_refuses`, and :meth:`delete` -- returning both
        ``path``'s own (unresolved-final-component) entry location and its
        fully-resolved target, each already containment-checked.

        Returns ``(entry_location, real)`` where ``entry_location`` is where
        ``path`` itself lives as a directory entry (its ancestors resolved, its
        final component left literal) and ``real`` is the fully-dereferenced
        target; or ``None`` (a refusal) if either containment check fails.
        :meth:`delete` anchors its no-follow fd walk at the filesystem root and
        descends to ``entry_location`` -- it does NOT need the matched root itself,
        which is why only these two values are returned. See :meth:`resolve_guarded`
        for the two-check rationale (issue #141).

        A ``path`` containing a ``.`` or ``..`` component, or ending in a
        separator (an empty final component), is REFUSED outright rather than
        normalized. Normalizing here and acting on the normalized location is
        NOT equivalent to what the supplied path names: ``realpath`` collapses
        ``Gone/..`` LEXICALLY when ``Gone`` does not exist (POSIX lookup of the
        original path would be ENOENT, yet the collapsed path names a live
        sibling -- or, for a ``..`` leaf, the parent directory itself), and a
        trailing separator on a symlink-to-file makes ``realpath(dirname)``
        resolve THROUGH the link so the walk would target the link's TARGET
        while the caller named the link (POSIX would refuse ``link/`` with
        ENOTDIR). Our own pipeline only ever stores normalized absolute paths,
        so such a breadcrumb is malformed input -- refused loudly (fails
        closed), never silently retargeted.
        """
        if not path:
            return None
        parts = path.split(os.sep)
        if parts[-1] in ("", os.curdir, os.pardir) or any(
            part in (os.curdir, os.pardir) for part in parts
        ):
            return None
        entry_dir = os.path.dirname(path) or "."
        entry_location = os.path.join(os.path.realpath(entry_dir), os.path.basename(path))
        if not any(_is_within(root, entry_location) for root in self._library_roots):
            return None
        real = os.path.realpath(path)
        if not any(_is_within(root, real) for root in self._library_roots):
            return None
        return entry_location, real

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

        Fails CLOSED on either check -- an empty ``path``, a non-normalized
        ``path`` (a ``.``/``..`` component or a trailing separator, which
        resolution could silently retarget -- see :meth:`_guarded_resolution`),
        or no configured roots, returns ``None``.
        """
        resolution = self._guarded_resolution(path)
        return None if resolution is None else resolution[1]

    def delete_guard_refuses(self, path: str) -> bool:
        """Whether :meth:`delete` would REFUSE ``path`` as outside every configured
        library root -- the pure containment predicate, no delete attempted.

        A boolean view over :meth:`resolve_guarded` (the single shared
        resolve-and-check ``delete`` itself now uses) PLUS the same platform
        capability gate ``delete`` applies, so a read-only caller (the
        retention-telemetry would-evict SIMULATION) can pre-filter the same paths a
        real sweep's ``delete`` would refuse WITHOUT deleting anything and WITHOUT
        reimplementing the check -- so its would-evict count/bytes can never count
        space a real sweep would refuse to free, and can never drift from
        ``delete``'s own guard. Mirrors ``delete``: on a platform that cannot
        guarantee fd-anchored, no-follow delete containment (:func:`
        _delete_containment_supported`) EVERY path is refused, exactly as
        ``delete`` refuses up front; otherwise ``path`` is resolved to its realpath
        (dereferencing a symlinked COMPONENT that would escape the root, not just a
        symlink final entry), and it fails CLOSED -- an empty path, or no
        configured roots, refuses.
        """
        if not _delete_containment_supported():
            return True
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
        loudly even if that wrong path happens not to exist. A NON-NORMALIZED
        ``path`` (a ``.``/``..`` component or a trailing separator) is the same
        loud refusal: resolution would silently retarget it onto a DIFFERENT
        entry than the one POSIX lookup of the supplied string names (see
        :meth:`_guarded_resolution`), and our pipeline only ever stores
        normalized absolute paths, so such a breadcrumb is malformed input.

        ``path`` is resolved EXACTLY ONCE (via :meth:`_guarded_resolution`), and
        the removal below never re-resolves ``path`` through a SECOND pathname
        lookup at all -- not even the ``os.path.lexists``/``islink``/``isdir``
        re-checks a naive "resolve once, then act on the string" fix would still
        perform. Those pathname syscalls re-traverse every ancestor component
        from the kernel's current view of the tree, so if a writable ancestor
        directory is renamed and replaced with a symlink BETWEEN the containment
        check and the removal, they happily re-resolve through the swapped
        ancestor and can delete a same-suffix target outside every configured
        root -- the guard/delete TOCTOU is not closed by checking a string once
        if the removal still walks the filesystem by name again afterwards.

        Instead, the removal is ANCHORED to file descriptors opened with
        ``O_NOFOLLOW`` from the process filesystem root (``os.sep`` -- the one
        anchor an unprivileged actor cannot rename or swap) down to the leaf's
        own parent directory (:func:`_open_parent_nofollow`), so EVERY ancestor
        -- including the directory containing the configured root and the root
        itself -- is verified to be a real directory rather than a swapped-in
        symlink. Anchoring instead at ``dirname(root_real)`` opened by pathname
        would still trust that one level: a rename of the root's own parent
        between the check and the walk would be followed. The leaf itself is
        inspected (``os.lstat``) and removed
        (``os.unlink``/``shutil.rmtree``) relative to that held descriptor
        (``dir_fd=``) -- never by re-resolving ``path`` or ``real`` as a
        string. A concurrent ancestor swap can no longer redirect the removal:
        the kernel refuses to open a swapped-in symlink or non-directory
        component (``ELOOP``/``ENOTDIR``), which is SURFACED as a
        :class:`LocalFileSystemError` (north-star #3: honesty) rather than
        silently followed.

        The leaf itself is never dereferenced either: when ``path`` ITSELF is a
        symlink (e.g. a breadcrumb that turned out to be a link rather than the
        real placed file), only that link entry is unlinked -- never
        ``shutil.rmtree``/target removal on whatever it points at, even though
        that target already passed the containment check above. The target may
        be OTHER library content (a different title/season) that some other
        request still references directly; eviction owns the breadcrumb it was
        given, never transitively whatever that breadcrumb happens to point to.

        A path (or an intermediate ancestor) that no longer exists at all is a
        no-op, not an error, so a retried eviction (a previous partial success,
        or a breadcrumb pointing at something already removed out-of-band) sees
        a clean, idempotent success. On a platform that cannot guarantee
        fd-anchored, no-follow removal (no ``O_NOFOLLOW`` / no ``dir_fd``
        support / no symlink-attack-resistant ``shutil.rmtree``), every delete
        is refused up front rather than silently falling back to the unsafe
        pathname re-check this method exists to avoid.
        """
        if not _delete_containment_supported():
            raise LocalFileSystemError(
                "refusing to delete: this platform cannot guarantee fd-anchored, "
                "no-follow delete containment"
            )
        # Resolve-and-check ONCE: `entry_location` is the checked entry the fd walk
        # below descends to -- this method never re-resolves `path` as a string
        # afterwards (see the docstring for why that would reopen the very TOCTOU
        # this closes).
        resolution = self._guarded_resolution(path)
        if resolution is None:
            raise LocalFileSystemError(
                f"refusing to delete {path!r}: outside every configured library root "
                "(or not a normalized path)"
            )
        entry_location, _real = resolution
        # Anchor the no-follow walk at the process filesystem root, NOT at
        # `dirname(root_real)`: the root's own parent (indeed every ancestor) must
        # be walked with O_NOFOLLOW, or a swap of the directory CONTAINING the
        # configured root -- opened by pathname if it were the anchor -- would be
        # followed. `relpath` against os.sep yields normalized components with no
        # `.`/`..` to escape the walk. `entry_location` is absolute (a realpath of
        # the entry's directory joined with its basename), so os.sep is its anchor.
        start_dir = os.sep
        components = os.path.relpath(entry_location, start_dir).split(os.sep)
        parent_fd = _open_parent_nofollow(start_dir, components, path)
        if parent_fd is None:
            return  # an intermediate ancestor is already gone -- idempotent no-op
        try:
            leaf = components[-1]
            try:
                leaf_stat = os.lstat(leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                return  # already gone -- idempotent no-op, not an error
            if stat.S_ISLNK(leaf_stat.st_mode):
                # Remove ONLY the link entry -- never follow it into its target,
                # and never shutil.rmtree a symlinked directory's contents.
                os.unlink(leaf, dir_fd=parent_fd)
            elif stat.S_ISDIR(leaf_stat.st_mode):
                shutil.rmtree(leaf, dir_fd=parent_fd)
            else:
                os.unlink(leaf, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

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
