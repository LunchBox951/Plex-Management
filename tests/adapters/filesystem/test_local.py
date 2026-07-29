"""LocalFileSystem tests — real disk operations confined to ``tmp_path``."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import queue
import shutil
import signal
import stat
import threading
import time
from collections.abc import Generator
from pathlib import Path
from types import FrameType
from typing import IO

import pytest

from plex_manager.adapters.filesystem import (
    LocalFileSystem,
    LocalFileSystemError,
    PartialDeleteError,
)
from plex_manager.adapters.filesystem import local as local_fs
from plex_manager.adapters.filesystem.local import (
    _EMPTY_LOCK_STALE_SECONDS,  # pyright: ignore[reportPrivateUsage]
)


def _reclaim_guard_path(tmp_path: Path, lock_name: str) -> Path:
    """Resolve the deterministic bounded identity-only recovery guard path."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        guard_name = local_fs._reclaim_guard_name(  # pyright: ignore[reportPrivateUsage]
            dir_fd, lock_name
        )
    finally:
        os.close(dir_fd)
    return tmp_path / guard_name


def test_available_bytes_is_positive(tmp_path: Path) -> None:
    assert LocalFileSystem().available_bytes(tmp_path) > 0


def test_available_bytes_for_nonexistent_path_uses_existing_ancestor(tmp_path: Path) -> None:
    planned = tmp_path / "not" / "yet" / "created"
    assert LocalFileSystem().available_bytes(planned) > 0


