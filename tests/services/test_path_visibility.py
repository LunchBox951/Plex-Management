"""``remap_to_visible`` -- host-namespace path -> container-visible path
(issues #131/#132/#133). Table-driven where natural, over real ``tmp_path`` dirs
so the ``predicate`` probes are genuine, not mocked.

Most tests use plain ``tmp_path`` directories as stand-in mounts, so the autouse
fixture below relaxes :func:`path_visibility.is_live_mount` to ``os.path.isdir``;
the "live mount gate" section restores the REAL gate to prove a stock distro's
plain ``/media``-like directory never counts as a mount.
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
    resolve_downloads_host_root,
)

# Captured at import time, BEFORE the autouse fixture patches the module attr, so
# the live-mount-gate tests below can exercise the real predicate.
_REAL_IS_LIVE_MOUNT = path_visibility.is_live_mount


@pytest.fixture(autouse=True)
def tmp_dirs_count_as_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let plain tmp_path directories stand in as the container mounts.

    Production gates every KNOWN mount on ``is_live_mount`` (isdir AND ismount) so
    a stock distro's plain ``/media`` directory never counts (the tests-py314 CI
    regression: Ubuntu runners HAVE a plain ``/media``, the Arch dev box doesn't,
    so behaviour differed by host). ``tmp_path`` fixtures are ordinary dirs, never
    mount points, so these unit tests relax the gate to ``isdir`` -- the documented
    test seam -- and the dedicated live-mount-gate tests restore the real one.
    """
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)


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
# under-mount short-circuit + phantom preference (round 3)
# --------------------------------------------------------------------------- #
def test_under_mount_path_is_never_suffix_probed_deeper(tmp_path: Path) -> None:
    # An already-container-visible /media/Movies must be kept as-is even when a
    # nested twin /media/media/Movies exists (the longest-suffix-first search
    # would otherwise prefer it) -- and even pre-init (probe_original=False):
    # probing our OWN mounts is never a remote-server oracle.
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    (mount / "media" / "Movies").mkdir(parents=True)  # the nesting trap
    target = str(mount / "Movies")
    assert remap_to_visible(target, [str(mount)]) == target
    assert remap_to_visible(target, [str(mount)], probe_original=False) == target


def test_under_mount_path_that_does_not_exist_is_none(tmp_path: Path) -> None:
    # Under our mount but nonexistent: an honest None (the write gate 422s),
    # never a deeper suffix guess.
    mount = tmp_path / "media"
    (mount / "media" / "Movies").mkdir(parents=True)  # trap only, no real target
    assert remap_to_visible(str(mount / "Movies"), [str(mount)]) is None


def test_prefers_the_mounted_twin_over_an_outside_mount_phantom(tmp_path: Path) -> None:
    # Round-3 finding: a pre-fix PHANTOM (e.g. /home/Media/Movies the old importer
    # os.makedirs-ed inside this container) exists, but the same suffix also
    # resolves under the live mount: the MOUNTED candidate wins, so settings/
    # setup remaps land where Plex can actually see the files.
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    phantom = tmp_path / "phantom" / "Media" / "Movies"
    phantom.mkdir(parents=True)
    assert remap_to_visible(str(phantom), [str(mount)]) == str(mount / "Movies")


def test_outside_mount_original_kept_when_no_mounted_twin(tmp_path: Path) -> None:
    # Visible, outside the mounts, and nothing under a mount matches: honestly
    # accepted as-is (an operator's legitimate EXTRA volume at a custom path).
    mount = tmp_path / "media"
    mount.mkdir()
    extra = tmp_path / "extra" / "Anime"
    extra.mkdir(parents=True)
    assert remap_to_visible(str(extra), [str(mount)]) == str(extra)


def test_original_kept_when_no_live_mounts(tmp_path: Path) -> None:
    # Bare metal (zero live mounts): the visible original is the truth, as-is.
    real = tmp_path / "Movies"
    real.mkdir()
    assert remap_to_visible(str(real), [str(tmp_path / "nonexistent-mount")]) == str(real)


# --------------------------------------------------------------------------- #
# remap_download_content — anchored on save_path AND proven by the torrent's
# own file list (relative path + exact byte size); never an existence-only guess
# --------------------------------------------------------------------------- #
def test_download_content_returns_verbatim_when_visible(tmp_path: Path) -> None:
    video = tmp_path / "dl" / "movies" / "Foo.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    # Already visible: returned unchanged. No remap happened, so no proof is
    # needed -- an empty file list must not block the same-namespace fast path.
    assert remap_download_content(str(video), str(video.parent), []) == str(video)


