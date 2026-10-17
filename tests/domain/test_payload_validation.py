"""Torrent manifests must contain video and no known executable types."""

import pytest

from plex_manager.domain.payload_validation import inspect_payload


@pytest.mark.parametrize("name", ["movie.EXE", "extras/setup.ps1", "movie.mkv.exe"])
def test_executable_sibling_rejects_whole_release(name: str) -> None:
    result = inspect_payload(["movie.mkv", name])
    assert result.reason is not None
    assert name in result.reason
    assert result.video_indices == ()


def test_video_sidecars_are_skipped_without_rejecting_release() -> None:
    result = inspect_payload(["movie.MKV", "poster.jpg", "subs.srt", "readme", "extra.zip"])
    assert result.reason is None
    assert result.video_indices == (0,)


@pytest.mark.parametrize("names", [["readme.txt"], ["BDMV/STREAM/movie.m2ts"], ["movie.iso"]])
def test_non_video_payload_is_rejected(names: list[str]) -> None:
    result = inspect_payload(names)
    assert result.reason == "release payload contains no Plex-supported video file"


def test_application_bundle_is_dangerous_even_with_video_inside() -> None:
    assert inspect_payload(["setup.app/Contents/movie.mkv"]).reason is not None
