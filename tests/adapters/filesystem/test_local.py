"""LocalFileSystem tests — real disk operations confined to ``tmp_path``."""

from __future__ import annotations

import os
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

    def _refuse_link(_src: str, _dst: str) -> None:
        raise OSError("simulated cross-device link")

    monkeypatch.setattr(os, "link", _refuse_link)
    LocalFileSystem().hardlink_or_copy(src, dst)

    assert dst.read_text() == "payload"
    assert src.stat().st_ino != dst.stat().st_ino  # a copy, not a link


def test_adapter_satisfies_filesystem_port() -> None:
    from plex_manager.ports.filesystem import FileSystemPort

    assert isinstance(LocalFileSystem(), FileSystemPort)
