"""Tests for the torrent manifest payload safety gate."""

from __future__ import annotations

import pytest

from plex_manager.domain.download_payload import (
    PayloadRejectionReason,
    format_payload_rejection,
    validate_payload_files,
)
from plex_manager.ports.download_client import DownloadedFile


def _file(name: str, *, size_bytes: int = 1024) -> DownloadedFile:
    return DownloadedFile(name=name, size_bytes=size_bytes)


def test_allows_video_files_and_subtitle_sidecars() -> None:
    result = validate_payload_files(
        [
            _file("Movie.2020.1080p/Movie.2020.1080p.MKV"),
            _file("Movie.2020.1080p/Movie.2020.1080p.m2ts"),
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


@pytest.mark.parametrize("name", ["setup.ts", "postinstall.TS", "movie.mkv.ts"])
@pytest.mark.parametrize(
    "size_bytes",
    [
        0,
        (50 * 1024 * 1024) - 1,
        50 * 1024 * 1024,
        (50 * 1024 * 1024) + 1,
        10 * 1024 * 1024 * 1024,
    ],
)
def test_rejects_ambiguous_typescript_payloads_at_any_size(name: str, size_bytes: int) -> None:
    result = validate_payload_files(
        [
            _file("Movie.2020.1080p/movie.mkv", size_bytes=4 * 1024 * 1024 * 1024),
            _file(f"Movie.2020.1080p/{name}", size_bytes=size_bytes),
        ]
    )

    assert result.accepted is False
    assert len(result.rejections) == 1
    assert result.rejections[0].name.endswith(name)
    assert result.rejections[0].reason is PayloadRejectionReason.UNSUPPORTED_EXTENSION
    assert result.rejections[0].detail == "unsupported file type .ts"


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
        "Movie/.. /movie.mkv",
        "Movie/. /movie.mkv",
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
        "Movie/movie\u2028name.mkv",
        "Movie/movie\u2029name.mkv",
        "Movie/movie\ud800name.mkv",
        "Movie/trailing./movie.mkv",
        "Movie/movie.mkv:payload.exe",
        "Movie/NUL.mkv",
        "Movie/NUL .mkv",
        "Movie/com1.MKV",
        "Movie/CONIN$.mkv",
        "Movie/CONOUT$.mkv",
        "Movie/COM¹.mkv",
        "Movie/COM².mkv",
        "Movie/LPT³.mkv",
        "Movie/movie?.mkv",
        "Movie/movie*.mkv",
        'Movie/movie".mkv',
        "Movie/movie<.mkv",
        "Movie/movie>.mkv",
        "Movie/movie|.mkv",
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

    long_extension = "Movie.2020.1080p/file." + ("x" * 10_000)
    extension_result = validate_payload_files([_file(long_extension)])
    assert len(format_payload_rejection(extension_result)) < 600

    separator_result = validate_payload_files([_file("Movie/bad\u2028name.exe")])
    separator_message = format_payload_rejection(separator_result)
    assert "\u2028" not in separator_message
    assert "bad?name.exe" in separator_message
    assert format_payload_rejection(long_result).endswith("...)")
