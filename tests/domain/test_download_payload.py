"""Tests for the torrent manifest payload safety gate."""

from __future__ import annotations

import pytest

from plex_manager.domain.download_payload import (
    PayloadRejectionReason,
    format_payload_rejection,
    validate_payload_files,
)
from plex_manager.ports.download_client import DownloadedFile


def _file(name: str) -> DownloadedFile:
    return DownloadedFile(name=name, size_bytes=1024)


def test_allows_video_files_and_subtitle_sidecars() -> None:
    result = validate_payload_files(
        [
            _file("Movie.2020.1080p/Movie.2020.1080p.MKV"),
            _file("Movie.2020.1080p/Subs/English.SRT"),
            _file("Movie.2020.1080p/Subs/Commentary.ass"),
            _file("Movie.2020.1080p/Subs/English.idx"),
            _file("Movie.2020.1080p/Subs/English.sub"),
            _file("Movie.2020.1080p/Subs/English.sup"),
            _file("Movie.2020.1080p/Subs/English.vtt"),
            _file("Movie.2020.1080p/Subs/English.smi"),
        ]
    )

    assert result.accepted is True
    assert result.rejections == ()


@pytest.mark.parametrize(
    "name",
    [
        "Movie.2020.1080p/readme.txt",
        "Movie.2020.1080p/movie.nfo",
        "Movie.2020.1080p/poster.jpg",
        "Movie.2020.1080p/archive.zip",
        "Movie.2020.1080p/setup.exe",
        "Movie.2020.1080p/install.sh",
        "Movie.2020.1080p/movie.mkv.exe",
        "Movie.2020.1080p/movie",
    ],
)
def test_rejects_any_non_video_or_subtitle_payload(name: str) -> None:
    result = validate_payload_files([_file("Movie.2020.1080p/movie.mkv"), _file(name)])

    assert result.accepted is False
    assert len(result.rejections) == 1
    assert result.rejections[0].name == name
    assert result.rejections[0].reason is PayloadRejectionReason.UNSUPPORTED_EXTENSION


@pytest.mark.parametrize(
    "name",
    [
        "../movie.mkv",
        "Movie/../movie.mkv",
        "/downloads/movie.mkv",
        "C:/Downloads/movie.mkv",
        "C:\\Downloads\\movie.mkv",
        "C:Downloads\\movie.mkv",
        "\\\\server\\share\\movie.mkv",
        "Movie//movie.mkv",
        "Movie/./movie.mkv",
        "Movie/movie\nname.mkv",
        "Movie/movie\rname.mkv",
        "Movie/movie\tname.mkv",
        "Movie/movie\u202ename.mkv",
        "",
        "Movie\x00Name/movie.mkv",
    ],
)
def test_rejects_unsafe_manifest_paths(name: str) -> None:
    result = validate_payload_files([_file(name)])

    assert result.accepted is False
    assert len(result.rejections) == 1
    assert result.rejections[0].name == name
    assert result.rejections[0].reason is PayloadRejectionReason.UNSAFE_PATH


def test_failed_reason_uses_first_rejection() -> None:
    result = validate_payload_files(
        [
            _file("Movie.2020.1080p/setup.exe"),
            _file("Movie.2020.1080p/readme.txt"),
        ]
    )

    assert format_payload_rejection(result) == (
        "torrent payload rejected: unsupported file type .exe (Movie.2020.1080p/setup.exe)"
    )


def test_failed_reason_sanitizes_and_truncates_manifest_name() -> None:
    result = validate_payload_files([_file("Movie.2020.1080p/bad\nname.exe")])

    message = format_payload_rejection(result)

    assert "\n" not in message
    assert "bad?name.exe" in message

    long_name = "Movie.2020.1080p/" + ("a" * 300) + ".exe"
    long_result = validate_payload_files([_file(long_name)])

    assert len(format_payload_rejection(long_result)) < 260
    assert format_payload_rejection(long_result).endswith("...)")
