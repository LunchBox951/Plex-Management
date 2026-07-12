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
import tempfile
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

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
    barely-fitting disk. The exclusive-create guarantee against a CONCURRENT
    PUBLISHER is preserved by the per-destination ``_publish_lock`` plus the
    ``os.path.lexists(dst)`` check made under it — every publisher in this
    module takes that same lock before touching ``dst``.
    """
    with _publish_lock(dst):
        # lexists, not exists: on a hardlink-refusing filesystem the copy fallback
        # below is os.rename, which WOULD silently replace a dangling symlink's
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
            # The rename consumes the temp — nothing left to unlink.
            os.rename(tmp_path, os.fspath(dst))
            return
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _publish_link_no_overwrite(src: Path, dst: Path) -> None:
    """Publish ``src`` at ``dst`` via an exclusive hardlink under the destination lock."""
    with _publish_lock(dst):
        os.link(os.fspath(src), os.fspath(dst))


def _is_within(root_real: str, candidate_real: str) -> bool:
    """True if ``candidate_real`` is ``root_real`` or sits under it (both realpaths)."""
    return candidate_real == root_real or candidate_real.startswith(root_real + os.sep)


def _open_parent_nofollow(start_dir: str, components: list[str], original_path: str) -> int | None:
    """Open the delete leaf's PARENT directory via a no-follow ``openat`` walk,
    anchored at ``start_dir`` -- a canonical, symlink-free directory (the
    ``dirname`` of a configured library root's realpath).

    This is the enforcement layer that closes the ancestor-symlink-swap TOCTOU a
    pathname re-check cannot: every INTERMEDIATE component (``components[:-1]``
    -- the last component is the leaf itself, left for the caller to inspect and
    remove) is opened relative to the PREVIOUS component's already-open file
    descriptor with ``O_NOFOLLOW | O_DIRECTORY``. A concurrent actor who renames
    an ancestor and replaces it with a symlink (or a non-directory) between the
    containment check and this walk cannot redirect it: the kernel refuses that
    open (``ELOOP``/``ENOTDIR``) rather than following it, so the swap is
    SURFACED as a refusal (north-star #3: honesty), never silently traversed.
    Contrast a second ``os.path.realpath``/``os.path.lexists`` call, which would
    simply re-resolve through the swapped component and hand back a DIFFERENT
    real path than the one already checked.

    Returns the parent directory's fd (the caller must ``os.close`` it), or
    ``None`` when an intermediate ancestor no longer exists at all -- an
    idempotent no-op, matching :meth:`LocalFileSystem.delete`'s existing
    "already gone" contract for a path that vanished out-of-band.
    """
    dir_fd = os.open(start_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in components[:-1]:
            try:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
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

    def hardlink_or_copy(self, src: Path, dst: Path) -> None:
        """Hardlink ``src`` to ``dst``, falling back to a copy across devices.

        A cross-device link raises ``OSError`` (``EXDEV``); some filesystems also
        reject hardlinks with ``EPERM``. Either way we fall back to a metadata-
        preserving copy rather than failing the import.
        """
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            _publish_link_no_overwrite(src, dst)
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

    def _guarded_resolution(self, path: str) -> tuple[str, str, str] | None:
        """The shared resolve-and-check behind :meth:`resolve_guarded`,
        :meth:`delete_guard_refuses`, and :meth:`delete` -- returning, in
        addition to the resolved target, the configured root that anchors
        ``path``'s own entry location so :meth:`delete` can walk down to it via
        no-follow file descriptors rather than a second pathname resolution.

        Returns ``(root_real, entry_location, real)`` where ``root_real`` is the
        configured root (already a realpath, per the constructor) containing
        ``entry_location``; or ``None`` (a refusal) if either containment check
        fails. See :meth:`resolve_guarded` for the two-check rationale (issue
        #141) -- unchanged here, only the return value is richer.
        """
        if not path:
            return None
        entry_dir = os.path.dirname(path) or "."
        entry_location = os.path.join(os.path.realpath(entry_dir), os.path.basename(path))
        root_real = next(
            (root for root in self._library_roots if _is_within(root, entry_location)), None
        )
        if root_real is None:
            return None
        real = os.path.realpath(path)
        if not any(_is_within(root, real) for root in self._library_roots):
            return None
        return root_real, entry_location, real

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
        resolution = self._guarded_resolution(path)
        return None if resolution is None else resolution[2]

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
        ``O_NOFOLLOW`` from the checked root's canonical parent down to the
        leaf's own parent directory (:func:`_open_parent_nofollow`), and the
        leaf itself is inspected (``os.lstat``) and removed
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
        if not (
            hasattr(os, "O_NOFOLLOW")
            and os.unlink in os.supports_dir_fd
            and os.rmdir in os.supports_dir_fd
            and shutil.rmtree.avoids_symlink_attacks
        ):
            raise LocalFileSystemError(
                "refusing to delete: this platform cannot guarantee fd-anchored, "
                "no-follow delete containment"
            )
        # Resolve-and-check ONCE: `root_real` is the configured root whose realpath
        # anchors the fd walk below, and `entry_location` is the checked entry --
        # this method never re-resolves `path` as a string afterwards (see the
        # docstring for why that would reopen the very TOCTOU this closes).
        resolution = self._guarded_resolution(path)
        if resolution is None:
            raise LocalFileSystemError(
                f"refusing to delete {path!r}: outside every configured library root"
            )
        root_real, entry_location, _real = resolution
        start_dir = os.path.dirname(root_real) or os.sep
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