def test_download_content_anchors_under_the_remapped_save_dir(tmp_path: Path) -> None:
    mount = tmp_path / "dl"
    video = mount / "movies" / "Foo.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    # HOST save_path ``/host/qbt/movies`` -> ``<mount>/movies``; the file's position
    # under it (``Foo.mkv``) is preserved verbatim and PROVEN by name + exact size.
    assert remap_download_content(
        "/host/qbt/movies/Foo.mkv",
        "/host/qbt/movies",
        [("Foo.mkv", 1)],
        candidate_mounts=(str(mount),),
    ) == str(video)


def test_download_content_never_matches_a_stale_shorter_suffix(tmp_path: Path) -> None:
    # Round-2 finding, now stronger: the real ``<mount>/movies/Foo.mkv`` is
    # MISSING (only its category dir exists) while a stale ``<mount>/Foo.mkv``
    # sits at the mount root with the SAME name and even the SAME size. The
    # category-directory interpretation wins (deepest existing save-dir suffix)
    # and its failed proof is FINAL -- no fallthrough to the bind-root guess, so
    # the stale file can never shadow the genuinely-missing real one.
    mount = tmp_path / "dl"
    (mount / "movies").mkdir(parents=True)
    (mount / "Foo.mkv").write_bytes(b"x")  # same 1-byte size as the torrent's file
    assert (
        remap_download_content(
            "/host/qbt/movies/Foo.mkv",
            "/host/qbt/movies",
            [("Foo.mkv", 1)],
            candidate_mounts=(str(mount),),
        )
        is None
    )


def test_download_content_maps_the_save_path_bind_root_with_proof(tmp_path: Path) -> None:
    # save_path IS the download bind-source root (``/host/qbt`` -> ``<mount>``,
    # the live deployment's shape): no suffix of it is a real dir under the
    # mount, so the bind-root interpretation applies -- accepted ONLY because the
    # torrent's own file is exhibited at its exact relative location with its
    # exact size, never on mere existence.
    mount = tmp_path / "dl"
    video = mount / "Foo" / "Foo.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    assert remap_download_content(
        "/host/qbt/Foo/Foo.mkv",
        "/host/qbt",
        [("Foo/Foo.mkv", 1)],
        candidate_mounts=(str(mount),),
    ) == str(video)


def test_download_content_bind_root_rejects_a_same_name_wrong_size_stale(
    tmp_path: Path,
) -> None:
    # Round-3 finding: the bind-root interpretation must PROVE itself. A
    # same-named file with a DIFFERENT size at the expected location is an
    # immediate disproof (a stale/unrelated tree) -> honest None.
    mount = tmp_path / "dl"
    mount.mkdir()
    (mount / "Foo.mkv").write_bytes(b"stale")  # 5 bytes; the torrent's file is 1
    assert (
        remap_download_content(
            "/host/qbt/Foo.mkv",
            "/host/qbt",
            [("Foo.mkv", 1)],
            candidate_mounts=(str(mount),),
        )
        is None
    )


def test_download_content_requires_a_witness_not_just_existence(tmp_path: Path) -> None:
    # No expected file materializes at the candidate location -> not proven ->
    # None, even though the mount root itself exists; and an EMPTY file list
    # (the client reported nothing) can never prove anything either.
    mount = tmp_path / "dl"
    mount.mkdir()
    assert (
        remap_download_content(
            "/host/qbt/Foo.mkv", "/host/qbt", [("Foo.mkv", 1)], candidate_mounts=(str(mount),)
        )
        is None
    )
    (mount / "Foo.mkv").write_bytes(b"x")
    assert (
        remap_download_content("/host/qbt/Foo.mkv", "/host/qbt", [], candidate_mounts=(str(mount),))
        is None
    )


def test_download_content_deselected_files_are_neutral(tmp_path: Path) -> None:
    # A multi-file pack with a deselected (never-downloaded) file: its absence is
    # neutral -- the present file's exact name+size still proves the
    # interpretation, and the content DIRECTORY resolves.
    mount = tmp_path / "dl"
    release = mount / "Release"
    release.mkdir(parents=True)
    (release / "main.mkv").write_bytes(b"xx")
    expected = [("Release/main.mkv", 2), ("Release/extra.mkv", 7)]  # extra absent
    assert remap_download_content(
        "/host/qbt/Release", "/host/qbt", expected, candidate_mounts=(str(mount),)
    ) == str(release)


