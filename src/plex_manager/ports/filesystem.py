"""FileSystemPort — the local-filesystem interface for the import step.

Defined now, used in v1: the import pipeline (validate -> rename -> route) calls
these. Operations are synchronous (local disk). ``hardlink_or_copy`` hardlinks
when possible and falls back to a copy across devices.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Protocol, runtime_checkable

from plex_manager.domain.plex_video import PLEX_VIDEO_EXTENSIONS

__all__ = [
    "PLEX_VIDEO_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "FilePublication",
    "FileSystemPort",
    "PublishedFileIdentity",
]

# The device/inode pair captured while publication still holds the inode open. It is
# the ownership token rollback must present before deleting the destination entry.
PublishedFileIdentity = tuple[int, int]


class FilePublication(NamedTuple):
    """Result of publishing a file and the identity authorizing its rollback."""

    placed: bool
    identity: PublishedFileIdentity


# Compatibility export for callers that predate the Plex-specific policy name.
# New code should use ``PLEX_VIDEO_EXTENSIONS`` so the acceptance boundary is
# explicit rather than looking like a generic list of every video-ish suffix.
VIDEO_EXTENSIONS = PLEX_VIDEO_EXTENSIONS


@runtime_checkable
class FileSystemPort(Protocol):
    """Disk-space queries and move / hardlink-or-copy operations."""

    def available_bytes(self, path: Path) -> int:
        """Return free bytes on the filesystem containing ``path``."""
        raise NotImplementedError

    def move(self, src: Path, dst: Path, *, root: Path) -> None:
        """Move ``src`` to ``dst`` (atomic rename when on the same device).

        ``root`` is the library root the caller selected for this title; see
        :meth:`hardlink_or_copy` for the containment implementations MUST enforce.

        Raises ``NotImplementedError`` by default (issue #80): this is a
        mutating operation the import pipeline depends on to actually place a
        file, so a subclass or fake that forgets to override it must fail
        loudly at call time rather than silently no-op — a quiet no-op here
        would let an import report success while never having moved anything.
        """
        raise NotImplementedError

    def hardlink_or_copy(self, src: Path, dst: Path, *, root: Path) -> FilePublication:
        """Hardlink ``src`` to ``dst``, falling back to a copy across devices.

        Returns whether THIS call created ``dst`` plus the published entry's device/inode
        identity. The identity is an ownership token for :meth:`remove_published`; callers
        MUST pass it when rolling back a placement so a later replacement is never deleted.
        ``placed`` is ``False`` when an entry holding exactly ``src``'s bytes was already
        there (an idempotent re-import, or a concurrent import that won the placement race).
        A DIFFERENT file at ``dst`` is raised as ``FileExistsError`` -- never overwritten,
        and never reported as placed. Callers roll ``dst`` back only when ``placed`` is
        true, so the already/identical answer decides whether a file is theirs to delete;
        it MUST therefore be computed against the same verified destination directory the
        publish attempt used, never by a second pathname lookup the caller makes afterwards
        (an ancestor swapped in between would answer for a file outside the root --
        GHSA-r5vh).

        ``dst`` MUST lie beneath ``root`` -- the library root the caller selected
        for this title (the ADR-0015 anime root, or the normal one). Implementations
        MUST create and traverse every destination component below ``root`` without
        following symlinks, and MUST REFUSE (raise, never silently fall back to
        pathname publication) when an ancestor is a symlink or not a directory: a
        lexically in-root destination whose ancestor is a symlink otherwise writes
        media outside every configured root while the caller records an in-root
        breadcrumb, which the containment guard on :meth:`delete` then correctly
        refuses to clean up -- media that can no longer be corrected from the web UI.

        No-follow traversal alone is NOT sufficient, and implementations MUST also
        verify containment AFTER placing the file: an anchoring directory handle stops
        a replacement symlink from being followed, but nothing stops the directory it
        refers to from being RENAMED out of the library mid-publication, which lands
        the bytes outside every root by way of a perfectly correct write through that
        handle. Implementations MUST therefore re-resolve ``dst`` no-follow from
        ``root`` once placement is complete and confirm it names the very file just
        placed (same device and inode), MUST undo a placement of their own that fails
        this check, and MUST report success only when it passes. A rename after that
        verification is an ordinary post-import library mutation for the reconciler,
        not a containment failure -- but a breadcrumb must never be BORN pointing
        outside the root.

        Implementations MUST also prove ``src`` is a REGULAR FILE before linking or
        reading it, and MUST refuse anything else (FIFO, socket, device, directory,
        symlink): a blocking read of a FIFO swapped in at ``src`` after validation
        never returns and wedges the calling worker, and a hardlink of one publishes a
        non-media entry that every later containment check would (correctly) wave
        through on its identical inode.

        Every refusal above is signalled by RAISING -- ``LocalFileSystemError`` or an
        ``OSError`` subclass, the two the import pipeline catches and turns into a
        visible, retryable ``ImportBlocked``. There is deliberately no port-level
        exception type: callers must treat both as the same honest refusal.

        Raises ``NotImplementedError`` by default (issue #80): same rationale
        as :meth:`move` — a silent no-op default would let an import pipeline
        report a file as placed without writing or linking anything.
        """
        raise NotImplementedError

    def remove_published(self, dst: Path, *, root: Path, identity: PublishedFileIdentity) -> None:
        """Remove the matching file :meth:`hardlink_or_copy` published beneath ``root``.

        ``identity`` MUST be the ownership token returned by the publication being rolled
        back. Implementations MUST leave ``dst`` untouched when its current entry has a
        different device/inode identity: another writer replaced it and owns that entry.

        The rollback counterpart of :meth:`hardlink_or_copy`, for the import that
        placed a file and then failed a later step (a Plex scan error). It carries
        the SAME containment obligation, for the same reason: implementations MUST
        open every component below ``root`` without following symlinks and MUST
        REFUSE (raise) a symlinked or non-directory ancestor. A plain pathname
        unlink re-resolves the whole chain, so a title/season directory renamed and
        replaced by a symlink after publication would send the rollback outside every
        configured root and delete an unrelated same-named file there (GHSA-r5vh,
        CWE-59) while leaving the published file behind.

        A ``dst`` (or an ancestor) that no longer exists is a no-op, not an error --
        rollback runs on failure paths that may already have been partly applied.
        """
        raise NotImplementedError

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
