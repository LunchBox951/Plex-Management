"""``remap_to_visible`` -- host-namespace path -> container-visible path
(issues #131/#132/#133). Table-driven where natural, over real ``tmp_path`` dirs
so the ``predicate`` probes are genuine, not mocked.
"""

from __future__ import annotations

import os
from pathlib import Path

from plex_manager.services.path_visibility import remap_to_visible


def test_returns_original_when_already_visible(tmp_path: Path) -> None:
    # Already a real dir: returned UNCHANGED (the exact operator-supplied string).
    assert remap_to_visible(str(tmp_path), [str(tmp_path / "elsewhere")]) == str(tmp_path)


def test_suffix_matches_under_a_mount(tmp_path: Path) -> None:
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    # A HOST-namespace path (mirrors docker-compose's host /home/Media -> /media)
    # that must not itself exist on the machine running this test.
    host_path = "/definitely-not-a-real-host-path/Media/Movies"
    assert remap_to_visible(host_path, [str(mount)]) == str(mount / "Movies")


def test_prefers_longest_matching_suffix(tmp_path: Path) -> None:
    mount = tmp_path / "media"
    (mount / "a" / "b").mkdir(parents=True)
    (mount / "b").mkdir(parents=True)
    assert remap_to_visible("/x/a/b", [str(mount)]) == str(mount / "a" / "b")


def test_returns_none_when_no_suffix_exists(tmp_path: Path) -> None:
    mount = tmp_path / "media"
    mount.mkdir()
    assert remap_to_visible("/host/Movies/does/not/exist", [str(mount)]) is None


def test_empty_path_and_blank_mount_return_none(tmp_path: Path) -> None:
    assert remap_to_visible(None, [str(tmp_path)]) is None
    assert remap_to_visible("", [str(tmp_path)]) is None
    assert remap_to_visible("/host/Movies", [""]) is None


def test_probe_original_false_skips_raw_path(tmp_path: Path) -> None:
    # tmp_path IS a real dir, but its own suffix ("tmp_path"'s basename) has no
    # match under the mount -- probe_original=False must never short-circuit on
    # the raw (already-visible) path, proving the raw path itself was never probed.
    mount = tmp_path / "media"
    mount.mkdir()
    assert remap_to_visible(str(tmp_path), [str(mount)], probe_original=False) is None


def test_never_escapes_mount_via_dotdot(tmp_path: Path) -> None:
    mount = tmp_path / "media"
    mount.mkdir()
    # A crafted "../../etc/passwd"-shaped suffix must never lexically climb back
    # out of the mount -- ".."/"." segments are dropped structurally, so the
    # result (if any) always stays a descendant of ``mount``.
    result = remap_to_visible("/a/../../etc/passwd", [str(mount)])
    assert result is None or result.startswith(str(mount) + os.sep)


def test_predicate_exists_matches_a_file(tmp_path: Path) -> None:
    mount = tmp_path / "downloads"
    video = mount / "rel" / "movie.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    assert remap_to_visible("/host/rel/movie.mkv", [str(mount)], predicate=os.path.exists) == str(
        video
    )


def test_normalizes_trailing_slash_and_dot_segments(tmp_path: Path) -> None:
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    assert remap_to_visible("/x/./Movies/", [str(mount)]) == str(mount / "Movies")


def test_ties_break_by_mount_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    (first / "Movies").mkdir(parents=True)
    (second / "Movies").mkdir(parents=True)
    assert remap_to_visible("/host/Movies", [str(first), str(second)]) == str(first / "Movies")