def test_download_content_without_a_save_path_anchor_is_verbatim_only(tmp_path: Path) -> None:
    # No save_path (a stored crash-resume breadcrumb): only the verbatim path counts
    # -- a free suffix search is deliberately NOT attempted (it would reintroduce
    # the stale-match hazard). A missing path stays an honest None.
    mount = tmp_path / "dl"
    mount.mkdir()
    (mount / "Foo.mkv").write_bytes(b"x")
    assert (
        remap_download_content(
            "/host/qbt/Foo.mkv", None, [("Foo.mkv", 1)], candidate_mounts=(str(mount),)
        )
        is None
    )
    real = tmp_path / "already" / "Foo.mkv"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"x")
    assert remap_download_content(str(real), None, [], candidate_mounts=(str(mount),)) == str(real)


def test_download_content_refuses_a_path_not_under_save_path(tmp_path: Path) -> None:
    mount = tmp_path / "dl"
    mount.mkdir()
    (mount / "Foo.mkv").write_bytes(b"x")
    # content escapes save_path via ``..`` -> no honest anchor -> None (never the
    # bare mount tree, never a sibling), no matter what the file list says.
    assert (
        remap_download_content(
            "/host/qbt/../other/Foo.mkv",
            "/host/qbt",
            [("Foo.mkv", 1)],
            candidate_mounts=(str(mount),),
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
    assert remap_download_content(
        "/host/qbt/movies/Foo.mkv", "/host/qbt/movies", [("Foo.mkv", 1)]
    ) == str(video)


def test_download_content_prefers_mounted_over_outside_mount_phantom(tmp_path: Path) -> None:
    # Issue #290, finding #2: the HOST-namespace ``content`` coincidentally exists
    # in this container as a stale PHANTOM tree OUTSIDE the live mount, while the
    # REAL file sits under the mount. The bare-existence fast path would return the
    # phantom (watching/placing the wrong location); the mount-aware guard falls
    # through to the proof-gated remap so the REAL mounted path wins.
    mount = tmp_path / "dl"
    real = mount / "movies" / "Foo.mkv"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"x")
    phantom = tmp_path / "phantom" / "movies" / "Foo.mkv"  # outside the mount
    phantom.parent.mkdir(parents=True)
    phantom.write_bytes(b"x")
    assert remap_download_content(
        str(phantom),
        str(phantom.parent),  # save_path suffix "movies" remaps under the mount
        [("Foo.mkv", 1)],
        candidate_mounts=(str(mount),),
    ) == str(real)


def test_download_content_verbatim_when_outside_mount_but_no_mounted_candidate(
    tmp_path: Path,
) -> None:
    # The preserved legitimate case (operator's EXTRA volume at a custom path):
    # ``content`` exists OUTSIDE every live mount and NO mounted candidate can be
    # proven, so the verbatim existing path is still returned -- never a guess,
    # only a path that actually exists. Guards against over-tightening finding #2.
    mount = tmp_path / "dl"
    mount.mkdir()  # a live mount, but nothing under it matches
    extra = tmp_path / "extra_volume" / "Foo.mkv"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"x")
    assert remap_download_content(
        str(extra),
        str(extra.parent),
        [("Foo.mkv", 1)],
        candidate_mounts=(str(mount),),
    ) == str(extra)


def test_download_content_fails_closed_when_colliding_mounted_subdir_lacks_the_file(
    tmp_path: Path,
) -> None:
    # Finding #1 (the disclosed narrowing of the SHARED import path, pinned): the
    # legitimate operator EXTRA-volume case COLLIDES with a same-named mounted
    # category subdir. ``content`` exists OUTSIDE every live mount, but the
    # save_path's trailing component (``movies``) ALSO resolves to a real
    # ``<mount>/movies`` dir under the live mount -- one that does NOT hold this
    # torrent's file. The old unconditional verbatim fast path would import the
    # outside-mount file; now the mounted save-dir suffix resolves, its proof
    # FAILS (the file is absent there), and the function fails CLOSED to None (an
    # honest, retryable "not visible / content mismatch") rather than the verbatim
    # extra-volume path. This is intended: fail-CLOSED over an unproven guess, per
    # the north stars. Contrast test_..._verbatim_when_outside_mount_but_no_mounted
    # _candidate, where NO colliding subdir exists and the verbatim path IS kept.
    mount = tmp_path / "dl"
    (mount / "movies").mkdir(parents=True)  # colliding category dir, WITHOUT the file
    extra = tmp_path / "extra_volume" / "movies" / "Foo.mkv"  # the real file, outside
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"x")
    assert (
        remap_download_content(
            str(extra),
            str(extra.parent),  # ".../extra_volume/movies" -> suffix "movies" collides
            [("Foo.mkv", 1)],
            candidate_mounts=(str(mount),),
        )
        is None
    )
    # Sanity: with the colliding subdir ALSO holding the torrent's file, the same
    # inputs resolve to the MOUNTED file (the phantom-preference path), proving the
    # None above is the failed-proof block, not an unrelated mismatch.
    (mount / "movies" / "Foo.mkv").write_bytes(b"x")
    assert remap_download_content(
        str(extra),
        str(extra.parent),
        [("Foo.mkv", 1)],
        candidate_mounts=(str(mount),),
    ) == str(mount / "movies" / "Foo.mkv")