def test_move_relocates_file_and_creates_parent(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "library" / "movie" / "dst.mkv"

    LocalFileSystem().move(src, dst, root=tmp_path)

    assert not src.exists()
    assert dst.read_text() == "payload"


def test_move_refuses_existing_destination_and_preserves_both_files(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("new payload")
    dst = tmp_path / "library" / "movie" / "dst.mkv"
    dst.parent.mkdir(parents=True)
    dst.write_text("existing payload")

    with pytest.raises(FileExistsError):
        LocalFileSystem().move(src, dst, root=tmp_path)

    assert src.read_text() == "new payload"
    assert dst.read_text() == "existing payload"


def test_move_cross_device_copy_removes_source_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "library" / "dst.mkv"

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)
    LocalFileSystem().move(src, dst, root=tmp_path)

    assert not src.exists()
    assert dst.read_text() == "payload"


def test_move_cross_device_copy_refuses_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("new payload")
    dst = tmp_path / "library" / "dst.mkv"
    dst.parent.mkdir(parents=True)
    dst.write_text("existing payload")

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().move(src, dst, root=tmp_path)

    assert src.read_text() == "new payload"
    assert dst.read_text() == "existing payload"


def test_move_onto_the_same_file_is_a_noop_and_preserves_it(tmp_path: Path) -> None:
    """``move(p, p)`` must not self-destruct. ``src`` and ``dst`` being the SAME entry
    makes :meth:`hardlink_or_copy` report the idempotent match (returns ``False``,
    creates nothing); the follow-up ``src.unlink()`` would then delete the file's only
    name. The move short-circuits to a no-op instead."""
    root = tmp_path / "library"
    root.mkdir()
    path = root / "movie.mkv"
    path.write_text("payload")
    before = path.stat()

    LocalFileSystem().move(path, path, root=root)

    assert path.exists()
    assert path.read_text() == "payload"
    after = path.stat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_hardlink_or_copy_creates_linked_copy(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "linked" / "dst.mkv"

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert src.exists()  # source preserved
    assert dst.read_text() == "payload"
    # On the same device this is a true hardlink: same inode.
    assert src.stat().st_ino == dst.stat().st_ino


def test_hardlink_release_failure_rolls_back_placement_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lock-release failure leaves neither an untracked destination nor stale ownership."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock_name = ".dst.mkv.publish.lock"
    real_unlink = os.unlink
    fail_once = True

    def _fail_lock_release_once(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal fail_once
        if path == lock_name and fail_once:
            fail_once = False
            raise OSError(errno.EIO, "simulated release failure")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", _fail_lock_release_once)

    with pytest.raises(local_fs.PublicationReleaseError) as raised:
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert isinstance(raised.value.release_error, OSError)
    assert "simulated release failure" in str(raised.value.release_error)
    assert raised.value.cleanup_error is None
    assert not dst.exists()
    assert not (tmp_path / lock_name).exists()

    publication = LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert publication.placed is True
    assert dst.read_text() == "payload"


def test_hardlink_release_error_is_not_misrouted_to_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-placement release errors are typed before errno-based hardlink fallback."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock_name = ".dst.mkv.publish.lock"
    real_unlink = os.unlink
    real_link = os.link
    link_calls = 0

    def _release_eprem(path: str, *, dir_fd: int | None = None) -> None:
        if path == lock_name:
            raise OSError(errno.EPERM, "simulated release EPERM")
        real_unlink(path, dir_fd=dir_fd)

    def _record_link(*args: object, **kwargs: object) -> None:
        nonlocal link_calls
        link_calls += 1
        real_link(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", _release_eprem)
    monkeypatch.setattr(os, "link", _record_link)

    with pytest.raises(local_fs.PublicationReleaseError) as raised:
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert isinstance(raised.value.release_error, OSError)
    assert raised.value.release_error.errno == errno.EPERM
    assert link_calls == 1
    assert not dst.exists()


def test_hardlink_or_copy_hardlink_path_preserves_active_publish_lock(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "linked" / "dst.mkv"
    dst.parent.mkdir(parents=True)
    lock = dst.parent / ".dst.mkv.publish.lock"
    lock.write_text(str(os.getpid()))
    lock_fd = os.open(lock, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(FileExistsError):
            LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)
    finally:
        os.close(lock_fd)

    assert src.exists()
    assert not dst.exists()
    assert lock.read_text() == str(os.getpid())


def test_hardlink_or_copy_falls_back_to_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"
    real_link = os.link

    def _refuse_link(
        _src: str, _dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        if _src == os.fspath(src):
            raise OSError(errno.EXDEV, "simulated cross-device link")
        real_link(_src, _dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "link", _refuse_link)
    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    assert src.stat().st_ino != dst.stat().st_ino  # a copy, not a link


def test_cross_device_copy_refuses_destination_created_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("copy-path-loser")
    dst = tmp_path / "copied.mkv"
    real_link = os.link

    def _race_link(
        _src: str, _dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        if _src == os.fspath(src):
            raise OSError(errno.EXDEV, "simulated cross-device link")
        if _dst == dst.name:
            dst.write_text("race winner")
            raise FileExistsError(os.fspath(dst))
        real_link(_src, _dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "link", _race_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert src.read_text() == "copy-path-loser"
    assert dst.read_text() == "race winner"


def test_hardlink_or_copy_falls_back_when_all_hardlinks_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)
    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert src.exists()
    assert dst.read_text() == "payload"
    assert src.stat().st_ino != dst.stat().st_ino


def test_hardlinkless_publish_renames_temp_without_second_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a fully hardlinkless filesystem (SMB/FAT: EVERY os.link refuses) the
    completed temp copy must be RENAMED into the final name, not content-copied a
    second time — the old fallback wrote the title's bytes twice, transiently
    needing ~2x its size and hitting spurious ENOSPC on a barely-fitting disk."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "movie" / "copied.mkv"

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    real_copyfileobj = shutil.copyfileobj
    copies: list[int] = []

    def _counting_copy(source: IO[bytes], target: IO[bytes]) -> None:
        copies.append(source.fileno())
        real_copyfileobj(source, target)

    real_rename = os.rename
    renames: list[str] = []

    def _recording_rename(rename_src: str, rename_dst: str, **_dir_fds: int) -> None:
        renames.append(os.fspath(rename_dst))
        real_rename(rename_src, rename_dst, **_dir_fds)

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(shutil, "copyfileobj", _counting_copy)
    monkeypatch.setattr(os, "rename", _recording_rename)

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    # The content was written exactly ONCE (src -> temp); the publish is a rename.
    assert len(copies) == 1
    assert renames == [dst.name]
    # The rename consumed the temp: nothing left over next to the final file.
    leftovers = [p for p in dst.parent.iterdir() if p != dst]
    assert leftovers == []


def test_hardlinkless_publish_still_refuses_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename publish keeps the no-overwrite contract: an existing final file
    is refused under the publish lock (FileExistsError) and never replaced."""
    src = tmp_path / "src.mkv"
    src.write_text("new-download")
    dst = tmp_path / "copied.mkv"
    dst.write_text("existing library file")

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "existing library file"
    # The temp copy was cleaned up; only src and dst remain in the directory.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["copied.mkv", "src.mkv"]


def test_hardlinkless_publish_refuses_dangling_symlink_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GHSA-8fj8: ``Path.exists()`` follows symlinks, so a DANGLING symlink at
    dst used to read as "absent" -- the copy-fallback publish would then
    silently replace the symlink entry via ``os.rename``. A dangling symlink
    must refuse exactly like a real existing file, and must be left untouched
    (not resolved, not replaced)."""
    src = tmp_path / "src.mkv"
    src.write_text("new-download")
    dst = tmp_path / "copied.mkv"
    target = tmp_path / "gone.mkv"  # never created -- dst is a DANGLING symlink
    dst.symlink_to(target)
    assert dst.is_symlink()
    assert not dst.exists()  # confirms the dangling shape this test exercises

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.is_symlink()
    assert os.readlink(dst) == os.fspath(target)
    assert not target.exists()  # no real file was ever created at the target


def test_move_refuses_dangling_symlink_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same GHSA-8fj8 guard, exercised through ``move`` instead of ``hardlink_or_copy``."""
    src = tmp_path / "src.mkv"
    src.write_text("new-download")
    dst = tmp_path / "copied.mkv"
    target = tmp_path / "gone.mkv"
    dst.symlink_to(target)

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().move(src, dst, root=tmp_path)

    assert dst.is_symlink()
    assert os.readlink(dst) == os.fspath(target)
    assert not target.exists()
    assert src.exists()  # move must not have consumed src on a refused publish


def test_publish_lock_refuses_dangling_symlink_under_stale_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the OTHER GHSA-8fj8 site: the lock-contention early-exit in
    ``_publish_lock`` (line ~114). A stale (dead-pid) lock is reclaimed, and the
    dangling-symlink dst underneath it must still refuse, not be silently
    replaced."""
    src = tmp_path / "src.mkv"
    src.write_text("new-download")
    dst = tmp_path / "copied.mkv"
    target = tmp_path / "gone.mkv"
    dst.symlink_to(target)

    lock_path = tmp_path / f".{dst.name}.publish.lock"
    lock_path.write_text("999999999")  # a pid that cannot be running -- reclaimable

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.is_symlink()
    assert os.readlink(dst) == os.fspath(target)
    assert not target.exists()


def test_hardlink_or_copy_cross_device_copy_uses_temp_file_until_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"
    in_flight: list[list[str]] = []
    real_link = os.link
    real_copyfileobj = shutil.copyfileobj

    def _refuse_link(
        _src: str, _dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        if _src == os.fspath(src):
            raise OSError(errno.EXDEV, "simulated cross-device link")
        real_link(_src, _dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def _observing_copy(source: IO[bytes], target: IO[bytes]) -> None:
        real_copyfileobj(source, target)
        target.flush()
        in_flight.append(sorted(p.name for p in tmp_path.iterdir()))

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(shutil, "copyfileobj", _observing_copy)

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    # Mid-copy the bytes lived in a temp entry, never at the final name...
    assert in_flight and dst.name not in in_flight[0]
    assert [name for name in in_flight[0] if name != src.name]
    # ...and that temp is gone once the publish completes.
    assert sorted(p.name for p in tmp_path.iterdir()) == [dst.name, src.name]


def test_cross_device_copy_recovers_stale_publish_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"
    lock = tmp_path / ".copied.mkv.publish.lock"
    lock.write_text("999999999")

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    assert not lock.exists()


def test_publish_lock_uses_identity_only_cleanup_when_advisory_locking_is_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"

    def _unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "advisory locking unsupported")

    monkeypatch.setattr(fcntl, "flock", _unsupported_flock)

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    assert not lock.exists()


def test_identity_only_reclaim_close_failure_cleans_replacement_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed old-fd close must not strand the newly claimed replacement lock."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    real_close = os.close
    old_fd: int | None = None
    replacement_fd: int | None = None
    closed_fds: set[int] = set()

    def _unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "advisory locking unsupported")

    def _fail_old_fd_close_once(fd: int) -> None:
        nonlocal old_fd
        if fd == old_fd:
            old_fd = None
            real_close(fd)
            closed_fds.add(fd)
            raise OSError(errno.EIO, "simulated old lock close failure")
        real_close(fd)
        closed_fds.add(fd)

    monkeypatch.setattr(fcntl, "flock", _unsupported_flock)
    monkeypatch.setattr(os, "close", _fail_old_fd_close_once)
    real_open = os.open

    def _record_existing_lock_fd(
        path: str,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal old_fd, replacement_fd
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == lock_name:
            if flags & os.O_CREAT:
                replacement_fd = fd
            else:
                old_fd = fd
        return fd

    monkeypatch.setattr(os, "open", _record_existing_lock_fd)

    with pytest.raises(OSError, match="simulated old lock close failure"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert old_fd is None
    assert replacement_fd in closed_fds
    assert not lock.exists()
    assert not dst.exists()


def test_flock_reclaim_close_failure_closes_each_descriptor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-close EIO must not make acquisition close the stale fd a second time."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    real_open = os.open
    real_close = os.close
    old_fd: int | None = None
    replacement_fd: int | None = None
    close_calls: list[int] = []

    def _record_lock_fds(
        path: str,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal old_fd, replacement_fd
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == lock_name and not flags & os.O_CREAT:
            old_fd = fd
        elif path.startswith(".publish-lock-replace-"):
            replacement_fd = fd
        return fd

    def _close_old_then_raise(fd: int) -> None:
        close_calls.append(fd)
        real_close(fd)
        if fd == old_fd:
            raise OSError(errno.EIO, "simulated old lock close failure")

    monkeypatch.setattr(os, "open", _record_lock_fds)
    monkeypatch.setattr(os, "close", _close_old_then_raise)

    with pytest.raises(OSError, match="simulated old lock close failure"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert old_fd is not None
    assert replacement_fd is not None
    assert close_calls.count(old_fd) == 1
    assert close_calls.count(replacement_fd) == 1
    assert not lock.exists()
    assert not dst.exists()


def test_identity_only_guard_cleanup_failure_removes_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard-unlink error cannot leak a live canonical replacement lock."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    guard = _reclaim_guard_path(tmp_path, lock_name)
    real_unlink = os.unlink

    def _unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "advisory locking unsupported")

    def _fail_guard_unlink(path: str, *, dir_fd: int | None = None) -> None:
        if path == guard.name:
            raise OSError(errno.EIO, "simulated guard unlink failure")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(fcntl, "flock", _unsupported_flock)
    monkeypatch.setattr(os, "unlink", _fail_guard_unlink)

    with pytest.raises(OSError, match="simulated guard unlink failure"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert guard.exists()
    assert not lock.exists()
    assert not dst.exists()


def test_identity_only_guard_cleanup_failure_does_not_retry_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first guard-unlink failure stays primary and leaves the wedge visible."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    guard = _reclaim_guard_path(tmp_path, lock_name)
    real_unlink = os.unlink
    guard_unlink_attempts = 0

    def _unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "advisory locking unsupported")

    def _fail_guard_unlink_twice(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal guard_unlink_attempts
        if path == guard.name:
            guard_unlink_attempts += 1
            if guard_unlink_attempts == 1:
                raise OSError(errno.EIO, "first guard unlink failure")
            raise OSError(errno.EPERM, "retry guard unlink failure")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(fcntl, "flock", _unsupported_flock)
    monkeypatch.setattr(os, "unlink", _fail_guard_unlink_twice)

    with pytest.raises(OSError, match="first guard unlink failure"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert guard_unlink_attempts == 1
    assert guard.exists()
    assert not lock.exists()
    assert not dst.exists()


def test_publish_lock_reclaims_mode_0400_dead_owner_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only fallback routes NFS's EBADF flock result to guarded recovery."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    lock.chmod(0o400)
    real_open = os.open

    def _reject_existing_write_open(
        path: str,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == lock_name and not flags & os.O_CREAT and flags & os.O_ACCMODE == os.O_RDWR:
            raise OSError(errno.EACCES, "simulated mode-0400 lock")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def _read_only_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.EBADF, "NFS requires write-open descriptor for LOCK_EX")

    monkeypatch.setattr(os, "open", _reject_existing_write_open)
    monkeypatch.setattr(fcntl, "flock", _read_only_flock)

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    assert not lock.exists()


def test_publish_lock_propagates_ebadf_from_writable_existing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EBADF on a writable lock descriptor is not an identity-only fallback."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("999999999")

    def _bad_writable_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.EBADF, "simulated writable descriptor failure")

    monkeypatch.setattr(fcntl, "flock", _bad_writable_flock)

    with pytest.raises(OSError, match="simulated writable descriptor failure"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert lock.exists()
    assert not dst.exists()


def test_publish_lock_routes_enolck_to_identity_only_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOLCK means advisory ownership is unavailable, not a hard acquisition error."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("999999999")

    def _no_locks(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "simulated NFS lock exhaustion")

    monkeypatch.setattr(fcntl, "flock", _no_locks)

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    assert not lock.exists()


def test_publish_lock_flock_contention_remains_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking flock remains genuine contention rather than identity-only recovery."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("999999999")

    def _contended_flock(_fd: int, _operation: int) -> None:
        raise BlockingIOError(errno.EWOULDBLOCK, "simulated active publisher")

    monkeypatch.setattr(fcntl, "flock", _contended_flock)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert lock.exists()
    assert not dst.exists()


def test_publish_lock_reclaims_max_length_canonical_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identity-only guard names stay below NAME_MAX for full-sized canonical names."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / ("d" * 241)
    lock = tmp_path / f".{dst.name}.publish.lock"
    assert len(lock.name) == os.pathconf(tmp_path, "PC_NAME_MAX")
    lock.write_text("999999999")

    def _unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "advisory locking unsupported")

    monkeypatch.setattr(fcntl, "flock", _unsupported_flock)

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    assert not lock.exists()


def test_open_publish_lock_fstat_failure_removes_created_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A creation-time metadata failure must not strand an empty lock entry."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_name = ".dst.mkv.publish.lock"
    real_fstat = os.fstat
    failed = False

    def _fail_created_lock_fstat(fd: int) -> os.stat_result:
        nonlocal failed
        info = real_fstat(fd)
        if not failed and stat.S_ISREG(info.st_mode) and info.st_size == 0:
            failed = True
            raise OSError(errno.EIO, "simulated fstat failure")
        return info

    monkeypatch.setattr(os, "fstat", _fail_created_lock_fstat)
    try:
        with pytest.raises(OSError, match="simulated fstat failure"):
            local_fs._open_publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, lock_name, os.fspath(tmp_path / "dst.mkv")
            )
    finally:
        os.close(dir_fd)

    assert not (tmp_path / lock_name).exists()


def test_created_lock_cleanup_unlinks_empty_lock_when_flock_returns_enolck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ENOLCK cleanup route cannot strand a newly-created empty canonical lock."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_name = ".dst.mkv.publish.lock"
    real_fstat = os.fstat
    failed = False

    def _fail_created_lock_fstat(fd: int) -> os.stat_result:
        nonlocal failed
        info = real_fstat(fd)
        if not failed and stat.S_ISREG(info.st_mode) and info.st_size == 0:
            failed = True
            raise OSError(errno.EIO, "simulated fstat failure")
        return info

    def _no_locks(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "simulated lock exhaustion")

    monkeypatch.setattr(os, "fstat", _fail_created_lock_fstat)
    monkeypatch.setattr(fcntl, "flock", _no_locks)
    try:
        with pytest.raises(OSError, match="simulated fstat failure"):
            local_fs._open_publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, lock_name, os.fspath(tmp_path / "dst.mkv")
            )
    finally:
        os.close(dir_fd)

    assert not (tmp_path / lock_name).exists()


def test_created_lock_cleanup_unlinks_written_lock_when_flock_returns_enolck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-write identity failure with ENOLCK removes the created canonical lock."""
    lock_name = ".dst.mkv.publish.lock"
    real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]
    identity_checks = 0

    def _no_locks(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOLCK, "simulated lock exhaustion")

    def _lose_post_write_identity(passed_dir_fd: int, name: str, fd: int) -> bool:
        nonlocal identity_checks
        if name == lock_name:
            identity_checks += 1
            if identity_checks == 2:
                return False
        return real_owns_name(passed_dir_fd, name, fd)

    monkeypatch.setattr(fcntl, "flock", _no_locks)
    monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _lose_post_write_identity)
    claimed_fd: int | None = None
    dir_fd: int | None = None
    try:
        dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            claimed_fd = local_fs._acquire_publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd,
                lock_name,
                os.fspath(tmp_path / "dst.mkv"),
                reclaim_stale_with_existing_entry=False,
            )
        except FileExistsError:
            pass
        else:
            os.close(claimed_fd)
            claimed_fd = None
            pytest.fail("publish lock acquisition unexpectedly succeeded")
    finally:
        if claimed_fd is not None:
            os.close(claimed_fd)
        if dir_fd is not None:
            os.close(dir_fd)

    assert identity_checks >= 2
    assert not (tmp_path / lock_name).exists()


def test_publish_lock_release_surfaces_identity_probe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An indeterminate release probe must not silently strand a live PID lock."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_name = ".dst.mkv.publish.lock"
    real_stat = os.stat
    lock_identity_probes = 0

    def _fail_only_release_stat(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal lock_identity_probes
        if path == lock_name:
            lock_identity_probes += 1
            # Successful acquisition checks precede the release check.
            if lock_identity_probes == 3:
                raise OSError(errno.EIO, "transient release stat failure")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", _fail_only_release_stat)
    try:
        with (
            pytest.raises(OSError, match="transient release stat failure"),
            local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", os.fspath(tmp_path / "dst.mkv")
            ),
        ):
            pass
    finally:
        os.close(dir_fd)

    assert not (tmp_path / lock_name).exists()


def test_publish_lock_context_surfaces_unlink_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release must report a lock that it could not remove, rather than claiming success."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_name = ".dst.mkv.publish.lock"
    try:

        def _fail_canonical_lock_unlink(path: str, *, dir_fd: int | None = None) -> None:
            if path == lock_name:
                raise OSError(errno.EIO, "simulated unlink failure")

        monkeypatch.setattr(os, "unlink", _fail_canonical_lock_unlink)
        with (
            pytest.raises(OSError, match="simulated unlink failure"),
            local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", os.fspath(tmp_path / "dst.mkv")
            ),
        ):
            pass
    finally:
        os.close(dir_fd)

    # The failed unlink intentionally leaves the PID-bearing lock visible for recovery.
    assert (tmp_path / lock_name).read_text() == f"v1:{os.getpid()}\n"


def test_replace_held_publish_lock_cleans_private_temp_after_identity_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    dir_fd: int | None = None
    old_fd: int | None = None
    replacement_fd: int | None = None
    try:
        dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        old_fd = os.open(lock_name, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]

        def _lose_only_old_identity(_dir_fd: int, _name: str, fd: int) -> bool:
            if fd == old_fd:
                return False
            return real_owns_name(_dir_fd, _name, fd)

        monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _lose_only_old_identity)
        try:
            replacement_fd = local_fs._replace_held_publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, lock_name, old_fd, os.fspath(tmp_path / "dst.mkv"), advisory_locking=True
            )
        except FileExistsError:
            pass
        else:
            os.close(replacement_fd)
            old_fd = None
            pytest.fail("replacement unexpectedly succeeded")
    finally:
        if old_fd is not None:
            os.close(old_fd)
        if dir_fd is not None:
            os.close(dir_fd)

    assert not list(tmp_path.glob(".publish-lock-replace-*.tmp"))


def test_replace_held_publish_lock_cleans_private_temp_after_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    dir_fd: int | None = None
    old_fd: int | None = None
    replacement_fd: int | None = None
    try:
        dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        old_fd = os.open(lock_name, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        real_rename = os.rename

        def _fail_private_lock_rename(
            src: str,
            dst: str,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            if src.endswith(".tmp") and dst == lock_name:
                raise OSError(errno.EIO, "simulated rename failure")
            real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

        monkeypatch.setattr(os, "rename", _fail_private_lock_rename)
        try:
            replacement_fd = local_fs._replace_held_publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, lock_name, old_fd, os.fspath(tmp_path / "dst.mkv"), advisory_locking=True
            )
        except OSError as error:
            assert "simulated rename failure" in str(error)
        else:
            os.close(replacement_fd)
            old_fd = None
            pytest.fail("replacement unexpectedly succeeded")
    finally:
        if old_fd is not None:
            os.close(old_fd)
        if dir_fd is not None:
            os.close(dir_fd)

    assert not list(tmp_path.glob(".publish-lock-replace-*.tmp"))


def test_replace_held_publish_lock_retains_fd_when_post_rename_cleanup_is_unprovable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-rename identity error cannot close and strand our canonical replacement."""
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    old_fd = os.open(lock_name, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
    real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]
    replacement_fd: int | None = None
    replacement_checks = 0

    def _fail_replacement_identity(_dir_fd: int, name: str, fd: int) -> bool:
        nonlocal replacement_fd, replacement_checks
        if name == lock_name and fd != old_fd:
            replacement_fd = fd
            replacement_checks += 1
            raise OSError(errno.EIO, "simulated replacement identity failure")
        return real_owns_name(_dir_fd, name, fd)

    monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _fail_replacement_identity)
    try:
        with pytest.raises(local_fs._RetainedPublishLockReplacementError) as raised:  # pyright: ignore[reportPrivateUsage]
            local_fs._replace_held_publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, lock_name, old_fd, os.fspath(tmp_path / "dst.mkv"), advisory_locking=True
            )
        assert replacement_fd is not None
        assert raised.value.replacement_fd == replacement_fd
        assert replacement_checks == 1 + local_fs._REPLACEMENT_CLEANUP_RETRIES + 1  # pyright: ignore[reportPrivateUsage]
        assert lock.exists()
        monkeypatch.setattr(local_fs, "_lock_fd_owns_name", real_owns_name)
        local_fs._unlink_owned_publish_lock(  # pyright: ignore[reportPrivateUsage]
            dir_fd, lock_name, raised.value.replacement_fd
        )
        os.close(raised.value.replacement_fd)
        replacement_fd = None
    finally:
        if replacement_fd is not None:
            os.close(replacement_fd)
        os.close(old_fd)
        os.close(dir_fd)


def test_publish_lock_finishes_retained_replacement_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The context owner removes a replacement after its helper retries exhaust."""
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    identity_checks = 0
    real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]

    def _fail_then_recover_identity(_dir_fd: int, name: str, fd: int) -> bool:
        nonlocal identity_checks
        if name == lock_name:
            identity_checks += 1
            # The first two checks validate the existing canonical inode before
            # replacement; fail the post-rename probe and every helper cleanup
            # probe, then let the context manager's outer cleanup prove ownership.
            if identity_checks in range(3, 3 + local_fs._REPLACEMENT_CLEANUP_RETRIES + 2):  # pyright: ignore[reportPrivateUsage]
                raise OSError(errno.EIO, "simulated replacement identity failure")
        return real_owns_name(_dir_fd, name, fd)

    monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _fail_then_recover_identity)
    registry = local_fs._PublishLockReplacementRegistry()  # pyright: ignore[reportPrivateUsage]
    try:
        with (
            pytest.raises(local_fs.PublishLockReplacementError),
            local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd,
                "dst.mkv",
                os.fspath(tmp_path / "dst.mkv"),
                replacement_registry=registry,
            ),
        ):
            pytest.fail("lock acquisition unexpectedly entered")
        assert lock.exists()
        with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
            dir_fd,
            "dst.mkv",
            os.fspath(tmp_path / "dst.mkv"),
            replacement_registry=registry,
        ):
            pass
    finally:
        os.close(dir_fd)

    assert identity_checks == local_fs._REPLACEMENT_CLEANUP_RETRIES + 7  # pyright: ignore[reportPrivateUsage]
    assert not lock.exists()


def test_hardlink_retry_recovers_adapter_owned_replacement_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discarded public errors retain no caller-owned fd and heal on retry."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    checks = 0
    real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]

    def _fail_both_cleanup_layers(_dir_fd: int, name: str, fd: int) -> bool:
        nonlocal checks
        if name == lock_name:
            checks += 1
            # Existing-lock identity, old-fd pre-rename identity, then fail the
            # post-rename probe and all bounded helper cleanup probes. The next
            # public call's registry retry can prove ownership and release it.
            if checks in range(3, 3 + local_fs._REPLACEMENT_CLEANUP_RETRIES + 2):  # pyright: ignore[reportPrivateUsage]
                raise OSError(errno.EIO, "simulated replacement metadata outage")
        return real_owns_name(_dir_fd, name, fd)

    monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _fail_both_cleanup_layers)
    filesystem = LocalFileSystem()
    try:
        filesystem.hardlink_or_copy(src, dst, root=tmp_path)
    except OSError:
        pass  # Matches the broad import-service handler; it owns no descriptor.
    else:
        pytest.fail("replacement cleanup unexpectedly succeeded")

    assert lock.exists()
    publication = filesystem.hardlink_or_copy(src, dst, root=tmp_path)

    assert publication.placed is True
    assert dst.read_text() == "payload"
    assert not lock.exists()


def test_publish_lock_registry_retains_fd_across_transient_enoent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unobserved canonical name cannot dispose the adapter's held replacement."""
    lock_name = ".dst.mkv.publish.lock"
    display = os.fspath(tmp_path / "dst.mkv")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    retained_fd = os.open(
        lock_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600, dir_fd=dir_fd
    )
    os.write(retained_fd, f"v1:{os.getpid()}\n".encode("ascii"))
    registry = local_fs._PublishLockReplacementRegistry()  # pyright: ignore[reportPrivateUsage]
    key = local_fs._publish_lock_registry_key(dir_fd, lock_name)  # pyright: ignore[reportPrivateUsage]
    registry.retain(
        key,
        lock_name,
        display,
        local_fs._RetainedPublishLockReplacementError(  # pyright: ignore[reportPrivateUsage]
            retained_fd,
            OSError(errno.EIO, "original failure"),
            OSError(errno.EIO, "cleanup failure"),
        ),
    )
    real_stat = os.stat
    injected = False

    def _transient_enoent(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal injected
        if path == lock_name and not injected:
            injected = True
            raise FileNotFoundError(errno.ENOENT, "transient NFS lookup miss", path)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", _transient_enoent)
    try:
        with pytest.raises(local_fs.PublishLockReplacementError):
            registry.recover(key, dir_fd)
        assert key in registry._entries  # pyright: ignore[reportPrivateUsage]
        assert os.fstat(retained_fd).st_ino > 0
        monkeypatch.setattr(os, "stat", real_stat)
        with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
            dir_fd, "dst.mkv", display, replacement_registry=registry
        ):
            pass
        assert key not in registry._entries  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(retained_fd)
    finally:
        os.close(dir_fd)

    assert not (tmp_path / lock_name).exists()


def test_publish_lock_registry_discards_retained_fd_after_foreign_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conclusive foreign inode mismatch disposes ours and resumes acquisition."""
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    display = os.fspath(tmp_path / "dst.mkv")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    registry = local_fs._PublishLockReplacementRegistry()  # pyright: ignore[reportPrivateUsage]
    real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]
    checks = 0

    def _retain_replacement_fd(_dir_fd: int, name: str, fd: int) -> bool:
        nonlocal checks
        if name == lock_name:
            checks += 1
            if checks in range(3, 3 + local_fs._REPLACEMENT_CLEANUP_RETRIES + 2):  # pyright: ignore[reportPrivateUsage]
                raise OSError(errno.EIO, "simulated replacement metadata outage")
        return real_owns_name(_dir_fd, name, fd)

    monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _retain_replacement_fd)
    try:
        with (
            pytest.raises(local_fs.PublishLockReplacementError),
            local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", display, replacement_registry=registry
            ),
        ):
            pytest.fail("replacement cleanup unexpectedly succeeded")
        key = local_fs._publish_lock_registry_key(dir_fd, lock_name)  # pyright: ignore[reportPrivateUsage]
        retained_fd = registry._entries[key].replacement_fd  # pyright: ignore[reportPrivateUsage]
        foreign_name = ".foreign.lock"
        (tmp_path / foreign_name).write_text(f"v1:{os.getpid()}\n")
        os.replace(foreign_name, lock_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        monkeypatch.setattr(local_fs, "_lock_fd_owns_name", real_owns_name)

        with (
            pytest.raises(FileExistsError),
            local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", display, replacement_registry=registry
            ),
        ):
            pytest.fail("foreign live lock unexpectedly entered")

        assert key not in registry._entries  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(retained_fd)
    finally:
        os.close(dir_fd)

    assert lock.exists()
    assert lock.read_text() == f"v1:{os.getpid()}\n"


def test_publish_lock_registry_collision_closes_incoming_fd(tmp_path: Path) -> None:
    """A duplicate retain consumes its incoming descriptor before surfacing failure."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_name = ".dst.mkv.publish.lock"
    display = os.fspath(tmp_path / "dst.mkv")
    key = local_fs._publish_lock_registry_key(dir_fd, lock_name)  # pyright: ignore[reportPrivateUsage]
    registry = local_fs._PublishLockReplacementRegistry()  # pyright: ignore[reportPrivateUsage]
    first_fd = os.open(
        ".first.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=dir_fd
    )
    incoming_fd = os.open(
        ".incoming.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=dir_fd
    )
    try:
        first_error = local_fs._RetainedPublishLockReplacementError(  # pyright: ignore[reportPrivateUsage]
            first_fd, OSError("first"), OSError("first cleanup")
        )
        registry.retain(key, lock_name, display, first_error)
        incoming_error = local_fs._RetainedPublishLockReplacementError(  # pyright: ignore[reportPrivateUsage]
            incoming_fd, OSError("incoming"), OSError("incoming cleanup")
        )
        with pytest.raises(local_fs.PublishLockReplacementError) as raised:
            registry.retain(key, lock_name, display, incoming_error)
        assert raised.value.destination == display
        assert raised.value.lock_name == lock_name
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(incoming_fd)
        assert os.fstat(first_fd).st_ino > 0
    finally:
        entry = registry._entries.pop(key, None)  # pyright: ignore[reportPrivateUsage]
        if entry is not None:
            os.close(entry.replacement_fd)
        os.close(dir_fd)


def test_publish_lock_hardlinked_stale_entry_preserves_linked_source(tmp_path: Path) -> None:
    src = tmp_path / "source.mkv"
    original = b"old-media-payload-not-a-lock"
    src.write_bytes(original)
    old = time.time() - _EMPTY_LOCK_STALE_SECONDS - 5
    os.utime(src, (old, old))

    dst = tmp_path / "destination.mkv"
    lock = tmp_path / f".{dst.name}.publish.lock"
    os.link(src, lock)
    before = src.stat()
    assert before.st_nlink == 2

    publication = LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert publication.placed is True
    assert src.read_bytes() == original
    assert dst.read_bytes() == original
    assert src.stat().st_ino == before.st_ino == dst.stat().st_ino
    assert not lock.exists()


def test_publish_lock_empty_expired_lock_is_reclaimed(tmp_path: Path) -> None:
    """A crash between creating the lock and writing its pid leaves a zero-byte
    lock. Once it is older than the threshold it must be reclaimed, not block the
    destination forever (north-star #1: no terminal-only dead ends)."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("")  # poisoned: created, pid never written
    aged = time.time() - (_EMPTY_LOCK_STALE_SECONDS + 5)
    os.utime(lock, (aged, aged))

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    assert not lock.exists()


def test_publish_lock_incomplete_v1_pid_is_age_gated(tmp_path: Path) -> None:
    """A short v1 PID write cannot authorize immediate identity-only reclaim."""
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("v1:999999999")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_fd = os.open(lock.name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        assert not local_fs._existing_lock_is_reclaimable(lock_fd)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(lock_fd)
        os.close(dir_fd)


def test_publish_lock_reclaims_complete_v1_dead_pid(tmp_path: Path) -> None:
    """A complete v1 PID retains immediate dead-owner recovery semantics."""
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("v1:999999999\n")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_fd = os.open(lock.name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        assert local_fs._existing_lock_is_reclaimable(lock_fd)  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(lock_fd)
        os.close(dir_fd)


def test_identity_only_contender_is_age_gated_during_partial_v1_pid_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An identity-only contender cannot reclaim a creator's incomplete v1 record."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    display = os.fspath(tmp_path / "dst.mkv")
    partial_written = threading.Event()
    resume_creator = threading.Event()
    creator_inside = threading.Event()
    contender_inside = threading.Event()
    outcomes: queue.Queue[str | BaseException] = queue.Queue()
    real_write = os.write

    def _unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "advisory locking unsupported")

    def _pause_after_v1_prefix(fd: int, payload: bytes) -> int:
        if threading.current_thread().name == "partial-v1-creator" and not partial_written.is_set():
            count = real_write(fd, payload[:2])
            partial_written.set()
            assert resume_creator.wait(2)
            return count
        return real_write(fd, payload)

    def _creator() -> None:
        with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
            dir_fd, "dst.mkv", display
        ):
            creator_inside.set()

    def _contender() -> None:
        try:
            with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", display
            ):
                contender_inside.set()
                outcomes.put("entered")
        except FileExistsError as error:
            outcomes.put(error)
        except Exception as error:
            outcomes.put(error)

    monkeypatch.setattr(fcntl, "flock", _unsupported_flock)
    monkeypatch.setattr(os, "write", _pause_after_v1_prefix)
    creator = threading.Thread(target=_creator, name="partial-v1-creator")
    contender = threading.Thread(target=_contender, name="partial-v1-contender")
    try:
        creator.start()
        assert partial_written.wait(2)
        contender.start()
        contender.join(2)
        assert not contender.is_alive()
        outcome = outcomes.get_nowait()
        assert isinstance(outcome, FileExistsError)
        assert not contender_inside.is_set()
        resume_creator.set()
        creator.join(2)
        assert not creator.is_alive()
        assert creator_inside.is_set()
    finally:
        resume_creator.set()
        creator.join(2)
        contender.join(2)
        os.close(dir_fd)


def test_flock_winner_refuses_live_identity_only_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOLCK creator ownership remains authoritative after flock service recovery."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    display = os.fspath(tmp_path / "dst.mkv")
    creator_inside = threading.Event()
    allow_creator_release = threading.Event()
    creator_ready = threading.Event()
    outcomes: queue.Queue[str | BaseException] = queue.Queue()
    real_flock = fcntl.flock

    def _creator_enolck(fd: int, operation: int) -> None:
        if threading.current_thread().name == "identity-only-creator":
            raise OSError(errno.ENOLCK, "simulated initial lock exhaustion")
        real_flock(fd, operation)

    def _creator() -> None:
        try:
            with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", display
            ):
                creator_inside.set()
                creator_ready.set()
                assert allow_creator_release.wait(2)
                outcomes.put("creator-entered")
        except Exception as error:
            outcomes.put(error)

    def _contender() -> None:
        try:
            with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", display
            ):
                outcomes.put("contender-entered")
        except FileExistsError as error:
            outcomes.put(error)
        except Exception as error:
            outcomes.put(error)

    monkeypatch.setattr(fcntl, "flock", _creator_enolck)
    creator = threading.Thread(target=_creator, name="identity-only-creator")
    contender = threading.Thread(target=_contender, name="flock-contender")
    try:
        creator.start()
        assert creator_ready.wait(2)
        assert creator_inside.is_set()
        contender.start()
        contender.join(2)
        assert not contender.is_alive()
        contender_outcome = outcomes.get_nowait()
        assert isinstance(contender_outcome, FileExistsError)
        allow_creator_release.set()
        creator.join(2)
        assert not creator.is_alive()
        assert outcomes.get_nowait() == "creator-entered"
    finally:
        allow_creator_release.set()
        creator.join(2)
        contender.join(2)
        os.close(dir_fd)


def test_publish_lock_fences_creator_suspended_after_empty_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A creator that loses its empty lock may not resume into the critical section."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_name = ".dst.mkv.publish.lock"
    creator_opened = threading.Event()
    resume_creator = threading.Event()
    contender_inside = threading.Event()
    release_contender = threading.Event()
    outcomes: queue.Queue[str] = queue.Queue()
    real_open = os.open

    def _pause_creator_after_create(
        path: str,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if (
            threading.current_thread().name == "empty-lock-creator"
            and path == lock_name
            and flags & os.O_EXCL
        ):
            creator_opened.set()
            assert resume_creator.wait(2.0)
        return fd

    def _creator() -> None:
        try:
            with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", os.fspath(tmp_path / "dst.mkv")
            ):
                outcomes.put("creator-entered")
        except FileExistsError:
            outcomes.put("creator-fenced")

    def _contender() -> None:
        with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
            dir_fd, "dst.mkv", os.fspath(tmp_path / "dst.mkv")
        ):
            outcomes.put("contender-entered")
            contender_inside.set()
            assert release_contender.wait(2.0)

    monkeypatch.setattr(os, "open", _pause_creator_after_create)
    creator = threading.Thread(target=_creator, name="empty-lock-creator")
    contender = threading.Thread(target=_contender, name="empty-lock-contender")
    try:
        creator.start()
        assert creator_opened.wait(2.0)
        lock = tmp_path / lock_name
        expired = time.time() - _EMPTY_LOCK_STALE_SECONDS - 1.0
        os.utime(lock, (expired, expired))

        contender.start()
        assert contender_inside.wait(2.0)
        resume_creator.set()
        creator.join(2.0)
        assert not creator.is_alive()

        assert sorted([outcomes.get_nowait(), outcomes.get_nowait()]) == [
            "contender-entered",
            "creator-fenced",
        ]
        assert lock.exists()  # the fenced creator must not unlink B's active lock
        with (
            pytest.raises(FileExistsError),
            local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", os.fspath(tmp_path / "dst.mkv")
            ),
        ):
            pytest.fail("third claimant entered while contender held the lock")
    finally:
        resume_creator.set()
        release_contender.set()
        creator.join(2.0)
        contender.join(2.0)
        os.close(dir_fd)


def test_publish_lock_fresh_empty_contender_claims_and_fences_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A contender that owns a fresh empty lock replaces it rather than poisoning it."""
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    lock_name = ".dst.mkv.publish.lock"
    display = os.fspath(tmp_path / "dst.mkv")
    creator_created = threading.Event()
    resume_creator = threading.Event()
    contender_holds_flock = threading.Event()
    creator_failed_both_flocks = threading.Event()
    release_contender = threading.Event()
    outcomes: queue.Queue[tuple[str, str]] = queue.Queue()
    real_open = os.open
    real_flock = fcntl.flock
    creator_flock_calls = 0

    def _paused_open(
        path: str,
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if threading.current_thread().name == "creator" and path == lock_name and flags & os.O_EXCL:
            creator_created.set()
            assert resume_creator.wait(2)
        return fd

    def _ordered_flock(fd: int, operation: int) -> None:
        nonlocal creator_flock_calls
        name = threading.current_thread().name
        if name == "contender":
            real_flock(fd, operation)
            contender_holds_flock.set()
            assert release_contender.wait(2)
            return
        if name == "creator":
            creator_flock_calls += 1
            try:
                real_flock(fd, operation)
            except BlockingIOError:
                if creator_flock_calls == 2:
                    creator_failed_both_flocks.set()
                raise
            return
        real_flock(fd, operation)

    def _run(label: str) -> None:
        try:
            with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", display
            ):
                outcomes.put((label, "entered"))
        except FileExistsError:
            outcomes.put((label, "file-exists"))

    monkeypatch.setattr(os, "open", _paused_open)
    monkeypatch.setattr(fcntl, "flock", _ordered_flock)
    creator = threading.Thread(target=_run, args=("creator",), name="creator")
    contender = threading.Thread(target=_run, args=("contender",), name="contender")
    try:
        creator.start()
        assert creator_created.wait(2)
        contender.start()
        assert contender_holds_flock.wait(2)
        resume_creator.set()
        assert creator_failed_both_flocks.wait(2)
        creator.join(2)
        assert not creator.is_alive()
        release_contender.set()
        contender.join(2)
        assert not contender.is_alive()

        assert sorted([outcomes.get_nowait(), outcomes.get_nowait()]) == [
            ("contender", "entered"),
            ("creator", "file-exists"),
        ]
        assert not (tmp_path / lock_name).exists()
        with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
            dir_fd, "dst.mkv", display
        ):
            pass
    finally:
        resume_creator.set()
        release_contender.set()
        creator.join(2)
        contender.join(2)
        os.close(dir_fd)


def test_publish_lock_replaces_the_inspected_stale_inode(tmp_path: Path) -> None:
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("999999999")
    inspected = lock.stat()
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
            dir_fd, "dst.mkv", os.fspath(tmp_path / "dst.mkv")
        ):
            claimed = os.stat(lock.name, dir_fd=dir_fd, follow_symlinks=False)
            assert (claimed.st_dev, claimed.st_ino) != (
                inspected.st_dev,
                inspected.st_ino,
            )
    finally:
        os.close(dir_fd)

    assert not lock.exists()


def test_publish_lock_fresh_empty_lock_held_by_another_process_is_refused(
    tmp_path: Path,
) -> None:
    """With flock support, a held fresh empty inode remains an active claim."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("")
    lock_fd = os.open(lock, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(FileExistsError):
            LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)
    finally:
        os.close(lock_fd)

    assert not dst.exists()
    assert lock.exists()


def test_publish_lock_refuses_fresh_empty_hardlinked_inode(tmp_path: Path) -> None:
    """Immediate empty-lock recovery never claims an inode shared with user data."""
    src = tmp_path / "source.mkv"
    src.write_bytes(b"")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / f".{dst.name}.publish.lock"
    os.link(src, lock)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert not dst.exists()
    assert src.read_bytes() == b""
    assert lock.exists()
    assert src.stat().st_nlink == 2


def test_publish_lock_fresh_empty_lock_without_holder_is_replaced(tmp_path: Path) -> None:
    """With flock support, ownership—not age—authorizes empty-lock recovery."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("")
    observed = lock.stat()

    LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert dst.read_text() == "payload"
    assert not lock.exists()
    assert observed.st_nlink == 1


def test_publish_lock_identity_only_refuses_fresh_empty_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without flock support, age remains the conservative empty-lock authority."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("")

    def _unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "advisory locking unsupported")

    monkeypatch.setattr(fcntl, "flock", _unsupported_flock)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert not dst.exists()
    assert lock.exists()


def test_identity_only_loser_cannot_unlink_winners_new_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reclaim guard holds B outside A's canonical identity/unlink window."""
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    display = os.fspath(tmp_path / "dst.mkv")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    a_at_identity_check = threading.Event()
    allow_a = threading.Event()
    a_canonical_checks = 0
    outcomes: queue.Queue[tuple[str, str]] = queue.Queue()
    real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]
    real_unlink = os.unlink

    def _unsupported(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "unsupported")

    def _pause_a_identity_check(passed_dir_fd: int, name: str, fd: int) -> bool:
        nonlocal a_canonical_checks
        result = real_owns_name(passed_dir_fd, name, fd)
        if threading.current_thread().name == "a" and name == lock_name and result:
            a_canonical_checks += 1
            if a_canonical_checks == 2:
                a_at_identity_check.set()
                assert allow_a.wait(2)
        return result

    def _run(label: str) -> None:
        try:
            with local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", display
            ):
                outcomes.put((label, "entered"))
        except FileExistsError:
            outcomes.put((label, "contended"))

    monkeypatch.setattr(fcntl, "flock", _unsupported)
    monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _pause_a_identity_check)
    first = threading.Thread(target=_run, args=("a",), name="a")
    second = threading.Thread(target=_run, args=("b",), name="b")
    try:
        first.start()
        assert a_at_identity_check.wait(2)
        second.start()
        second.join(2)
        assert not second.is_alive()
        assert outcomes.get_nowait() == ("b", "contended")
        allow_a.set()
        first.join(2)
        assert not first.is_alive()
        assert outcomes.get_nowait() == ("a", "entered")
        assert not lock.exists()
    finally:
        allow_a.set()
        first.join(2)
        second.join(2)
        if lock.exists():
            real_unlink(lock_name, dir_fd=dir_fd)
        os.close(dir_fd)


def test_identity_only_fresh_reclaim_guard_refuses_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    guard = _reclaim_guard_path(tmp_path, lock_name)
    guard.write_text(str(os.getpid()))
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def _unsupported(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr(fcntl, "flock", _unsupported)
    try:
        with (
            pytest.raises(FileExistsError),
            local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", os.fspath(tmp_path / "dst.mkv")
            ),
        ):
            pytest.fail("fresh reclaim guard allowed a second reclaimer")
    finally:
        os.close(dir_fd)

    assert lock.read_text() == "999999999"
    assert guard.exists()


def test_hardlink_or_copy_preserves_reclaim_guard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public hardlink publication must not hide the operator recovery guidance."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"payload")
    dst = tmp_path / "dst.mkv"
    lock_name = f".{dst.name}.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    guard = _reclaim_guard_path(tmp_path, lock_name)
    guard.write_text("crashed-reclaimer")

    def _unsupported(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr(fcntl, "flock", _unsupported)

    with pytest.raises(local_fs.PublishLockReclaimGuardError) as raised:
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert str(guard) in str(raised.value)


def test_copy_fallback_preserves_reclaim_guard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copy fallback must preserve the same operator-visible guard failure."""
    src = tmp_path / "src.mkv"
    src.write_bytes(b"payload")
    dst = tmp_path / "dst.mkv"
    lock_name = f".{dst.name}.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    guard = _reclaim_guard_path(tmp_path, lock_name)
    guard.write_text("crashed-reclaimer")

    def _unsupported_flock(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "unsupported")

    def _cross_device(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(fcntl, "flock", _unsupported_flock)
    monkeypatch.setattr(os, "link", _cross_device)

    with pytest.raises(local_fs.PublishLockReclaimGuardError) as raised:
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert str(guard) in str(raised.value)


def test_identity_only_stale_guard_blocks_reclaim_visibly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale guard is an honest, operator-visible recovery block, never stolen."""
    lock_name = ".dst.mkv.publish.lock"
    lock = tmp_path / lock_name
    lock.write_text("999999999")
    guard = _reclaim_guard_path(tmp_path, lock_name)
    guard.write_text("crashed-reclaimer")
    old = time.time() - _EMPTY_LOCK_STALE_SECONDS - 5
    os.utime(guard, (old, old))
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def _unsupported(_fd: int, _operation: int) -> None:
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr(fcntl, "flock", _unsupported)
    try:
        with (
            pytest.raises(
                local_fs.PublishLockReclaimGuardError,
                match="crashed reclaimer left its guard artifact",
            ),
            local_fs._publish_lock(  # pyright: ignore[reportPrivateUsage]
                dir_fd, "dst.mkv", os.fspath(tmp_path / "dst.mkv")
            ),
        ):
            pytest.fail("stale reclaim guard was silently taken over")
    finally:
        os.close(dir_fd)

    assert lock.read_text() == "999999999"
    assert guard.read_text() == "crashed-reclaimer"


def test_cross_device_copy_preserves_active_publish_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"
    lock = tmp_path / ".copied.mkv.publish.lock"
    lock.write_text(str(os.getpid()))
    lock_fd = os.open(lock, os.O_RDWR | os.O_CLOEXEC)

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(FileExistsError):
            LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)
    finally:
        os.close(lock_fd)

    assert not dst.exists()
    assert lock.read_text() == str(os.getpid())


def test_hardlink_or_copy_raises_when_destination_too_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("a sizeable payload")
    dst = tmp_path / "copied.mkv"

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    def _plenty(_self: LocalFileSystem, _path: str) -> int:
        return 1

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(LocalFileSystem, "available_bytes", _plenty)

    with pytest.raises(OSError, match="insufficient space"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert not dst.exists()  # nothing written on a failed preflight


def test_hardlink_or_copy_rolls_back_partial_copy_on_size_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("the full expected payload")
    dst = tmp_path / "copied.mkv"

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    def _short_copy(_source: IO[bytes], target: IO[bytes]) -> None:
        target.write(b"short")  # truncated write

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(shutil, "copyfileobj", _short_copy)

    with pytest.raises(OSError, match="incomplete"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert not dst.exists()  # partial destination rolled back


# --------------------------------------------------------------------------- #
# GHSA-r5vh: publication must not follow symlinked ancestors out of the root
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("relative_dst", "linked_ancestor"),
    [
        ("The Matrix (1999)/The Matrix (1999).mkv", "The Matrix (1999)"),
        ("Some Show (2020)/Season 01/Some Show - S01E01.mkv", "Some Show (2020)"),
        ("Some Show (2020)/Season 01/Some Show - S01E01.mkv", "Some Show (2020)/Season 01"),
    ],
    ids=["movie-title", "show", "season"],
)
def test_hardlink_or_copy_refuses_pre_existing_symlinked_ancestor(
    tmp_path: Path, relative_dst: str, linked_ancestor: str
) -> None:
    """A destination ancestor replaced by a symlink out of the library must refuse:
    the file is what an operator later corrects/evicts by its in-root breadcrumb, and
    the delete-side guard rightly refuses an escaped target, so publishing through
    the link would be uncorrectable from the web UI."""
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    linked_path = root / linked_ancestor
    linked_path.parent.mkdir(parents=True, exist_ok=True)
    linked_path.symlink_to(outside)

    with pytest.raises(LocalFileSystemError, match="symlink or non-directory"):
        LocalFileSystem().hardlink_or_copy(src, root / relative_dst, root=root)

    assert list(outside.iterdir()) == []  # nothing created at the link's target


def test_hardlink_or_copy_refuses_non_directory_ancestor(tmp_path: Path) -> None:
    """The same refusal for an ancestor that is a plain file: ``O_DIRECTORY`` is
    what makes the walk trustworthy, so a non-directory component is a refusal, not
    a mkdir attempt that would fail with a bare, unexplained EEXIST."""
    root = tmp_path / "library"
    root.mkdir()
    (root / "The Matrix (1999)").write_text("not a directory")
    src = tmp_path / "src.mkv"
    src.write_text("payload")

    with pytest.raises(LocalFileSystemError, match="symlink or non-directory"):
        LocalFileSystem().hardlink_or_copy(
            src, root / "The Matrix (1999)" / "The Matrix (1999).mkv", root=root
        )


def test_hardlink_or_copy_refuses_destination_outside_root(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    escaped = tmp_path / "outside" / "escaped.mkv"

    with pytest.raises(LocalFileSystemError, match="outside the library root"):
        LocalFileSystem().hardlink_or_copy(src, escaped, root=root)

    assert not escaped.parent.exists()  # refused before anything was created


def test_hardlink_or_copy_refuses_when_platform_cannot_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``dir_fd``-relative, no-follow primitives there is no containment to
    enforce; publication refuses rather than silently falling back to the pathname
    publication GHSA-r5vh exploits."""
    monkeypatch.setattr(local_fs, "_PUBLICATION_CONTAINMENT_SUPPORTED", False)
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"

    with pytest.raises(LocalFileSystemError, match="platform cannot guarantee"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert not dst.exists()


def test_hardlink_or_copy_refuses_ancestor_swapped_to_symlink_mid_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ancestor-swap race a one-time ``realpath()`` check cannot close: the
    season directory is renamed away and replaced with a symlink out of the library
    at the instant of publication.

    The descriptor the walk holds still refers to the directory it verified, so the
    bytes never reach the symlink's target. But the LEXICAL destination no longer
    names them, so the breadcrumb the caller would persist is already wrong the
    moment it is written -- the post-placement verification catches that, rolls the
    entry back through the held descriptor, and refuses."""
    root = tmp_path / "library"
    season = root / "Some Show (2020)" / "Season 01"
    season.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = season / "Some Show - S01E01.mkv"
    real_link = os.link

    def _swap_then_link(
        _src: str, _dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        # The swap lands AFTER the no-follow walk verified the season directory and
        # BEFORE the entry is created -- the exact window the pathname publish lost.
        if season.is_dir() and not season.is_symlink():
            season.rename(season.parent / "Season 01.real")
            season.symlink_to(outside)
        real_link(_src, _dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "link", _swap_then_link)

    with pytest.raises(LocalFileSystemError, match="no longer names the file that was placed"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert list(outside.iterdir()) == []  # the swapped-in link never received bytes
    # The entry created through the held descriptor is undone, so no orphan is left
    # at a path the caller was never told about.
    assert list((season.parent / "Season 01.real").iterdir()) == []


def test_hardlink_or_copy_refuses_when_destination_dir_is_renamed_out_of_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GHSA-r5vh, the half a held descriptor cannot close: the verified season
    directory is renamed OUT of the library (same filesystem) and a symlink to it is
    left at the original name. The publish through the descriptor is itself correct --
    and lands outside every configured root.

    Nothing may be reported as placed here: the caller would store the in-root path as
    its breadcrumb, and correction/eviction/purge would then address a path that names
    nothing, while the real bytes sit somewhere they can never be reached."""
    root = tmp_path / "library"
    season = root / "Some Show (2020)" / "Season 01"
    season.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped_dir = outside / "Season 01.real"
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = season / "Some Show - S01E01.mkv"
    real_link = os.link

    def _escape_then_link(
        _src: str, _dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        if season.is_dir() and not season.is_symlink():
            season.rename(escaped_dir)  # the opened directory leaves the library
            season.symlink_to(escaped_dir)  # ...and its name becomes a link to it
        real_link(_src, _dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "link", _escape_then_link)

    with pytest.raises(LocalFileSystemError, match="no longer names the file that was placed"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    # The bytes that briefly landed outside the root are gone: no escaped file
    # survives a call that did not report success.
    assert list(escaped_dir.iterdir()) == []
    assert src.read_text() == "payload"  # the source is untouched, so a retry is real


def test_hardlink_or_copy_verifies_the_lexical_path_on_the_success_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containment verification is not a failure-path-only branch: an ordinary
    import runs it and passes it. Without this, the check could regress to never
    running and every escape test would still pass for the wrong reason."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    calls: list[tuple[bool, tuple[int, int]]] = []
    real_verify = local_fs._verify_lexical_publication  # pyright: ignore[reportPrivateUsage]

    def _spy(
        verify_root: Path,
        verify_dst: Path,
        parent_fd: int,
        name: str,
        published_identity: tuple[int, int],
        *,
        placed: bool,
    ) -> None:
        calls.append((placed, published_identity))
        real_verify(
            verify_root,
            verify_dst,
            parent_fd,
            name,
            published_identity,
            placed=placed,
        )

    monkeypatch.setattr(local_fs, "_verify_lexical_publication", _spy)

    assert LocalFileSystem().hardlink_or_copy(src, dst, root=root).placed is True

    # Ran once, for the file this call placed, and was handed the identity captured at
    # publication time -- the very inode the source was hardlinked to.
    assert calls == [(True, (src.stat().st_dev, src.stat().st_ino))]
    assert dst.read_text() == "payload"
    # ...and the property it asserted actually holds: the lexical path names the inode.
    assert dst.stat().st_ino == src.stat().st_ino


def test_hardlink_publication_keeps_source_fd_open_through_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The linked inode cannot be freed and reused before lexical verification."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "linked.mkv"
    real_open_source = local_fs._open_regular_source  # pyright: ignore[reportPrivateUsage]
    real_verify = local_fs._verify_lexical_publication  # pyright: ignore[reportPrivateUsage]
    source_fds: list[int] = []

    def _record_source_fd(source: Path, display: str) -> int:
        source_fd = real_open_source(source, display)
        source_fds.append(source_fd)
        return source_fd

    def _verify_while_source_is_held(
        verify_root: Path,
        verify_dst: Path,
        parent_fd: int,
        name: str,
        published_identity: tuple[int, int],
        *,
        placed: bool,
    ) -> None:
        assert len(source_fds) == 1
        held = os.fstat(source_fds[0])
        assert (held.st_dev, held.st_ino) == published_identity
        real_verify(
            verify_root,
            verify_dst,
            parent_fd,
            name,
            published_identity,
            placed=placed,
        )

    monkeypatch.setattr(local_fs, "_open_regular_source", _record_source_fd)
    monkeypatch.setattr(local_fs, "_verify_lexical_publication", _verify_while_source_is_held)

    LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert len(source_fds) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(source_fds[0])


def test_copy_publication_keeps_written_inode_fd_open_through_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy inode cannot be freed and reused before lexical verification."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "copied.mkv"
    real_link = os.link
    real_copy_no_overwrite = (
        LocalFileSystem._copy_no_overwrite  # pyright: ignore[reportPrivateUsage]
    )
    real_verify = local_fs._verify_lexical_publication  # pyright: ignore[reportPrivateUsage]
    verified_fds: list[int] = []

    def _refuse_source_link(
        link_src: str,
        link_dst: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if link_src == os.fspath(src):
            raise OSError(errno.EXDEV, "simulated cross-device link")
        real_link(link_src, link_dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    def _record_copy_no_overwrite(
        fs: LocalFileSystem,
        copy_src: Path,
        copy_dst: Path,
        source_fd: int,
        parent_fd: int,
        name: str,
    ) -> tuple[tuple[int, int], int]:
        published_identity, published_fd = real_copy_no_overwrite(
            fs, copy_src, copy_dst, source_fd, parent_fd, name
        )
        verified_fds.append(published_fd)
        return published_identity, published_fd

    def _verify_with_held_fd(
        verify_root: Path,
        verify_dst: Path,
        parent_fd: int,
        name: str,
        published_identity: tuple[int, int],
        *,
        placed: bool,
    ) -> None:
        assert len(verified_fds) == 1
        held = os.fstat(verified_fds[0])
        assert (held.st_dev, held.st_ino) == published_identity
        real_verify(
            verify_root,
            verify_dst,
            parent_fd,
            name,
            published_identity,
            placed=placed,
        )

    monkeypatch.setattr(os, "link", _refuse_source_link)
    monkeypatch.setattr(LocalFileSystem, "_copy_no_overwrite", _record_copy_no_overwrite)
    monkeypatch.setattr(local_fs, "_verify_lexical_publication", _verify_with_held_fd)

    LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert len(verified_fds) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(verified_fds[0])


def test_idempotent_digest_keeps_matched_entry_fd_open_through_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest-matched inode stays descriptor-pinned until lexical verification."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "copied.mkv"
    dst.write_text("payload")  # same bytes, distinct inode: forces digest matching
    real_idempotent_or_conflict = local_fs._idempotent_or_conflict  # pyright: ignore[reportPrivateUsage]
    real_verify = local_fs._verify_lexical_publication  # pyright: ignore[reportPrivateUsage]
    verified_fds: list[int] = []

    def _record_idempotent_or_conflict(
        source_fd: int, parent_fd: int, name: str, display: str
    ) -> tuple[tuple[int, int], int]:
        published_identity, published_fd = real_idempotent_or_conflict(
            source_fd, parent_fd, name, display
        )
        verified_fds.append(published_fd)
        return published_identity, published_fd

    def _verify_with_held_fd(
        verify_root: Path,
        verify_dst: Path,
        parent_fd: int,
        name: str,
        published_identity: tuple[int, int],
        *,
        placed: bool,
    ) -> None:
        assert len(verified_fds) == 1
        held = os.fstat(verified_fds[0])
        assert (held.st_dev, held.st_ino) == published_identity
        real_verify(
            verify_root,
            verify_dst,
            parent_fd,
            name,
            published_identity,
            placed=placed,
        )

    monkeypatch.setattr(local_fs, "_idempotent_or_conflict", _record_idempotent_or_conflict)
    monkeypatch.setattr(local_fs, "_verify_lexical_publication", _verify_with_held_fd)

    result = LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert result.placed is False
    assert len(verified_fds) == 1
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(verified_fds[0])


@contextlib.contextmanager
def _bounded(seconds: float, what: str) -> Generator[None]:
    """Turn a HANG in the body into a test FAILURE.

    The FIFO regressions below are hang bugs, so an unbounded test would stop the
    whole suite instead of reporting one red test. ``SIGALRM`` interrupts the
    blocking syscall itself -- which is the only thing that can unwedge a blocking
    ``open`` of a writer-less FIFO, and something no after-the-fact wall-clock
    assertion could ever reach.
    """

    def _fire(_signum: int, _frame: FrameType | None) -> None:
        raise TimeoutError(f"{what} blocked for more than {seconds}s")

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_hardlink_or_copy_refuses_fifo_destination_without_blocking(tmp_path: Path) -> None:
    """A pre-existing FIFO at the destination is an ordinary conflict, resolved by
    ``fstatat`` -- never opened for reading. A blocking ``O_RDONLY`` open of a FIFO
    with no writer never returns, which would wedge the import worker forever on a
    path any local actor can plant with ``mkfifo``."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = title / "The Matrix (1999).mkv"
    os.mkfifo(dst)

    with (
        _bounded(10.0, "hardlink_or_copy onto a FIFO destination"),
        pytest.raises(FileExistsError, match="already exists with different content"),
    ):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert stat.S_ISFIFO(dst.lstat().st_mode)  # untouched, for the operator to remove


@pytest.mark.parametrize("maker", ["fifo", "directory"], ids=["fifo", "directory"])
def test_hardlink_or_copy_refuses_non_regular_destination_on_the_copy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, maker: str
) -> None:
    """The same guarantee on the cross-device COPY fallback, whose publish also lands
    on the existing entry: a non-regular destination is a conflict, decided without a
    readable open."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = title / "The Matrix (1999).mkv"
    if maker == "fifo":
        os.mkfifo(dst)
    else:
        dst.mkdir()

    def _refuse_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)

    with (
        _bounded(10.0, f"hardlink_or_copy onto a {maker} destination via the copy path"),
        pytest.raises(FileExistsError, match="already exists with different content"),
    ):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert list(title.iterdir()) == [dst]  # no temp copy left behind


def _library_with_fifo_source(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A library root, a FIFO standing where the validated video was, and the intended
    destination -- the scan-to-place race an unprivileged local actor wins with
    ``mkfifo``."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    os.mkfifo(src)
    return root, src, title / "The Matrix (1999).mkv"


def test_hardlink_or_copy_refuses_fifo_source_on_the_copy_path_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SOURCE-side counterpart of the destination-side FIFO guard above.

    The copy fallback used to stream the source with a plain ``open(src, "rb")`` --
    blocking, and never asking what ``src`` actually is. An actor who swaps the
    validated video for a same-named FIFO after the scan makes that open sleep until a
    writer appears, inside ``asyncio.to_thread``: an executor thread no cancellation
    can reach, holding the hash's download lock, until the pool is exhausted and the
    whole app stalls with only a process restart left as recovery. The source must be
    proven ``S_ISREG`` before a byte is read, and refused honestly if it is not.
    """
    root, src, dst = _library_with_fifo_source(tmp_path)

    def _refuse_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)  # force the copy fallback

    with (
        _bounded(5.0, "hardlink_or_copy from a FIFO source via the copy path"),
        pytest.raises(LocalFileSystemError, match="is not a regular file"),
    ):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert not dst.exists()
    assert list(dst.parent.iterdir()) == []  # nothing published, no temp left behind


def test_hardlink_or_copy_refuses_fifo_source_on_the_hardlink_path(tmp_path: Path) -> None:
    """The same refusal on the path that never copies at all.

    ``os.link`` happily hardlinks a FIFO, and the resulting entry then passes
    :func:`_verify_lexical_publication` on its (correctly identical) inode -- so the
    call used to report success and the pipeline would persist a breadcrumb and fire a
    Plex scan for a FIFO sitting in the library. A regular-file proof of the SOURCE has
    to gate the link, not just the copy stream.
    """
    root, src, dst = _library_with_fifo_source(tmp_path)

    with (
        _bounded(5.0, "hardlink_or_copy from a FIFO source via the hardlink path"),
        pytest.raises(LocalFileSystemError, match="is not a regular file"),
    ):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert not dst.exists()  # no success, no breadcrumb-worthy entry
    assert list(dst.parent.iterdir()) == []
    assert stat.S_ISFIFO(src.lstat().st_mode)  # source untouched, for the operator


def test_hardlink_or_copy_refuses_without_unlinking_a_third_party_entry_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unlink-after-hardlink race the mismatch branch must NOT lose: ``os.link``
    succeeding proves WE created the entry AT LINK TIME, but the identity read happens
    later. A non-cooperating writer with access to the destination directory unlinks
    our fresh link and drops ITS OWN file at the same name before that read. The
    identity no longer matches the proven-regular source, so the publish is REFUSED --
    but the third party's file must survive: an entry whose ownership can no longer be
    proven is never unlinked (pre-fix, this branch deleted it)."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = title / "The Matrix (1999).mkv"
    real_link = os.link

    def _link_then_swap_entry(
        _src: str, _dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        real_link(_src, _dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        # Between our exclusive link and the identity read, a third party replaces the
        # entry at the same name with an unrelated file (a different inode).
        os.unlink(_dst, dir_fd=dst_dir_fd)
        replacement_fd = os.open(
            _dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dst_dir_fd
        )
        try:
            os.write(replacement_fd, b"third-party file")
        finally:
            os.close(replacement_fd)

    monkeypatch.setattr(os, "link", _link_then_swap_entry)

    with pytest.raises(LocalFileSystemError, match="changed identity"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    # The third party's file was NOT unlinked -- it survives with its own bytes.
    assert dst.read_bytes() == b"third-party file"


def test_hardlink_or_copy_idempotency_compares_held_source_not_a_reopened_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GHSA-r5vh, the idempotent-retry window (Finding 1): on ``EEXIST`` the existing
    destination must be compared against the descriptor :func:`_open_regular_source`
    already proved regular -- NEVER a fresh ``open(src)`` by pathname.

    An actor pre-places arbitrary destination bytes, then swaps ``src`` to a NEW inode
    holding those same bytes in the window between the proof and the compare. A re-open
    by name would read the decoy, match the destination, and report a false idempotent
    success -- finalizing and scanning attacker-chosen content. Reading the held
    descriptor sees the ORIGINAL inode's bytes, so the decoy never matches and the
    publish is refused as an honest conflict. Pre-fix this returned ``False`` (a false
    idempotent win); the fix RAISES."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    src.write_bytes(b"original-payload")  # the validated bytes, behind the held fd
    dst = title / "The Matrix (1999).mkv"
    # Pre-placed by the actor: same length, DIFFERENT bytes -> the link fails EEXIST and
    # the idempotency compare runs.
    dst.write_bytes(b"decoy-aaaaaaaaaa")

    real_open = local_fs._open_regular_source  # pyright: ignore[reportPrivateUsage]

    def _open_then_swap_source(swap_src: Path, display: str) -> int:
        fd = real_open(swap_src, display)  # holds the ORIGINAL inode (original-payload)
        # The source PATH is repointed to a new inode whose bytes equal the decoy, in
        # the window before the idempotency compare. The held fd keeps the old inode.
        planted = tmp_path / "planted.mkv"
        planted.write_bytes(b"decoy-aaaaaaaaaa")
        os.rename(planted, src)
        return fd

    monkeypatch.setattr(local_fs, "_open_regular_source", _open_then_swap_source)

    # A false idempotent success would RETURN False (no exception); the fix REFUSES.
    with pytest.raises(FileExistsError, match="already exists with different content"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    # The decoy was never adopted as "ours" -- it is left untouched for the operator.
    assert dst.read_bytes() == b"decoy-aaaaaaaaaa"


def test_hardlink_or_copy_idempotent_retry_still_matches_identical_prior_placement(
    tmp_path: Path,
) -> None:
    """The held-descriptor compare must not break the legitimate retry: a prior attempt
    that already placed exactly these bytes (an independent copy -- a different inode) is
    still recognized as an idempotent win (``False``), not a spurious conflict."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    src.write_bytes(b"identical-bytes!")
    dst = title / "The Matrix (1999).mkv"
    dst.write_bytes(b"identical-bytes!")  # a true prior placement, same bytes, diff inode

    assert LocalFileSystem().hardlink_or_copy(src, dst, root=root).placed is False
    assert dst.read_bytes() == b"identical-bytes!"


def test_hardlink_or_copy_verifier_rejects_destination_replaced_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GHSA-r5vh, Finding 2: a writer replaces the destination AFTER the placement helper
    returns but BEFORE the verifier runs.

    A verifier that re-STATS the placed entry here would see the replacement, and so
    would the lexical resolution -- the two would compare equal and the call would report
    the wrong file as placed. Comparing the lexical path against the identity captured at
    PUBLICATION time catches the swap instead. Pre-fix this returned ``True`` (a false
    success on attacker content); the fix RAISES and reports nothing placed."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    src.write_bytes(b"payload")
    dst = title / "The Matrix (1999).mkv"

    real_publish = local_fs._publish_link_no_overwrite  # pyright: ignore[reportPrivateUsage]

    def _publish_then_replace(
        publish_src: Path, source_fd: int, dir_fd: int, name: str, display: str
    ) -> object:
        # Placement completes normally; THEN a writer replaces our entry with a different
        # regular file (same directory, no escape) before the verifier runs.
        result = real_publish(publish_src, source_fd, dir_fd, name, display)
        os.unlink(name, dir_fd=dir_fd)
        replacement_fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dir_fd)
        try:
            os.write(replacement_fd, b"attacker-chosen")
        finally:
            os.close(replacement_fd)
        return result

    monkeypatch.setattr(local_fs, "_publish_link_no_overwrite", _publish_then_replace)

    with pytest.raises(LocalFileSystemError, match="no longer names the file that was placed"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    # The swapped-in file was neither adopted as placed nor (it is not ours) unlinked.
    assert dst.read_bytes() == b"attacker-chosen"


def test_hardlink_or_copy_verifier_rollback_never_unlinks_a_third_party_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GHSA-r5vh, Finding 3: the verifier's rollback carries the same hazard already
    fixed in :func:`_publish_link_no_overwrite`.

    The verified season directory escapes the library (forcing the rollback branch), and
    a third party replaces our placed entry with its own file before the rollback unlink.
    An unconditional unlink would destroy the third party's file. The fix rolls back ONLY
    when the entry is still provably the inode we published, and otherwise REFUSES WITHOUT
    UNLINKING -- an entry whose ownership can no longer be proven is never deleted (the
    residual is an in-root orphan whose breadcrumb is never persisted -- reconciler
    territory, not the ancestor-escape GHSA-r5vh concerns). Pre-fix the rollback deleted
    the third party's file."""
    root = tmp_path / "library"
    season = root / "Some Show (2020)" / "Season 01"
    season.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped_dir = outside / "Season 01.real"
    src = tmp_path / "src.mkv"
    src.write_bytes(b"payload")
    dst = season / "Some Show - S01E01.mkv"

    real_publish = local_fs._publish_link_no_overwrite  # pyright: ignore[reportPrivateUsage]

    def _publish_then_escape_and_swap(
        publish_src: Path, source_fd: int, dir_fd: int, name: str, display: str
    ) -> object:
        result = real_publish(publish_src, source_fd, dir_fd, name, display)
        # The verified directory leaves the library (its held descriptor still reaches
        # it), so the lexical re-walk hits a symlink and the verifier enters rollback.
        season.rename(escaped_dir)
        season.symlink_to(escaped_dir)
        # A third party replaces OUR placed file with its own, via the held descriptor --
        # the window the rollback unlink must not act blindly in.
        os.unlink(name, dir_fd=dir_fd)
        replacement_fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=dir_fd)
        try:
            os.write(replacement_fd, b"third-party file")
        finally:
            os.close(replacement_fd)
        return result

    monkeypatch.setattr(local_fs, "_publish_link_no_overwrite", _publish_then_escape_and_swap)

    with pytest.raises(LocalFileSystemError, match="no longer names the file that was placed"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    # The third party's file SURVIVES -- the rollback refused to unlink an entry it could
    # no longer prove it owned (pre-fix, the unconditional unlink deleted it).
    assert (escaped_dir / dst.name).read_bytes() == b"third-party file"


@pytest.mark.parametrize("maker", ["directory", "symlink"], ids=["directory", "symlink"])
def test_hardlink_or_copy_refuses_other_non_regular_sources(tmp_path: Path, maker: str) -> None:
    """Every non-regular source fails fast and visibly, not just the FIFO that can hang:
    a directory is not publishable media, and a symlinked source is refused rather than
    silently followed to whatever it now names."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    if maker == "directory":
        src.mkdir()
    else:
        os.symlink(tmp_path / "elsewhere.mkv", src)
    dst = title / "The Matrix (1999).mkv"

    with (
        _bounded(5.0, f"hardlink_or_copy from a {maker} source"),
        pytest.raises(LocalFileSystemError, match="is not a regular file"),
    ):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert list(title.iterdir()) == []


def test_hardlink_or_copy_refusing_a_fifo_source_does_not_leak_a_descriptor(
    tmp_path: Path,
) -> None:
    """The source is now opened before it is inspected, so the refusal path is an
    early return holding a live descriptor. A daemon retrying a poisoned import (an
    ``ImportBlocked`` row the operator keeps re-submitting) would leak one fd per
    attempt and walk the process into ``EMFILE``, taking every other file operation
    down with it."""
    root, src, dst = _library_with_fifo_source(tmp_path)
    fs = LocalFileSystem()

    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        pytest.skip("requires /proc/self/fd (Linux)")

    before = len(os.listdir(fd_dir))
    for _ in range(300):
        with pytest.raises(LocalFileSystemError):
            fs.hardlink_or_copy(src, dst, root=root)
    after = len(os.listdir(fd_dir))

    assert after == before


def test_hardlink_or_copy_publishes_into_a_nested_library_root(tmp_path: Path) -> None:
    """ADR-0015 nests the anime root inside the normal one; the SELECTED root is the
    anchor, so a destination beneath the nested root is published normally."""
    movies_root = tmp_path / "library" / "Movies"
    anime_root = movies_root / "Anime"
    anime_root.mkdir(parents=True)
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = anime_root / "Akira (1988)" / "Akira (1988).mkv"

    LocalFileSystem().hardlink_or_copy(src, dst, root=anime_root)

    assert dst.read_text() == "payload"


def test_largest_video_file_picks_largest_and_skips_sample_and_extras(
    tmp_path: Path,
) -> None:
    (tmp_path / "feature.mkv").write_bytes(b"x" * 1000)
    (tmp_path / "small.mp4").write_bytes(b"x" * 10)
    (tmp_path / "sample.mkv").write_bytes(b"x" * 5000)  # name-filtered despite size
    (tmp_path / "notes.txt").write_bytes(b"x" * 9000)  # non-video
    extras = tmp_path / "Featurettes"
    extras.mkdir()
    (extras / "bonus.mkv").write_bytes(b"x" * 8000)  # extras dir, skipped

    result = LocalFileSystem().largest_video_file(os.fspath(tmp_path))

    assert result is not None
    assert Path(result) == (tmp_path / "feature.mkv").resolve()


def test_largest_video_file_returns_none_without_video(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("no video here")
    (tmp_path / "art.jpg").write_bytes(b"x" * 100)

    assert LocalFileSystem().largest_video_file(os.fspath(tmp_path)) is None


def test_largest_video_file_returns_single_video_file_root(tmp_path: Path) -> None:
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"x" * 100)

    result = LocalFileSystem().largest_video_file(os.fspath(movie))

    assert result is not None
    assert Path(result) == movie.resolve()


def test_largest_video_file_returns_none_for_non_video_file_root(tmp_path: Path) -> None:
    doc = tmp_path / "movie.txt"
    doc.write_text("not a video")

    assert LocalFileSystem().largest_video_file(os.fspath(doc)) is None


def test_video_discovery_keeps_standalone_m2ts_and_excludes_standalone_vob(
    tmp_path: Path,
) -> None:
    standalone = tmp_path / "movie.m2ts"
    standalone.write_bytes(b"x" * 100)
    (tmp_path / "legacy.vob").write_bytes(b"x" * 1000)

    fs = LocalFileSystem()

    assert fs.largest_video_file(os.fspath(tmp_path)) == os.fspath(standalone.resolve())
    assert [rel for _abs, _size, rel in fs.list_video_files(os.fspath(tmp_path))] == ["movie.m2ts"]


def test_video_discovery_prunes_nested_disc_image_directories(tmp_path: Path) -> None:
    standalone = tmp_path / "feature.m2ts"
    standalone.write_bytes(b"x" * 100)
    bdmv_stream = tmp_path / "BDMV" / "STREAM"
    bdmv_stream.mkdir(parents=True)
    (bdmv_stream / "00001.m2ts").write_bytes(b"x" * 5000)
    video_ts = tmp_path / "vIdEo_Ts"
    video_ts.mkdir()
    # Use an otherwise-supported suffix to prove the directory context itself
    # prunes the tree; the independent standalone-.vob exclusion is tested above.
    (video_ts / "title.mpg").write_bytes(b"x" * 6000)

    fs = LocalFileSystem()

    assert fs.largest_video_file(os.fspath(tmp_path)) == os.fspath(standalone.resolve())
    assert [rel for _abs, _size, rel in fs.list_video_files(os.fspath(tmp_path))] == [
        "feature.m2ts"
    ]


@pytest.mark.parametrize("disc_dir_name", ["BDMV", "video_ts", "ViDeO_tS"])
def test_video_discovery_rejects_disc_image_content_root(
    tmp_path: Path, disc_dir_name: str
) -> None:
    disc_root = tmp_path / disc_dir_name
    stream = disc_root / "STREAM"
    stream.mkdir(parents=True)
    (stream / "feature.m2ts").write_bytes(b"x" * 1000)

    fs = LocalFileSystem()

    assert fs.largest_video_file(os.fspath(disc_root)) is None
    assert fs.list_video_files(os.fspath(disc_root)) == []


def test_video_discovery_rejects_single_file_root_inside_disc_structure(tmp_path: Path) -> None:
    stream = tmp_path / "BDMV" / "STREAM" / "00001.m2ts"
    stream.parent.mkdir(parents=True)
    stream.write_bytes(b"x" * 1000)

    fs = LocalFileSystem()

    assert fs.largest_video_file(os.fspath(stream)) is None
    assert fs.list_video_files(os.fspath(stream)) == []


def test_adapter_satisfies_filesystem_port() -> None:
    from plex_manager.ports.filesystem import FileSystemPort

    assert isinstance(LocalFileSystem(), FileSystemPort)


def test_hardlink_or_copy_removes_partial_temp_when_copy_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The copy can die mid-write AFTER writing part of the temp (e.g. ENOSPC when
    # another writer ate the preflighted free space). The partial temp must be
    # removed and the ORIGINAL error surfaced, so a retry sees a clean slate
    # instead of leftovers next to the destination.
    src = tmp_path / "src.mkv"
    src.write_text("the full expected payload")
    dst = tmp_path / "copied.mkv"

    def _refuse_link(_src: str, _dst: str, **_dir_fds: int) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    def _partial_then_raise(_source: IO[bytes], target: IO[bytes]) -> None:
        target.write(b"partial")  # temp partially written...
        raise OSError(errno.ENOSPC, "no space left on device")  # ...then the write dies

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(shutil, "copyfileobj", _partial_then_raise)

    with pytest.raises(OSError) as exc_info:
        LocalFileSystem().hardlink_or_copy(src, dst, root=tmp_path)

    assert exc_info.value.errno == errno.ENOSPC  # original error, not masked
    assert not dst.exists()  # nothing published
    assert [p.name for p in tmp_path.iterdir()] == [src.name]  # partial temp removed


def test_largest_video_file_rejects_symlinked_root_escaping_its_parent(
    tmp_path: Path,
) -> None:
    # A file outside the download tree the importer must never reach.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x" * 5000)

    # The download tree; the "release" content root is itself a symlink that
    # escapes the tree (e.g. /downloads/release -> /etc).
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    evil_root = downloads / "release"
    os.symlink(outside, evil_root)

    # Must refuse to surface a file from outside the download tree, exactly as
    # the single-file branch already does for an escaping symlinked file root.
    assert LocalFileSystem().largest_video_file(os.fspath(evil_root)) is None


def test_largest_video_file_allows_symlinked_downloads_parent(
    tmp_path: Path,
) -> None:
    # Real backing store; /downloads is a symlink to it (classic seedbox layout).
    store = tmp_path / "store"
    release = store / "Movie.2020"
    release.mkdir(parents=True)
    (release / "feature.mkv").write_bytes(b"x" * 1000)

    downloads = tmp_path / "downloads"
    os.symlink(store, downloads)  # symlinked PARENT, not an escaping root
    root = downloads / "Movie.2020"

    result = LocalFileSystem().largest_video_file(os.fspath(root))

    assert result is not None
    assert Path(result) == (release / "feature.mkv").resolve()


# --------------------------------------------------------------------------- #
# list_video_files — TV season-pack enumeration
# --------------------------------------------------------------------------- #
def test_list_video_files_returns_folder_qualified_relative_paths(
    tmp_path: Path,
) -> None:
    # A whole-season pack: two episodes nested under a "Season 01" directory, the
    # shape a TV import needs to parse season/episode out of the folder token, not
    # just the filename.
    season_dir = tmp_path / "Season 01"
    season_dir.mkdir()
    (season_dir / "Show.S01E01.mkv").write_bytes(b"x" * 100)
    (season_dir / "Show.S01E02.mkv").write_bytes(b"x" * 200)

    files = LocalFileSystem().list_video_files(os.fspath(tmp_path))

    by_rel = {rel: (abs_path, size) for abs_path, size, rel in files}
    assert set(by_rel) == {
        os.path.join("Season 01", "Show.S01E01.mkv"),
        os.path.join("Season 01", "Show.S01E02.mkv"),
    }
    ep1_abs, ep1_size = by_rel[os.path.join("Season 01", "Show.S01E01.mkv")]
    assert Path(ep1_abs) == (season_dir / "Show.S01E01.mkv").resolve()
    assert ep1_size == 100


def test_list_video_files_skips_sample_and_extras(tmp_path: Path) -> None:
    (tmp_path / "Show.S01E01.mkv").write_bytes(b"x" * 1000)
    (tmp_path / "Show.S01E01.sample.mkv").write_bytes(b"x" * 5000)  # name-filtered
    (tmp_path / "notes.nfo").write_bytes(b"x" * 10)  # non-video
    extras = tmp_path / "Featurettes"
    extras.mkdir()
    (extras / "bonus.mkv").write_bytes(b"x" * 8000)  # extras dir, skipped

    files = LocalFileSystem().list_video_files(os.fspath(tmp_path))

    assert [rel for _abs, _size, rel in files] == ["Show.S01E01.mkv"]


def test_list_video_files_returns_empty_list_without_video(tmp_path: Path) -> None:
    (tmp_path / "readme.txt").write_text("no video here")

    assert LocalFileSystem().list_video_files(os.fspath(tmp_path)) == []


def test_list_video_files_rejects_symlinked_root_escaping_its_parent(
    tmp_path: Path,
) -> None:
    # Mirrors largest_video_file's containment guard: a content root that is
    # ITSELF a symlink escaping its own parent must yield nothing, not the
    # outside directory's files.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x" * 5000)

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    evil_root = downloads / "release"
    os.symlink(outside, evil_root)

    assert LocalFileSystem().list_video_files(os.fspath(evil_root)) == []


def test_list_video_files_allows_symlinked_downloads_parent(tmp_path: Path) -> None:
    store = tmp_path / "store"
    release = store / "Show.2020" / "Season 01"
    release.mkdir(parents=True)
    (release / "Show.S01E01.mkv").write_bytes(b"x" * 1000)

    downloads = tmp_path / "downloads"
    os.symlink(store, downloads)  # symlinked PARENT, not an escaping root
    root = downloads / "Show.2020"

    files = LocalFileSystem().list_video_files(os.fspath(root))

    assert len(files) == 1
    abs_path, size, rel = files[0]
    assert Path(abs_path) == (release / "Show.S01E01.mkv").resolve()
    assert size == 1000
    assert rel == os.path.join("Season 01", "Show.S01E01.mkv")


# --------------------------------------------------------------------------- #
# delete — root-guarded eviction removal (ADR-0012)
# --------------------------------------------------------------------------- #
def test_delete_removes_file_within_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    target = root / "Some Movie (2020)" / "movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 100)

    LocalFileSystem([os.fspath(root)]).delete(os.fspath(target))

    assert not target.exists()


def test_delete_removes_directory_tree_within_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "tv"
    root.mkdir()
    season_dir = root / "Show" / "Season 01"
    season_dir.mkdir(parents=True)
    (season_dir / "Show.S01E01.mkv").write_bytes(b"x" * 100)
    (season_dir / "Show.S01E02.mkv").write_bytes(b"x" * 200)

    LocalFileSystem([os.fspath(root)]).delete(os.fspath(season_dir))

    assert not season_dir.exists()
    assert (root / "Show").exists()  # only the season dir is removed, not its parent


def test_delete_missing_path_is_a_noop(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    already_gone = root / "Removed Movie" / "movie.mkv"

    # Must not raise: a retried eviction (or a breadcrumb pointing at something
    # already removed out-of-band) is idempotent, not a failure.
    LocalFileSystem([os.fspath(root)]).delete(os.fspath(already_gone))


def test_delete_raises_when_no_root_is_configured(tmp_path: Path) -> None:
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"x" * 10)

    with pytest.raises(LocalFileSystemError, match="outside every configured library root"):
        LocalFileSystem().delete(os.fspath(target))

    assert target.exists()  # refused, never deleted


def test_delete_raises_for_path_outside_every_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    outside = tmp_path / "outside" / "movie.mkv"
    outside.parent.mkdir()
    outside.write_bytes(b"x" * 10)

    with pytest.raises(LocalFileSystemError, match="outside every configured library root"):
        LocalFileSystem([os.fspath(root)]).delete(os.fspath(outside))

    assert outside.exists()  # refused, never deleted


def test_delete_raises_for_missing_path_outside_every_configured_root(tmp_path: Path) -> None:
    # A path outside every root is refused REGARDLESS of whether it exists -- a
    # caller bug (wrong/misconfigured breadcrumb) must be surfaced loudly, never
    # swallowed as a harmless no-op just because there happens to be nothing there.
    root = tmp_path / "movies"
    root.mkdir()
    missing_outside = tmp_path / "outside" / "movie.mkv"

    with pytest.raises(LocalFileSystemError, match="outside every configured library root"):
        LocalFileSystem([os.fspath(root)]).delete(os.fspath(missing_outside))


def test_delete_rejects_symlink_escaping_the_configured_root(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.mkv"
    secret.write_bytes(b"x" * 10)
    # A symlink INSIDE the configured root that points OUTSIDE it -- the realpath
    # containment check must catch this even though the nominal path is textually
    # under the root, mirroring the symlink-escape guard the import scan uses.
    escaping_link = root / "escape.mkv"
    os.symlink(secret, escaping_link)

    with pytest.raises(LocalFileSystemError, match="outside every configured library root"):
        LocalFileSystem([os.fspath(root)]).delete(os.fspath(escaping_link))

    assert secret.exists()  # the real target outside the root is untouched


def test_delete_rejects_outside_root_symlink_entry_pointing_inside_the_root(
    tmp_path: Path,
) -> None:
    """Issue #141: a symlink ENTRY located OUTSIDE every configured root, whose
    TARGET resolves INSIDE one, must be refused -- the mirror image of
    ``test_delete_rejects_symlink_escaping_the_configured_root``. Before the fix,
    ``resolve_guarded`` checked only the fully-dereferenced target's containment
    (``/library/movie.mkv`` -- inside the root), so the guard passed; ``delete``
    then unlinked the symlink ENTRY (``path`` itself, never its target, per its
    own no-dereference contract for a final symlink) -- deleting an entry outside
    every configured root."""
    root = tmp_path / "movies"
    root.mkdir()
    real_target = root / "movie.mkv"
    real_target.write_bytes(b"x" * 100)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_link = outside / "link.mkv"
    os.symlink(real_target, outside_link)

    fs = LocalFileSystem([os.fspath(root)])
    assert fs.delete_guard_refuses(os.fspath(outside_link)) is True

    with pytest.raises(LocalFileSystemError, match="outside every configured library root"):
        fs.delete(os.fspath(outside_link))

    assert outside_link.is_symlink()  # the outside-root symlink entry is untouched
    assert real_target.exists()  # and the in-root target is untouched too
    assert real_target.read_bytes() == b"x" * 100


def test_delete_guard_refuses_agrees_with_delete_on_a_symlink_escaping_the_root(
    tmp_path: Path,
) -> None:
    """The extracted refusal predicate ``delete`` shares with the retention-telemetry
    would-evict simulation: it must refuse EXACTLY what ``delete`` raises on -- a
    breadcrumb lexically under the root that realpaths (via a symlinked component)
    outside it -- and allow a genuinely in-root path, all WITHOUT deleting anything."""
    root = tmp_path / "movies"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mkv").write_bytes(b"x" * 10)
    # A symlinked COMPONENT: root/escaped -> outside, so root/escaped/secret.mkv is
    # lexically under root but resolves outside it.
    os.symlink(outside, root / "escaped")
    escaping = os.fspath(root / "escaped" / "secret.mkv")
    in_root = os.fspath(root / "Some Movie" / "movie.mkv")

    fs = LocalFileSystem([os.fspath(root)])
    assert fs.delete_guard_refuses(escaping) is True
    assert fs.delete_guard_refuses(in_root) is False
    assert fs.delete_guard_refuses("") is True  # empty path fails closed
    # No configured root -> everything is refused (fails closed), same as delete.
    assert LocalFileSystem().delete_guard_refuses(in_root) is True
    # Agreement with delete(): the refused path raises, the allowed path does not.
    with pytest.raises(LocalFileSystemError, match="outside every configured library root"):
        fs.delete(escaping)
    assert (outside / "secret.mkv").exists()  # never deleted


def test_delete_removes_the_guarded_resolution_never_a_reresolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5 P1 (guard/delete TOCTOU): ``delete`` must remove the path it RESOLVED at
    guard time, never a fresh re-resolution of ``path``. If containment were checked
    on one realpath and the removal computed another, a symlinked path COMPONENT
    repointed in between would let eviction delete outside every configured root even
    though the guard passed. True atomicity between the two calls can't be forced in
    a test, so we simulate that repoint by monkeypatching ``os.path.realpath`` to
    answer in-root the FIRST time it resolves the target and out-of-root on any LATER
    call: the pre-fix double resolution would delete the escaped file, the fixed
    single resolution deletes only the guarded in-root one."""
    root = tmp_path / "movies"
    root.mkdir()
    in_root = root / "Some Movie (2020)" / "movie.mkv"
    in_root.parent.mkdir(parents=True)
    in_root.write_bytes(b"x" * 100)
    outside = tmp_path / "outside" / "escape.mkv"
    outside.parent.mkdir()
    outside.write_bytes(b"x" * 100)

    # Roots are resolved in the constructor, BEFORE the monkeypatch, so containment
    # is still measured against the genuine in-root realpath.
    fs = LocalFileSystem([os.fspath(root)])

    real_realpath = os.path.realpath
    in_root_real = real_realpath(os.fspath(in_root))
    outside_real = real_realpath(os.fspath(outside))
    target = os.fspath(in_root)
    resolves = {"count": 0}

    def repointing_realpath(candidate: str) -> str:
        if os.fspath(candidate) == target:
            resolves["count"] += 1
            # First resolution (the guard) stays in-root; a COMPONENT repoint makes
            # every subsequent resolution of the same path escape the root.
            return in_root_real if resolves["count"] == 1 else outside_real
        return real_realpath(candidate)

    monkeypatch.setattr(os.path, "realpath", repointing_realpath)

    fs.delete(target)

    # The target was resolved exactly once, and it is the guarded (in-root) path
    # that was removed -- the escaped file is untouched.
    assert resolves["count"] == 1
    assert not in_root.exists()
    assert outside.exists()


def test_delete_removes_a_symlink_breadcrumb_without_touching_its_target(
    tmp_path: Path,
) -> None:
    """R4-4: a stored ``library_path`` that turns out to be a SYMLINK (rather
    than the real placed file) -- pointing at ANOTHER title's real content,
    also inside the configured root -- must have only the symlink entry
    removed. Before the fix, ``delete`` resolved the symlink to its realpath
    and deleted THAT (the other title's actual file), leaving the symlink
    breadcrumb itself dangling and destroying unrelated library data."""
    root = tmp_path / "movies"
    root.mkdir()
    real_target = root / "Other Movie (2020)" / "movie.mkv"
    real_target.parent.mkdir(parents=True)
    real_target.write_bytes(b"x" * 100)
    # A breadcrumb that is a symlink INSIDE the root, pointing at a DIFFERENT
    # (also in-root) title's real file -- both sides pass containment.
    breadcrumb = root / "Some Movie (2020)" / "movie.mkv"
    breadcrumb.parent.mkdir(parents=True)
    os.symlink(real_target, breadcrumb)

    LocalFileSystem([os.fspath(root)]).delete(os.fspath(breadcrumb))

    assert not os.path.lexists(breadcrumb)  # the symlink entry itself is gone
    assert real_target.exists()  # the OTHER title's real content is untouched
    assert real_target.read_bytes() == b"x" * 100


def test_delete_works_across_multiple_configured_roots(tmp_path: Path) -> None:
    movies_root = tmp_path / "movies"
    tv_root = tmp_path / "tv"
    movies_root.mkdir()
    tv_root.mkdir()
    movie = movies_root / "movie.mkv"
    movie.write_bytes(b"x" * 10)
    episode = tv_root / "Show" / "episode.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"x" * 10)

    fs = LocalFileSystem([os.fspath(movies_root), os.fspath(tv_root)])
    fs.delete(os.fspath(movie))
    fs.delete(os.fspath(episode.parent))

    assert not movie.exists()
    assert not episode.parent.exists()


def test_adapter_delete_conforms_to_filesystem_port() -> None:
    from plex_manager.ports.filesystem import FileSystemPort

    assert isinstance(LocalFileSystem(), FileSystemPort)


# --------------------------------------------------------------------------- #
# delete — ancestor-symlink swap AFTER validation (fd-anchored containment)
#
# These simulate the exact race a pathname re-check cannot defend against: the
# guard resolves and validates ``path`` against the real, pre-swap tree, and
# ONLY THEN does a concurrent actor rename a writable ancestor directory and
# replace it with a symlink (or a non-directory) pointing elsewhere. A fix that
# still performs a SECOND pathname-based lookup (``lexists``/``islink``/
# ``isdir``/``rmtree``/``os.remove`` on a string) would re-traverse the swapped
# ancestor and delete whatever now sits at the same suffix, outside every
# configured root. The fd-anchored walk must instead REFUSE the swap (honesty,
# north-star #3), never follow it.
# --------------------------------------------------------------------------- #
def test_delete_ancestor_symlink_swap_after_validation_does_not_escape_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    title_dir = root / "Some Movie (2020)"
    title_dir.mkdir()
    target = title_dir / "movie.mkv"
    target.write_bytes(b"x" * 100)

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_movie = outside / "movie.mkv"
    outside_movie.write_bytes(b"y" * 100)

    fs = LocalFileSystem([os.fspath(root)])
    real_guarded_resolution = fs._guarded_resolution  # pyright: ignore[reportPrivateUsage]

    def swap_after_validation(path: str) -> tuple[str, str] | None:
        # Validate against the REAL, pre-swap tree first (the guard's honest work) --
        # then a concurrent actor wins the race: the validated ancestor directory is
        # renamed away and replaced with a symlink to a same-suffix outside tree.
        resolution = real_guarded_resolution(path)
        title_dir.rename(tmp_path / "Some Movie (2020).real")
        os.symlink(outside, title_dir)
        return resolution

    monkeypatch.setattr(fs, "_guarded_resolution", swap_after_validation)

    with pytest.raises(LocalFileSystemError, match="ancestor changed"):
        fs.delete(os.fspath(target))

    # The outside file the swap redirected onto must survive untouched.
    assert outside_movie.exists()
    assert outside_movie.read_bytes() == b"y" * 100
    # And the genuine (now-relocated) original file is untouched too.
    assert (tmp_path / "Some Movie (2020).real" / "movie.mkv").read_bytes() == b"x" * 100


def test_delete_ancestor_symlink_swap_after_validation_does_not_escape_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tv"
    root.mkdir()
    show_dir = root / "Show"
    show_dir.mkdir()
    season_dir = show_dir / "Season 01"
    season_dir.mkdir()
    (season_dir / "Show.S01E01.mkv").write_bytes(b"x" * 100)

    outside = tmp_path / "outside"
    outside.mkdir()
    outside_tree = outside / "Season 01"
    outside_tree.mkdir()
    outside_episode = outside_tree / "Show.S01E01.mkv"
    outside_episode.write_bytes(b"y" * 100)

    fs = LocalFileSystem([os.fspath(root)])
    real_guarded_resolution = fs._guarded_resolution  # pyright: ignore[reportPrivateUsage]

    def swap_after_validation(path: str) -> tuple[str, str] | None:
        resolution = real_guarded_resolution(path)
        # Swap the target directory's PARENT (not the target itself) so the
        # fd walk hits the symlink one level above the leaf being removed.
        show_dir.rename(tmp_path / "Show.real")
        os.symlink(outside, show_dir)
        return resolution

    monkeypatch.setattr(fs, "_guarded_resolution", swap_after_validation)

    with pytest.raises(LocalFileSystemError, match="ancestor changed"):
        fs.delete(os.fspath(season_dir))

    # The outside tree the swap redirected onto must survive, whole and untouched.
    assert outside_tree.exists()
    assert outside_episode.read_bytes() == b"y" * 100
    assert (tmp_path / "Show.real" / "Season 01" / "Show.S01E01.mkv").read_bytes() == b"x" * 100


def test_delete_missing_intermediate_dir_is_idempotent_noop(tmp_path: Path) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    # "Gone" was never created (or was already removed out-of-band) -- the
    # containment check still passes (both checked locations are lexically
    # under the root), but the fd walk hits a genuinely missing ancestor.
    never_existed = root / "Gone" / "movie.mkv"

    LocalFileSystem([os.fspath(root)]).delete(os.fspath(never_existed))  # must not raise


def test_delete_missing_intermediate_dir_does_not_leak_a_file_descriptor(
    tmp_path: Path,
) -> None:
    """P1 regression: the no-follow parent walk opens ``start_dir`` (and each
    intermediate ancestor) via ``os.open``, and on a MISSING intermediate
    ancestor -- exactly the idempotent-retry case above -- it used to
    ``return None`` straight out of the loop's ``except FileNotFoundError``
    branch WITHOUT closing the still-open ``dir_fd``: that ``return`` is not an
    exception, so the surrounding ``except BaseException`` cleanup never ran.
    A single call leaking one fd is invisible to
    ``test_delete_missing_intermediate_dir_is_idempotent_noop`` (which only
    asserts non-raising), but a long-running daemon retrying this exact
    idempotent path repeatedly leaks one fd per call, walking toward EMFILE and
    taking down every other file operation in the process. Assert the
    process's open-fd count is unchanged across many repeats of the no-op
    delete."""
    root = tmp_path / "movies"
    root.mkdir()
    never_existed = root / "Gone" / "movie.mkv"
    fs = LocalFileSystem([os.fspath(root)])

    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        pytest.skip("requires /proc/self/fd (Linux)")

    before = len(os.listdir(fd_dir))
    for _ in range(200):
        fs.delete(os.fspath(never_existed))  # must not raise, must not leak
    after = len(os.listdir(fd_dir))

    assert after == before


def test_delete_surfaces_ancestor_tamper_rather_than_silently_skipping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distinguishes a genuinely MISSING ancestor (idempotent no-op, see
    ``test_delete_missing_intermediate_dir_is_idempotent_noop``) from an ancestor
    that was TAMPERED WITH (replaced by a non-directory) during deletion: the
    latter must be surfaced as a refusal, never silently treated the same as a
    harmless already-gone path."""
    root = tmp_path / "movies"
    root.mkdir()
    title_dir = root / "Some Movie (2020)"
    title_dir.mkdir()
    target = title_dir / "movie.mkv"
    target.write_bytes(b"x" * 100)

    fs = LocalFileSystem([os.fspath(root)])
    real_guarded_resolution = fs._guarded_resolution  # pyright: ignore[reportPrivateUsage]

    def swap_ancestor_for_a_plain_file(path: str) -> tuple[str, str] | None:
        resolution = real_guarded_resolution(path)
        # Replace the ancestor DIRECTORY with a plain file (ENOTDIR on the
        # O_DIRECTORY-anchored open), rather than a symlink (ELOOP) -- the
        # other half of the swapped-ancestor guard.
        renamed = tmp_path / "Some Movie (2020).real"
        title_dir.rename(renamed)
        title_dir.write_bytes(b"not a directory anymore")
        return resolution

    monkeypatch.setattr(fs, "_guarded_resolution", swap_ancestor_for_a_plain_file)

    with pytest.raises(LocalFileSystemError, match="ancestor changed"):
        fs.delete(os.fspath(target))

    assert (tmp_path / "Some Movie (2020).real" / "movie.mkv").read_bytes() == b"x" * 100


def test_delete_root_parent_symlink_swap_after_validation_does_not_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P1: the swap happens ONE LEVEL ABOVE the configured root -- at the
    directory CONTAINING it (``dirname(root_real)``). An earlier fix anchored the
    no-follow walk at ``dirname(root_real)`` opened by PATHNAME and only
    no-follow-walked components BELOW it, so this level was still trusted: renaming
    the root's own parent away and dropping a symlink to a same-suffix outside tree
    in its place made the initial ``os.open`` follow the symlink, and the remaining
    no-follow walk deleted the outside file. Anchoring at the filesystem root
    (``os.sep``) and no-follow-opening EVERY component -- including the root's
    parent and the root itself -- surfaces the swap as a refusal instead."""
    lib = tmp_path / "trusted"
    root = lib / "movies"
    title_dir = root / "Some Movie (2020)"
    title_dir.mkdir(parents=True)
    target = title_dir / "movie.mkv"
    target.write_bytes(b"x" * 100)

    # An outside tree with the SAME suffix below the swapped-in symlink, so a walk
    # that follows the swap would land on -- and delete -- this file.
    outside_lib = tmp_path / "attacker"
    outside_target = outside_lib / "movies" / "Some Movie (2020)" / "movie.mkv"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_bytes(b"y" * 100)

    fs = LocalFileSystem([os.fspath(root)])
    real_guarded_resolution = fs._guarded_resolution  # pyright: ignore[reportPrivateUsage]

    def swap_root_parent_after_validation(path: str) -> tuple[str, str] | None:
        resolution = real_guarded_resolution(path)
        # The race: the directory CONTAINING the configured root is renamed away
        # and replaced by a symlink to the attacker's same-suffix tree.
        lib.rename(tmp_path / "trusted.real")
        os.symlink(outside_lib, lib)
        return resolution

    monkeypatch.setattr(fs, "_guarded_resolution", swap_root_parent_after_validation)

    with pytest.raises(LocalFileSystemError, match="ancestor changed"):
        fs.delete(os.fspath(target))

    # The attacker's same-suffix file the swap redirected onto must survive.
    assert outside_target.exists()
    assert outside_target.read_bytes() == b"y" * 100
    # And the genuine (now-relocated) original is untouched too.
    relocated = tmp_path / "trusted.real" / "movies" / "Some Movie (2020)" / "movie.mkv"
    assert relocated.read_bytes() == b"x" * 100


def test_delete_missing_root_parent_is_idempotent_noop(tmp_path: Path) -> None:
    """Codex P2: when a configured root's PARENT has disappeared (e.g. an
    unmounted ``/mnt/library``), a stale-breadcrumb delete must be an idempotent
    no-op, not a raised ``FileNotFoundError``. Containment still passes lexically
    (``realpath`` of a missing prefix is its own literal path), and the fd walk --
    now anchored at ``os.sep`` and descending every component -- hits the missing
    parent inside its ENOENT handler and returns cleanly, exactly like a missing
    intermediate ancestor. Under the earlier ``dirname(root_real)`` anchor the
    initial ``os.open`` of the missing parent raised before any handler ran."""
    library = tmp_path / "library"
    root = library / "movies"
    target = root / "Some Movie (2020)" / "movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 100)

    fs = LocalFileSystem([os.fspath(root)])  # root resolved while the mount is present
    # The mount vanishes: the root's own parent directory is gone.
    shutil.rmtree(library)
    assert not library.exists()

    fs.delete(os.fspath(target))  # must NOT raise -- already gone, idempotent no-op


def test_delete_guard_refuses_mirrors_platform_capability_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex P2: on a platform that cannot guarantee fd-anchored, no-follow delete
    containment, ``delete`` refuses EVERY path up front -- so the read-only
    predicate ``delete_guard_refuses`` (purge / retention-telemetry's would-evict
    simulation) must refuse the same in-root breadcrumbs, or callers would report
    them as evictable and walk their bytes before the real delete refuses. Both
    share ``_delete_containment_supported``; force it False and assert they agree."""
    root = tmp_path / "movies"
    root.mkdir()
    breadcrumb = root / "movie.mkv"
    breadcrumb.write_bytes(b"x" * 100)

    fs = LocalFileSystem([os.fspath(root)])
    # Sanity: with the capability present, an in-root breadcrumb is NOT refused.
    assert fs.delete_guard_refuses(os.fspath(breadcrumb)) is False

    monkeypatch.setattr(
        "plex_manager.adapters.filesystem.local._delete_containment_supported",
        lambda: False,
    )

    # The predicate must now mirror delete()'s up-front platform refusal.
    assert fs.delete_guard_refuses(os.fspath(breadcrumb)) is True
    with pytest.raises(LocalFileSystemError, match="platform cannot guarantee"):
        fs.delete(os.fspath(breadcrumb))
    assert breadcrumb.exists()  # nothing was deleted


def test_delete_refuses_dotdot_path_that_normalization_would_retarget(
    tmp_path: Path,
) -> None:
    """Codex P1: ``realpath`` collapses ``Gone/..`` LEXICALLY when ``Gone`` does
    not exist -- POSIX lookup of ``/root/Gone/../Other`` is ENOENT, yet the
    normalized guarded location names the live sibling ``/root/Other`` (and a
    ``..`` LEAF names the parent directory itself, i.e. the whole root). Acting
    on the normalized location would therefore delete an entry the supplied
    path does not name. Non-normalized paths must be refused outright -- by
    ``delete`` (raised) and ``delete_guard_refuses`` (True) alike."""
    root = tmp_path / "movies"
    root.mkdir()
    other = root / "Other"
    other.mkdir()
    survivor = other / "movie.mkv"
    survivor.write_bytes(b"x" * 100)

    fs = LocalFileSystem([os.fspath(root)])
    dotdot_sibling = f"{os.fspath(root)}{os.sep}Gone{os.sep}..{os.sep}Other"
    dotdot_leaf = f"{os.fspath(root)}{os.sep}Gone{os.sep}.."  # collapses to the root
    dot_component = f"{os.fspath(root)}{os.sep}.{os.sep}Other"

    for malformed in (dotdot_sibling, dotdot_leaf, dot_component):
        assert fs.delete_guard_refuses(malformed) is True
        with pytest.raises(LocalFileSystemError, match="refusing to delete"):
            fs.delete(malformed)

    assert survivor.read_bytes() == b"x" * 100  # the collapsed-onto sibling survives
    assert root.is_dir()  # and so does the root a '..' leaf collapses onto


def test_delete_refuses_trailing_slash_that_would_dereference_a_symlink(
    tmp_path: Path,
) -> None:
    """Codex P2: for ``/root/link.mkv/`` the basename is EMPTY, so the guarded
    entry location is built from ``realpath('/root/link.mkv')`` -- which
    dereferences the symlink -- and the walk would unlink the link's TARGET
    while the caller named the link (POSIX refuses ``link/`` with ENOTDIR).
    An empty final component must be refused outright, leaving both the link
    entry and its target untouched."""
    root = tmp_path / "movies"
    root.mkdir()
    target = root / "Other Movie (2020)" / "movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 100)
    link = root / "link.mkv"
    os.symlink(target, link)

    fs = LocalFileSystem([os.fspath(root)])
    slashed = os.fspath(link) + os.sep

    assert fs.delete_guard_refuses(slashed) is True
    with pytest.raises(LocalFileSystemError, match="refusing to delete"):
        fs.delete(slashed)

    assert link.is_symlink()  # the link entry survives
    assert target.read_bytes() == b"x" * 100  # and its target was never unlinked


def test_delete_traverses_execute_only_ancestors_like_pathname_unlink(
    tmp_path: Path,
) -> None:
    """Codex P2: plain pathname ``unlink`` needs only SEARCH (execute)
    permission on ancestors, but an ``O_RDONLY`` fd walk would demand READ on
    every one of them and spuriously EACCES on a locked-down, execute-only
    mount parent -- a path ``delete_guard_refuses`` reports as evictable. The
    walk opens ancestors with ``O_PATH`` (search-only) where available, so a
    breadcrumb under an execute-only ancestor still deletes."""
    if not hasattr(os, "O_PATH"):
        pytest.skip("requires O_PATH (Linux) for search-only ancestor traversal")
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission checks")
    locked = tmp_path / "locked"
    root = locked / "movies"
    target = root / "Some Movie (2020)" / "movie.mkv"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x" * 100)

    fs = LocalFileSystem([os.fspath(root)])  # realpath'd while readable
    # Owner-only masks: 0o100 (owner execute-only, no read) is the scenario
    # under test -- the walk runs as the owner, so owner-x alone proves
    # search-only traversal, and is STRICTER than a world-execute mask.
    os.chmod(locked, 0o100)  # execute-only: search yes, read no
    try:
        assert fs.delete_guard_refuses(os.fspath(target)) is False
        fs.delete(os.fspath(target))
        assert not target.exists()
    finally:
        os.chmod(locked, 0o700)  # restore (owner-only) so pytest can clean tmp_path


def test_delete_reports_a_partial_removal_when_the_tree_top_survives(tmp_path: Path) -> None:
    """Issue #482: ``shutil.rmtree`` is NOT atomic -- it can remove children and
    then raise, leaving a tree that still exists but is missing files. A caller
    (eviction) reacts to that in the OPPOSITE way it reacts to a failure that
    removed nothing, so the two must be distinguishable.

    Reproduced deterministically against the real filesystem: a season directory
    whose PARENT is read-only (``0o500`` -- listable and searchable, not
    writable). ``rmtree`` empties the season fine and only the final ``rmdir``
    of the season itself needs write permission on that parent, so every child
    is really gone and the top really survives, on every entry ordering. The
    retry afterwards proves the convergence the caller relies on: an
    already-removed entry is an idempotent no-op, so re-purging the remains
    succeeds once the obstruction clears."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission checks")
    root = tmp_path / "tv"
    show = root / "Some Show"
    season = show / "Season 01"
    (season / "Specials").mkdir(parents=True)
    episode = season / "Some Show - S01E01.mkv"
    episode.write_bytes(b"x" * 100)
    nested = season / "Specials" / "Some Show - S01E02.mkv"
    nested.write_bytes(b"x" * 100)

    fs = LocalFileSystem([os.fspath(root)])
    os.chmod(show, 0o500)  # the season's own rmdir needs write HERE; its contents do not
    try:
        with pytest.raises(PartialDeleteError, match="partially deleted before failing"):
            fs.delete(os.fspath(season))
        # The tree was genuinely eaten into: the media is no longer playable,
        # which is exactly what the caller must not restore to 'available'.
        assert not episode.exists()
        assert not nested.exists()
        assert season.is_dir()
    finally:
        os.chmod(show, 0o700)  # restore so the retry (and pytest's cleanup) can proceed

    fs.delete(os.fspath(season))  # idempotent retry of the remains
    assert not season.exists()


def test_delete_propagates_an_untouched_failure_unchanged(tmp_path: Path) -> None:
    """Issue #482's other side: a delete that failed having removed NOTHING must
    keep raising its plain ``OSError``, never the partial signal -- the media is
    still complete and still watchable, and the caller's restore-to-'available'
    depends on that distinction being honest in BOTH directions.

    A read-only season directory (``0o500``) is the deterministic shape: no entry
    inside it can be unlinked, so ``rmtree`` fails on the very first one whatever
    order it scans in, and the whole tree is provably intact afterwards."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission checks")
    root = tmp_path / "tv"
    season = root / "Some Show" / "Season 01"
    (season / "Specials").mkdir(parents=True)
    episode = season / "Some Show - S01E01.mkv"
    episode.write_bytes(b"x" * 100)
    nested = season / "Specials" / "Some Show - S01E02.mkv"
    nested.write_bytes(b"x" * 100)

    fs = LocalFileSystem([os.fspath(root)])
    os.chmod(season, 0o500)
    try:
        with pytest.raises(PermissionError) as raised:
            fs.delete(os.fspath(season))
        assert not isinstance(raised.value, PartialDeleteError)
    finally:
        os.chmod(season, 0o700)  # restore so pytest can clean tmp_path

    assert episode.read_bytes() == b"x" * 100  # nothing was removed
    assert nested.read_bytes() == b"x" * 100


def test_delete_reports_partial_when_the_pre_inventory_walk_could_not_read_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An INCOMPLETE pre-removal inventory must never certify a failure as
    "untouched": ``before <= after`` only proves nothing was removed when
    ``before`` actually held everything the tree contained.

    A nested directory unreadable at inventory time contributes only its own
    entry (its children are never listed), so if access recovers before
    ``shutil.rmtree`` runs -- a permission flap, or an operator fixing modes
    mid-sweep -- the removal can unlink those children and still leave both
    inventoried entries in place. The diff then reads as untouched over a tree
    that really did lose files, and the caller restores it to 'available'."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permission checks")
    root = tmp_path / "tv"
    season = root / "Some Show" / "Season 01"
    specials = season / "Specials"
    specials.mkdir(parents=True)
    nested = specials / "Some Show - S01E02.mkv"
    nested.write_bytes(b"x" * 100)

    real_rmtree = shutil.rmtree

    class RecoveringRmtree:
        """``shutil.rmtree`` with access to ``specials`` restored just before it
        runs -- the flap ``delete``'s pre-inventory walk cannot see coming.
        Carries ``avoids_symlink_attacks`` because ``delete`` gates on it."""

        avoids_symlink_attacks = real_rmtree.avoids_symlink_attacks

        def __call__(self, path: str, *, dir_fd: int | None = None) -> None:
            os.chmod(specials, 0o700)
            real_rmtree(path, dir_fd=dir_fd)

    monkeypatch.setattr(shutil, "rmtree", RecoveringRmtree())

    fs = LocalFileSystem([os.fspath(root)])
    os.chmod(specials, 0o000)  # unlistable while the inventory is taken
    os.chmod(season, 0o500)  # not writable, so removing 'Specials' itself still fails
    try:
        with pytest.raises(PartialDeleteError):
            fs.delete(os.fspath(season))
    finally:
        os.chmod(season, 0o700)
        os.chmod(specials, 0o700)

    assert not nested.exists()  # a file really did leave, despite the equal diff
    assert specials.is_dir()


# --------------------------------------------------------------------------- #
# reclaimable_bytes — hardlink-aware freed-bytes accounting (R4-6, ADR-0012)
# --------------------------------------------------------------------------- #
def test_reclaimable_bytes_reports_full_size_for_a_single_link_file(tmp_path: Path) -> None:
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"x" * 500)

    assert LocalFileSystem().reclaimable_bytes(os.fspath(target)) == 500


def test_reclaimable_bytes_reports_zero_for_a_file_with_another_hard_link(tmp_path: Path) -> None:
    # A same-filesystem import (hardlink_or_copy) can leave the placed library
    # file with another hard link still present -- e.g. the download client's
    # own seed copy, never removed at import finalize. Deleting only THIS path
    # would free nothing: the inode's bytes stay allocated via the other link.
    target = tmp_path / "movie.mkv"
    target.write_bytes(b"x" * 500)
    other_link = tmp_path / "seed" / "movie.mkv"
    other_link.parent.mkdir()
    os.link(target, other_link)

    assert LocalFileSystem().reclaimable_bytes(os.fspath(target)) == 0


def test_reclaimable_bytes_for_a_directory_sums_only_single_link_files(tmp_path: Path) -> None:
    season_dir = tmp_path / "Show" / "Season 01"
    season_dir.mkdir(parents=True)
    single_link = season_dir / "Show.S01E01.mkv"
    single_link.write_bytes(b"x" * 300)
    hardlinked = season_dir / "Show.S01E02.mkv"
    hardlinked.write_bytes(b"x" * 700)
    seed_copy = tmp_path / "seed" / "Show.S01E02.mkv"
    seed_copy.parent.mkdir()
    os.link(hardlinked, seed_copy)

    # Only E01 (single-link, 300 bytes) is actually reclaimable; E02's bytes
    # stay allocated via its other hard link.
    assert LocalFileSystem().reclaimable_bytes(os.fspath(season_dir)) == 300


def test_reclaimable_bytes_is_zero_for_a_missing_path(tmp_path: Path) -> None:
    missing = tmp_path / "already-gone.mkv"

    assert LocalFileSystem().reclaimable_bytes(os.fspath(missing)) == 0


def test_reclaimable_bytes_is_zero_for_a_symlink_to_a_real_file(tmp_path: Path) -> None:
    # R5-2: a stored library_path can be a symlink to a single-linked file.
    # delete() only ever unlinks the symlink entry itself (never dereferences
    # it), so accounting must match: reclaiming a symlink frees ~nothing, NOT
    # the target's size (os.path.isfile/os.stat both follow symlinks, which is
    # exactly the bug -- they must never be trusted directly on `path`).
    real_target = tmp_path / "real" / "movie.mkv"
    real_target.parent.mkdir()
    real_target.write_bytes(b"x" * 900)
    link_path = tmp_path / "library" / "movie.mkv"
    link_path.parent.mkdir()
    os.symlink(real_target, link_path)

    assert LocalFileSystem().reclaimable_bytes(os.fspath(link_path)) == 0


def test_reclaimable_bytes_for_a_directory_skips_a_symlinked_file(tmp_path: Path) -> None:
    # A season dir can contain a symlinked episode alongside real files (e.g. a
    # breadcrumb pointing at content actually stored elsewhere). Only the real,
    # single-linked files are reclaimable; the symlinked entry contributes 0
    # bytes, matching that shutil.rmtree unlinks the link rather than freeing
    # whatever it points at.
    season_dir = tmp_path / "Show" / "Season 01"
    season_dir.mkdir(parents=True)
    single_link = season_dir / "Show.S01E01.mkv"
    single_link.write_bytes(b"x" * 300)
    real_target = tmp_path / "elsewhere" / "Show.S01E02.mkv"
    real_target.parent.mkdir()
    real_target.write_bytes(b"x" * 900)
    symlinked_episode = season_dir / "Show.S01E02.mkv"
    os.symlink(real_target, symlinked_episode)

    # Only E01 (300 bytes, real single-linked file) counts; the symlinked E02
    # must NOT contribute its target's 900 bytes.
    assert LocalFileSystem().reclaimable_bytes(os.fspath(season_dir)) == 300


def test_hardlink_or_copy_reports_placement_and_idempotent_skip(tmp_path: Path) -> None:
    """The publish result carries both rollback ownership and the published inode."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "The Matrix (1999)" / "The Matrix (1999).mkv"

    placed = LocalFileSystem().hardlink_or_copy(src, dst, root=root)
    idempotent = LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert placed.placed is True
    assert placed.identity == (dst.stat().st_dev, dst.stat().st_ino)
    assert idempotent.placed is False
    assert idempotent.identity == placed.identity


def test_hardlink_or_copy_conflicts_on_a_same_size_different_file(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    dst.parent.mkdir(parents=True)
    dst.write_text("PAYLOAD")  # same size, different bytes

    with pytest.raises(FileExistsError, match="different content"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert dst.read_text() == "PAYLOAD"  # never overwritten


def test_hardlink_or_copy_never_treats_a_dangling_symlink_as_an_idempotent_skip(
    tmp_path: Path,
) -> None:
    """GHSA-8fj8: a dangling symlink at dst reads as "absent" under ``exists()``. The
    entry comparison opens ``O_NOFOLLOW``, so it is an honest conflict -- never a
    "someone already placed our file" skip that would finalize a breadcrumb pointing
    at nothing."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "dst.mkv"
    target = root / "gone.mkv"  # never created
    dst.symlink_to(target)

    with pytest.raises(FileExistsError, match="different content"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert dst.is_symlink()
    assert not target.exists()


def test_hardlink_or_copy_idempotent_skip_ignores_a_swapped_in_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ancestor swap landing between the refused exclusive create and the
    already-there-and-identical decision. That decision is made against the held
    descriptor, so a same-content decoy planted outside the root can never be
    mistaken for our destination and reported as an idempotent skip."""
    root = tmp_path / "library"
    title = root / "The Matrix (1999)"
    title.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = title / "The Matrix (1999).mkv"
    dst.write_text("occupied")  # in-root: a DIFFERENT file -- an honest conflict
    (outside / dst.name).write_text("payload")  # out-of-root decoy: same content
    real_link = os.link

    def _swap_after_eexist(
        _src: str, _dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        try:
            real_link(_src, _dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        except FileExistsError:
            if title.is_dir() and not title.is_symlink():
                title.rename(title.parent / "The Matrix (1999).real")
                title.symlink_to(outside)
            raise

    monkeypatch.setattr(os, "link", _swap_after_eexist)

    with pytest.raises(FileExistsError, match="different content"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)


def test_remove_published_unlinks_the_file_it_placed(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)

    fs.remove_published(dst, root=root, identity=publication.identity)

    assert not dst.exists()
    assert dst.parent.is_dir()  # only the file goes, never the season directory


def test_remove_published_reclaims_a_stale_publish_lock(tmp_path: Path) -> None:
    """A crash after placement can leave the rollback's lock behind. Its dead PID
    proves it is stale, so rollback may reclaim it and remove the file it owns."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    lock.write_text("999999999")

    fs.remove_published(dst, root=root, identity=publication.identity)

    assert not dst.exists()
    assert not lock.exists()


def test_remove_published_refuses_an_expired_fifo_publish_lock_without_blocking(
    tmp_path: Path,
) -> None:
    """Rollback lock inspection must not block on or reclaim a non-regular lock entry."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    os.mkfifo(lock)
    expired = time.time() - _EMPTY_LOCK_STALE_SECONDS - 1.0
    os.utime(lock, (expired, expired))

    started = time.monotonic()
    with (
        _bounded(1.0, "remove_published with a FIFO publish lock"),
        pytest.raises(FileExistsError),
    ):
        fs.remove_published(dst, root=root, identity=publication.identity)

    # Load-bearing hang detector: _bounded's SIGALRM raises TimeoutError, an OSError
    # the adapter's inspection paths swallow into the expected FileExistsError — only
    # this elapsed bound distinguishes a blocking open from a fast refusal.
    assert time.monotonic() - started < 0.5
    assert dst.exists()
    assert stat.S_ISFIFO(lock.lstat().st_mode)


def test_remove_published_refuses_fifo_replacing_an_inspected_stale_lock_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The identity re-check must not block when a FIFO wins the stale-lock race."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    lock.write_text("999999999")
    real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]
    old_identity_checks = 0

    def _replace_before_reclaim(passed_dir_fd: int, name: str, fd: int) -> bool:
        nonlocal old_identity_checks
        result = real_owns_name(passed_dir_fd, name, fd)
        if name == lock.name and result:
            old_identity_checks += 1
            if old_identity_checks == 2:
                lock.unlink()
                os.mkfifo(lock)
                return False
        return result

    monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _replace_before_reclaim)

    with pytest.raises(FileExistsError):
        fs.remove_published(dst, root=root, identity=publication.identity)

    assert dst.exists()
    assert stat.S_ISFIFO(lock.lstat().st_mode)


def test_remove_published_refuses_a_live_publish_lock(tmp_path: Path) -> None:
    """Rollback cannot break a lock owned by a process still running, even when it
    owns the destination's inode; that process may be changing the entry."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    lock.write_text(str(os.getpid()))
    lock_fd = os.open(lock, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(FileExistsError):
            fs.remove_published(dst, root=root, identity=publication.identity)
    finally:
        os.close(lock_fd)

    assert dst.exists()
    assert lock.read_text() == str(os.getpid())


def test_remove_published_refuses_a_fresh_indeterminate_publish_lock(tmp_path: Path) -> None:
    """An empty fresh lock can be a concurrent publisher between lock creation and
    PID write, so rollback must preserve both it and the destination."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    lock.write_text("")

    with pytest.raises(FileExistsError):
        fs.remove_published(dst, root=root, identity=publication.identity)

    assert dst.exists()
    assert lock.exists()


@pytest.mark.parametrize("lock_contents", ["0", "-1"])
def test_remove_published_refuses_a_nonpositive_pid_publish_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lock_contents: str
) -> None:
    """A nonpositive PID must be indeterminate, never a process-group probe."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    lock.write_text(lock_contents)

    def _must_not_probe(_pid: int, _signal: int) -> None:
        raise AssertionError("nonpositive lock PID must not be process-probed")

    monkeypatch.setattr(os, "kill", _must_not_probe)

    with pytest.raises(FileExistsError):
        fs.remove_published(dst, root=root, identity=publication.identity)

    assert dst.exists()
    assert lock.read_text() == lock_contents


def test_remove_published_refuses_an_overflow_pid_publish_lock(tmp_path: Path) -> None:
    """An integer that overflows this platform's pid_t is indeterminate, not stale."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    lock.write_text("2147483648")

    with pytest.raises(FileExistsError):
        fs.remove_published(dst, root=root, identity=publication.identity)

    assert dst.exists()
    assert lock.read_text() == "2147483648"


def test_remove_published_refuses_when_stale_lock_is_replaced_before_reclaim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reclaim must target the stale lock that was inspected, not a live replacement
    another rollback installed at the same name before the unlink."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    lock.write_text("999999999")
    real_owns_name = local_fs._lock_fd_owns_name  # pyright: ignore[reportPrivateUsage]
    old_identity_checks = 0

    def _replace_before_reclaim(passed_dir_fd: int, name: str, fd: int) -> bool:
        nonlocal old_identity_checks
        result = real_owns_name(passed_dir_fd, name, fd)
        if name == lock.name and result:
            old_identity_checks += 1
            if old_identity_checks == 2:
                replacement = tmp_path / "replacement.lock"
                replacement.write_text(str(os.getpid()))
                os.replace(replacement, lock)
                return False
        return result

    monkeypatch.setattr(local_fs, "_lock_fd_owns_name", _replace_before_reclaim)

    with pytest.raises(FileExistsError):
        fs.remove_published(dst, root=root, identity=publication.identity)

    assert dst.exists()
    assert lock.read_text() == str(os.getpid())


def test_remove_published_refuses_when_lock_is_replaced_after_pid_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A claimant that loses its name while publishing its PID must not yield."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    lock = dst.parent / f".{dst.name}.publish.lock"
    lock.write_text("999999999")
    write_pid = local_fs._write_lock_pid  # pyright: ignore[reportPrivateUsage]

    def _replace_after_pid(lock_fd: int) -> None:
        write_pid(lock_fd)
        replacement = tmp_path / "replacement.lock"
        replacement.write_text(str(os.getpid()))
        os.replace(replacement, lock)

    monkeypatch.setattr(local_fs, "_write_lock_pid", _replace_after_pid)

    with pytest.raises(FileExistsError):
        fs.remove_published(dst, root=root, identity=publication.identity)

    assert dst.exists()
    assert lock.read_text() == str(os.getpid())


def test_remove_published_refuses_to_unlink_a_replacement(tmp_path: Path) -> None:
    """Rollback ownership is the inode captured at publication, not just the path.

    A writer that replaces ``dst`` after publication but before scan-failure rollback
    owns the new entry. The old unconditional unlink deleted that replacement.
    """
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "Some Show (2020)" / "Season 01" / "Some Show - S01E01.mkv"
    fs = LocalFileSystem()
    publication = fs.hardlink_or_copy(src, dst, root=root)
    replacement = tmp_path / "replacement.mkv"
    replacement.write_text("third-party file")
    os.replace(replacement, dst)

    fs.remove_published(dst, root=root, identity=publication.identity)

    assert dst.read_text() == "third-party file"


def test_remove_published_is_a_no_op_when_already_gone(tmp_path: Path) -> None:
    """Rollback runs on failure paths that may already be partly applied; a missing
    leaf OR a missing ancestor is an honest no-op, not an error."""
    root = tmp_path / "library"
    root.mkdir()
    fs = LocalFileSystem()

    fs.remove_published(root / "Show" / "Season 01" / "gone.mkv", root=root, identity=(0, 0))
    (root / "Show").mkdir()
    fs.remove_published(root / "Show" / "gone.mkv", root=root, identity=(0, 0))


def test_remove_published_refuses_an_ancestor_swapped_after_publication(
    tmp_path: Path,
) -> None:
    """GHSA-r5vh, CWE-59: publication legitimately completes into the directory whose
    descriptor the walk verified, even when that directory is renamed away mid-publish.
    Rolling that placement back by pathname afterwards would re-resolve through the
    symlink left in its place and unlink an unrelated same-named file OUTSIDE the root.
    The anchored removal refuses instead."""
    root = tmp_path / "library"
    season = root / "Some Show (2020)" / "Season 01"
    season.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = season / "Some Show - S01E01.mkv"
    LocalFileSystem().hardlink_or_copy(src, dst, root=root)
    victim = outside / dst.name
    victim.write_text("someone else's file")

    # The swap the publish walk's descriptor rode out, now visible by pathname.
    season.rename(season.parent / "Season 01.real")
    season.symlink_to(outside)

    with pytest.raises(LocalFileSystemError, match="symlink or non-directory"):
        LocalFileSystem().remove_published(
            dst, root=root, identity=(dst.stat().st_dev, dst.stat().st_ino)
        )

    assert victim.read_text() == "someone else's file"
    assert (season.parent / "Season 01.real" / dst.name).exists()


def test_remove_published_refuses_a_destination_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    escaped = tmp_path / "outside" / "escaped.mkv"
    escaped.parent.mkdir()
    escaped.write_text("payload")

    with pytest.raises(LocalFileSystemError, match="outside the library root"):
        LocalFileSystem().remove_published(escaped, root=root, identity=(0, 0))

    assert escaped.exists()


def test_remove_published_refuses_when_platform_cannot_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(local_fs, "_PUBLICATION_CONTAINMENT_SUPPORTED", False)
    dst = tmp_path / "dst.mkv"
    dst.write_text("payload")

    with pytest.raises(LocalFileSystemError, match="platform cannot guarantee"):
        LocalFileSystem().remove_published(
            dst, root=tmp_path, identity=(dst.stat().st_dev, dst.stat().st_ino)
        )

    assert dst.exists()


def test_hardlink_or_copy_conflicts_when_a_directory_occupies_the_destination(
    tmp_path: Path,
) -> None:
    """A directory sitting where the media file belongs is a conflict for the operator
    to resolve, never an idempotent skip -- only a REGULAR file can be the file a
    previous attempt placed."""
    root = tmp_path / "library"
    root.mkdir()
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = root / "The Matrix (1999)" / "The Matrix (1999).mkv"
    dst.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="different content"):
        LocalFileSystem().hardlink_or_copy(src, dst, root=root)

    assert dst.is_dir()  # left for the operator, never removed
