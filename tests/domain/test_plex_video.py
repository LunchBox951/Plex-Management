"""Tests for the conservative Plex video-container policy."""

from __future__ import annotations

import pytest

from plex_manager.domain.plex_video import (
    PLEX_VIDEO_EXTENSIONS,
    PLEX_VIDEO_FORMATS,
    expected_probe_formats,
    plex_video_extension,
)

_EXPECTED_EXTENSIONS = frozenset(
    {
        ".mkv",
        ".mp4",
        ".m4v",
        ".avi",
        ".mov",
        ".divx",
        ".wmv",
        ".mpg",
        ".mpeg",
        ".ts",
        ".m2ts",
        ".mts",
        ".webm",
        ".flv",
        ".ogv",
    }
)


def test_supported_extensions_are_the_exact_conservative_policy() -> None:
    assert PLEX_VIDEO_EXTENSIONS == _EXPECTED_EXTENSIONS
    assert frozenset(PLEX_VIDEO_FORMATS) == _EXPECTED_EXTENSIONS
    assert ".vob" not in PLEX_VIDEO_EXTENSIONS


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("Movie (2026).MKV", ".mkv"),
        ("season/Show.S01E01.Mp4", ".mp4"),
        (r"season\Show.S01E01.M2TS", ".m2ts"),
        (r"C:\downloads\Feature.WeBm", ".webm"),
    ],
)
def test_plex_video_extension_is_case_and_separator_insensitive(path: str, expected: str) -> None:
    assert plex_video_extension(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "README",
        ".mkv",
        "movie.vob",
        "movie.exe",
        "movie.mkv.exe",
        "directory.mkv/movie",
    ],
)
def test_plex_video_extension_rejects_unsupported_or_missing_final_suffix(path: str) -> None:
    assert plex_video_extension(path) is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("movie.mkv", frozenset({"matroska", "webm"})),
        ("movie.webm", frozenset({"matroska", "webm"})),
        ("movie.mp4", frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})),
        ("movie.m4v", frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})),
        ("movie.mov", frozenset({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"})),
        ("movie.avi", frozenset({"avi"})),
        ("movie.divx", frozenset({"avi"})),
        ("movie.wmv", frozenset({"asf"})),
        ("movie.mpg", frozenset({"mpeg"})),
        ("movie.mpeg", frozenset({"mpeg"})),
        ("movie.ts", frozenset({"mpegts"})),
        ("movie.m2ts", frozenset({"mpegts"})),
        ("movie.mts", frozenset({"mpegts"})),
        ("movie.flv", frozenset({"flv"})),
        ("movie.ogv", frozenset({"ogg"})),
    ],
)
def test_expected_probe_formats_maps_suffix_to_ffprobe_family(
    path: str, expected: frozenset[str]
) -> None:
    assert expected_probe_formats(path) == expected


def test_expected_probe_formats_is_empty_for_an_unsupported_path() -> None:
    assert expected_probe_formats("movie.vob") == frozenset()
    assert expected_probe_formats("movie") == frozenset()
