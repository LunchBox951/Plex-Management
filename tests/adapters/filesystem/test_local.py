"""LocalFileSystem tests — real disk operations confined to ``tmp_path``."""

from __future__ import annotations

import errno
import os
import shutil
import time
from pathlib import Path

import pytest

from plex_manager.adapters.filesystem import LocalFileSystem, LocalFileSystemError
from plex_manager.adapters.filesystem.local import (
    _EMPTY_LOCK_STALE_SECONDS,  # pyright: ignore[reportPrivateUsage]
)


def test_available_bytes_is_positive(tmp_path: Path) -> None:
    assert LocalFileSystem().available_bytes(tmp_path) > 0


def test_available_bytes_for_nonexistent_path_uses_existing_ancestor(tmp_path: Path) -> None:
    planned = tmp_path / "not" / "yet" / "created"
    assert LocalFileSystem().available_bytes(planned) > 0


def test_move_relocates_file_and_creates_parent(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "library" / "movie" / "dst.mkv"

    LocalFileSystem().move(src, dst)

    assert not src.exists()
    assert dst.read_text() == "payload"


def test_move_refuses_existing_destination_and_preserves_both_files(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("new payload")
    dst = tmp_path / "library" / "movie" / "dst.mkv"
    dst.parent.mkdir(parents=True)
    dst.write_text("existing payload")

    with pytest.raises(FileExistsError):
        LocalFileSystem().move(src, dst)

    assert src.read_text() == "new payload"
    assert dst.read_text() == "existing payload"


def test_move_cross_device_copy_removes_source_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "library" / "dst.mkv"

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)
    LocalFileSystem().move(src, dst)

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

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().move(src, dst)

    assert src.read_text() == "new payload"
    assert dst.read_text() == "existing payload"


def test_hardlink_or_copy_creates_linked_copy(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "linked" / "dst.mkv"

    LocalFileSystem().hardlink_or_copy(src, dst)

    assert src.exists()  # source preserved
    assert dst.read_text() == "payload"
    # On the same device this is a true hardlink: same inode.
    assert src.stat().st_ino == dst.stat().st_ino


def test_hardlink_or_copy_hardlink_path_preserves_active_publish_lock(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "linked" / "dst.mkv"
    dst.parent.mkdir(parents=True)
    lock = dst.parent / ".dst.mkv.publish.lock"
    lock.write_text(str(os.getpid()))

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst)

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

    def _refuse_link(_src: str, _dst: str) -> None:
        if _src == os.fspath(src):
            raise OSError(errno.EXDEV, "simulated cross-device link")
        real_link(_src, _dst)

    monkeypatch.setattr(os, "link", _refuse_link)
    LocalFileSystem().hardlink_or_copy(src, dst)

    assert dst.read_text() == "payload"
    assert src.stat().st_ino != dst.stat().st_ino  # a copy, not a link


def test_cross_device_copy_refuses_destination_created_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("copy-path-loser")
    dst = tmp_path / "copied.mkv"
    real_link = os.link

    def _race_link(_src: str, _dst: str) -> None:
        if _src == os.fspath(src):
            raise OSError(errno.EXDEV, "simulated cross-device link")
        if _dst == os.fspath(dst):
            dst.write_text("race winner")
            raise FileExistsError(os.fspath(dst))
        real_link(_src, _dst)

    monkeypatch.setattr(os, "link", _race_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst)

    assert src.read_text() == "copy-path-loser"
    assert dst.read_text() == "race winner"


def test_hardlink_or_copy_falls_back_when_all_hardlinks_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)
    LocalFileSystem().hardlink_or_copy(src, dst)

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

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    real_copy2 = shutil.copy2
    copies: list[tuple[str, str]] = []

    def _counting_copy2(copy_src: str, copy_dst: str) -> None:
        copies.append((copy_src, copy_dst))
        real_copy2(copy_src, copy_dst)

    real_rename = os.rename
    renames: list[tuple[str, str]] = []

    def _recording_rename(rename_src: str, rename_dst: str) -> None:
        renames.append((os.fspath(rename_src), os.fspath(rename_dst)))
        real_rename(rename_src, rename_dst)

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(shutil, "copy2", _counting_copy2)
    monkeypatch.setattr(os, "rename", _recording_rename)

    LocalFileSystem().hardlink_or_copy(src, dst)

    assert dst.read_text() == "payload"
    # The content was written exactly ONCE (src -> temp); the publish is a rename.
    assert len(copies) == 1
    assert copies[0][0] == os.fspath(src)
    assert [rename_dst for _s, rename_dst in renames] == [os.fspath(dst)]
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

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst)

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

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst)

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

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().move(src, dst)

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

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EPERM, "hardlinks unsupported")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst)

    assert dst.is_symlink()
    assert os.readlink(dst) == os.fspath(target)
    assert not target.exists()