def test_content_is_mounted_distinguishes_mounted_from_phantom(tmp_path: Path) -> None:
    mount = tmp_path / "dl"
    mount.mkdir()
    mounts = (str(mount),)
    # Under a live mount -> trustworthy verbatim.
    assert path_visibility.content_is_mounted(str(mount / "movies" / "Foo.mkv"), mounts)
    # Outside every live mount -> a phantom the caller must remap instead.
    assert not path_visibility.content_is_mounted(str(tmp_path / "phantom" / "Foo.mkv"), mounts)
    # No live mount at all (bare-metal / no host-container split) -> verbatim stands.
    assert path_visibility.content_is_mounted(str(tmp_path / "anywhere" / "Foo.mkv"), ())


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


# --------------------------------------------------------------------------- #
# live mount gate — a plain directory NEVER counts as one of the app's mounts
# --------------------------------------------------------------------------- #
def test_is_live_mount_requires_a_real_mount_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(path_visibility, "is_live_mount", _REAL_IS_LIVE_MOUNT)
    # ``/`` is the one path guaranteed to be a mount point on every POSIX host.
    assert path_visibility.is_live_mount("/") is True
    assert path_visibility.is_live_mount(str(tmp_path)) is False  # plain dir
    assert path_visibility.is_live_mount(str(tmp_path / "missing")) is False


def test_a_plain_directory_never_counts_as_a_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CI regression (tests-py314): stock Ubuntu/Debian ship a plain ``/media``
    DIRECTORY on the root filesystem, so any mount gate based on bare ``isdir``
    made every remap/suggestion environment-dependent -- the local (Arch, no
    ``/media``) gates passed while CI failed, and a bare-metal Ubuntu install
    would have had unresolvable paths remapped onto a directory nothing is
    mounted at. With the REAL gate, a plain dir -- even with a matching subtree
    or a matching basename -- never participates in any remap."""
    monkeypatch.setattr(path_visibility, "is_live_mount", _REAL_IS_LIVE_MOUNT)
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    # Deep-suffix match: the matching subtree exists, but the "mount" is a plain dir.
    assert remap_to_visible("/host/Media/Movies", [str(mount)]) is None
    # Zero-suffix bind-root match: basename matches, still a plain dir.
    assert remap_to_visible("/srv/media", [str(mount)], allow_mount_root=True) is None
    # Content remap: both the anchored save-dir path and the bind-root fallback.
    video = mount / "movies" / "Foo.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    assert (
        remap_download_content(
            "/host/qbt/movies/Foo.mkv",
            "/host/qbt/movies",
            [("Foo.mkv", 1)],
            candidate_mounts=(str(mount),),
        )
        is None
    )
    assert (
        remap_download_content(
            "/host/qbt/movies/Foo.mkv",
            "/host/qbt",
            [("movies/Foo.mkv", 1)],
            candidate_mounts=(str(mount),),
        )
        is None
    )
    # An already-visible path is untouched by the gate (step 1 needs no mount).
    assert remap_to_visible(str(mount / "Movies"), [str(mount)]) == str(mount / "Movies")


# --------------------------------------------------------------------------- #
# resolve_downloads_host_root (issues #133/#157 -- deriving the HOST-namespace
# downloads root that directs qBittorrent's per-add ``save_path``).
#
# There is deliberately NO ``/proc/self/mountinfo`` fallback: mountinfo's
# ``root`` field is the path relative to the MOUNTED FILESYSTEM, not a
# host-namespace pathname (for a bind whose source is its own disk, ``root``
# is just ``/``), so it cannot recover the host path and a prior fallback
# built on it is gone. ``Settings.downloads_root`` /
# ``PLEX_MANAGER_DOWNLOADS_ROOT`` is the only source now.
# --------------------------------------------------------------------------- #
def test_resolve_downloads_host_root_prefers_configured() -> None:
    assert resolve_downloads_host_root("/configured/root") == "/configured/root"


def test_resolve_downloads_host_root_none_when_unconfigured() -> None:
    assert resolve_downloads_host_root(None) is None
    assert resolve_downloads_host_root("") is None
