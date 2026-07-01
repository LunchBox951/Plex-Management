"""LocalFileSystem tests — real disk operations confined to ``tmp_path``."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from plex_manager.adapters.filesystem import LocalFileSystem


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


def test_hardlink_or_copy_creates_linked_copy(tmp_path: Path) -> None:
    src = tmp_path / "src.mkv"
    src.write_text("payload")
    dst = tmp_path / "linked" / "dst.mkv"

    LocalFileSystem().hardlink_or_copy(src, dst)

    assert src.exists()  # source preserved
    assert dst.read_text() == "payload"
    # On the same device this is a true hardlink: same inode.
    assert src.stat().st_ino == dst.stat().st_ino


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