def test_hardlink_or_copy_cross_device_copy_uses_temp_file_until_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"
    observed_copy_dst: list[Path] = []
    real_link = os.link

    def _refuse_link(_src: str, _dst: str) -> None:
        if _src == os.fspath(src):
            raise OSError(errno.EXDEV, "simulated cross-device link")
        real_link(_src, _dst)

    def _copy2(_src: str, dst_arg: str) -> None:
        copy_dst = Path(dst_arg)
        observed_copy_dst.append(copy_dst)
        copy_dst.write_text("partial")
        assert not dst.exists(), "final path must not exist while copy is in progress"
        copy_dst.write_text("payload")

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(shutil, "copy2", _copy2)

    LocalFileSystem().hardlink_or_copy(src, dst)

    assert dst.read_text() == "payload"
    assert observed_copy_dst and observed_copy_dst[0] != dst
    assert not observed_copy_dst[0].exists()


def test_cross_device_copy_recovers_stale_publish_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"
    lock = tmp_path / ".copied.mkv.publish.lock"
    lock.write_text("999999999")

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)

    LocalFileSystem().hardlink_or_copy(src, dst)

    assert dst.read_text() == "payload"
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

    LocalFileSystem().hardlink_or_copy(src, dst)

    assert dst.read_text() == "payload"
    assert not lock.exists()


def test_publish_lock_fresh_empty_lock_is_not_reclaimed(tmp_path: Path) -> None:
    """A just-created empty lock is a concurrent creator still mid-write, NOT a
    poisoned one; it must be left alone so the in-flight publisher keeps it."""
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "dst.mkv"
    lock = tmp_path / ".dst.mkv.publish.lock"
    lock.write_text("")  # empty but fresh (mtime == now)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst)

    assert not dst.exists()
    assert lock.exists()  # preserved for the in-flight creator


def test_cross_device_copy_preserves_active_publish_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "copied.mkv"
    lock = tmp_path / ".copied.mkv.publish.lock"
    lock.write_text(str(os.getpid()))

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)

    with pytest.raises(FileExistsError):
        LocalFileSystem().hardlink_or_copy(src, dst)

    assert not dst.exists()
    assert lock.read_text() == str(os.getpid())


def test_hardlink_or_copy_raises_when_destination_too_small(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("a sizeable payload")
    dst = tmp_path / "copied.mkv"

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    def _plenty(_self: LocalFileSystem, _path: str) -> int:
        return 1

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(LocalFileSystem, "available_bytes", _plenty)

    with pytest.raises(OSError, match="insufficient space"):
        LocalFileSystem().hardlink_or_copy(src, dst)

    assert not dst.exists()  # nothing written on a failed preflight


def test_hardlink_or_copy_rolls_back_partial_copy_on_size_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("the full expected payload")
    dst = tmp_path / "copied.mkv"

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    def _short_copy2(_src: str, dst_arg: str) -> None:
        Path(dst_arg).write_text("short")  # truncated write

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(shutil, "copy2", _short_copy2)

    with pytest.raises(OSError, match="incomplete"):
        LocalFileSystem().hardlink_or_copy(src, dst)

    assert not dst.exists()  # partial destination rolled back


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


def test_hardlink_or_copy_removes_partial_dst_when_copy_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # copy2 can die mid-write AFTER creating dst (e.g. ENOSPC when another writer
    # ate the preflighted free space). The partial file must be removed and the
    # ORIGINAL error surfaced, so a retry sees a clean slate instead of a
    # differently-sized dst that _place_file would reject as a persistent conflict.
    src = tmp_path / "src.mkv"
    src.write_text("the full expected payload")
    dst = tmp_path / "copied.mkv"

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError(errno.EXDEV, "simulated cross-device link")

    def _partial_then_raise(_src: str, dst_arg: str) -> None:
        Path(dst_arg).write_text("partial")  # dst created/truncated...
        raise OSError(errno.ENOSPC, "no space left on device")  # ...then the write dies

    monkeypatch.setattr(os, "link", _refuse_link)
    monkeypatch.setattr(shutil, "copy2", _partial_then_raise)

    with pytest.raises(OSError) as exc_info:
        LocalFileSystem().hardlink_or_copy(src, dst)

    assert exc_info.value.errno == errno.ENOSPC  # original error, not masked
    assert not dst.exists()  # partial destination removed so a retry is clean


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
    # S103 suppressed deliberately: the execute-only mask IS the scenario under
    # test, applied to a throwaway dir inside this test's private tmp_path.
    os.chmod(locked, 0o111)  # noqa: S103 -- execute-only: search yes, read no
    try:
        assert fs.delete_guard_refuses(os.fspath(target)) is False
        fs.delete(os.fspath(target))
        assert not target.exists()
    finally:
        os.chmod(locked, 0o755)  # noqa: S103 -- restore so pytest can clean tmp_path


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
