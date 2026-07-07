"""``remap_to_visible`` -- host-namespace path -> container-visible path
(issues #131/#132/#133). Table-driven where natural, over real ``tmp_path`` dirs
so the ``predicate`` probes are genuine, not mocked.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from plex_manager.services import path_visibility
from plex_manager.services.path_visibility import (
    remap_download_content,
    remap_library_root,
    remap_to_visible,
)


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


# --------------------------------------------------------------------------- #
# allow_mount_root — a HOST bind-SOURCE root maps to the container mount ROOT
# --------------------------------------------------------------------------- #
def test_allow_mount_root_maps_the_bind_source_root(tmp_path: Path) -> None:
    # docker-compose ``PLEX_MANAGER_MEDIA_ROOT=/srv/media`` -> ``/media``, with Plex
    # reporting the WHOLE media root as one library at ``/srv/media``: no trailing
    # component below the mount, so only the mount root itself can be the answer.
    mount = tmp_path / "media"
    mount.mkdir()
    host_root = "/definitely-not-a-real-host-path/media"  # final name matches the mount
    assert remap_to_visible(host_root, [str(mount)], allow_mount_root=True) == str(mount)


def test_mount_root_is_off_by_default(tmp_path: Path) -> None:
    # Without allow_mount_root the zero-suffix case is never tried -- the bind-root
    # path is honestly unresolved (the pre-fix behaviour, kept for content remaps).
    mount = tmp_path / "media"
    mount.mkdir()
    assert remap_to_visible("/x/media", [str(mount)]) is None


def test_allow_mount_root_rejects_a_differently_named_root(tmp_path: Path) -> None:
    # The mount root ALWAYS exists, so an unconstrained fallback would collapse
    # every unresolved path onto it. A path whose final name differs from the
    # mount's stays an honest None (the operator must fix their mounts / pick again).
    mount = tmp_path / "media"
    mount.mkdir()
    assert remap_to_visible("/host/tank/library", [str(mount)], allow_mount_root=True) is None
    assert remap_to_visible("/host/typo", [str(mount)], allow_mount_root=True) is None


def test_a_deeper_suffix_always_beats_the_mount_root(tmp_path: Path) -> None:
    # A real subdirectory match must win over the zero-suffix mount-root fallback,
    # even when the path's final component equals the mount's own name.
    mount = tmp_path / "media"
    (mount / "media").mkdir(parents=True)
    assert remap_to_visible("/host/media", [str(mount)], allow_mount_root=True) == str(
        mount / "media"
    )


# --------------------------------------------------------------------------- #
# remap_download_content — ANCHORED on save_path, never a shorter-suffix guess
# --------------------------------------------------------------------------- #
def test_download_content_returns_verbatim_when_visible(tmp_path: Path) -> None:
    video = tmp_path / "dl" / "movies" / "Foo.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    # Already visible: returned unchanged (save_path irrelevant on the fast path).
    assert remap_download_content(str(video), str(video.parent)) == str(video)


def test_download_content_anchors_under_the_remapped_save_dir(tmp_path: Path) -> None:
    mount = tmp_path / "dl"
    video = mount / "movies" / "Foo.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    # HOST save_path ``/host/qbt/movies`` -> ``<mount>/movies``; the file's position
    # under it (``Foo.mkv``) is preserved verbatim.
    assert remap_download_content(
        "/host/qbt/movies/Foo.mkv", "/host/qbt/movies", candidate_mounts=(str(mount),)
    ) == str(video)


def test_download_content_never_matches_a_stale_shorter_suffix(tmp_path: Path) -> None:
    # THE finding: the real file ``<mount>/movies/Foo.mkv`` is MISSING (only its
    # category dir exists), and a stale, unrelated ``<mount>/Foo.mkv`` sits at the
    # mount root. A free suffix search would shorten to ``Foo.mkv`` and match the
    # stale file; the anchored remap must return None (honest block) instead.
    mount = tmp_path / "dl"
    (mount / "movies").mkdir(parents=True)
    (mount / "Foo.mkv").write_bytes(b"stale")
    assert (
        remap_download_content(
            "/host/qbt/movies/Foo.mkv", "/host/qbt/movies", candidate_mounts=(str(mount),)
        )
        is None
    )


def test_download_content_maps_the_save_path_bind_root(tmp_path: Path) -> None:
    # save_path IS the download bind-source root (``/host/qbt`` -> ``<mount>``): no
    # suffix of it is a dir under the mount, so the mount root is used and the FULL
    # remainder (``Foo/Foo.mkv``) is anchored under it.
    mount = tmp_path / "dl"
    video = mount / "Foo" / "Foo.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    assert remap_download_content(
        "/host/qbt/Foo/Foo.mkv", "/host/qbt", candidate_mounts=(str(mount),)
    ) == str(video)


def test_download_content_without_a_save_path_anchor_is_verbatim_only(tmp_path: Path) -> None:
    # No save_path (a stored crash-resume breadcrumb): only the verbatim path counts
    # -- a free suffix search is deliberately NOT attempted (it would reintroduce
    # the stale-match hazard). A missing path stays an honest None.
    mount = tmp_path / "dl"
    mount.mkdir()
    (mount / "Foo.mkv").write_bytes(b"stale")
    assert remap_download_content("/host/qbt/Foo.mkv", None, candidate_mounts=(str(mount),)) is None
    real = tmp_path / "already" / "Foo.mkv"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"x")
    assert remap_download_content(str(real), None, candidate_mounts=(str(mount),)) == str(real)


def test_download_content_refuses_a_path_not_under_save_path(tmp_path: Path) -> None:
    mount = tmp_path / "dl"
    mount.mkdir()
    (mount / "Foo.mkv").write_bytes(b"x")
    # content escapes save_path via ``..`` -> no honest anchor -> None (never the
    # bare mount tree, never a sibling).
    assert (
        remap_download_content(
            "/host/qbt/../other/Foo.mkv", "/host/qbt", candidate_mounts=(str(mount),)
        )
        is None
    )


def test_download_content_reads_module_mounts_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # candidate_mounts=None resolves KNOWN_DOWNLOAD_MOUNTS at CALL time, so a
    # monkeypatch takes effect (the import-service call site relies on this).
    mount = tmp_path / "dl"
    video = mount / "movies" / "Foo.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(mount),))
    assert remap_download_content("/host/qbt/movies/Foo.mkv", "/host/qbt/movies") == str(video)


def test_remap_library_root_uses_library_mounts_and_the_mount_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shared library-root policy: LIBRARY mounts only, mount-root allowed.
    library_mount = tmp_path / "media"
    library_mount.mkdir()
    download_mount = tmp_path / "downloads"
    (download_mount / "media").mkdir(parents=True)  # a same-named tree under downloads
    monkeypatch.setattr(path_visibility, "KNOWN_LIBRARY_MOUNTS", (str(library_mount),))
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (str(download_mount),))
    # Resolves to the LIBRARY mount root, never the same-named subtree of downloads.
    assert remap_library_root("/host/media") == str(library_mount)
