"""Tests for the import-validation gate with a fake parser (no guessit).

Mirrors ``test_decision_engine.py``: a recorded title -> guessit-field mapping
keeps the suite guessit-free while still exercising the real source_mapping ->
resolve_quality -> check_quality path and the real ``matches_media`` identity
gate against the real ``default_profile``.
"""

from __future__ import annotations

from plex_manager.domain.import_validation import (
    ImportRejectionReason,
    ImportValidation,
    VideoFile,
    validate_import,
)
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.release import ParsedRelease
from plex_manager.domain.source_mapping import to_parsed_release

# basename -> recorded guessit-style field mapping (keeps the test guessit-free).
_FIELDS: dict[str, dict[str, object]] = {
    "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv": {
        "title": "The Matrix",
        "year": "1999",
        "source": "Web",
        "screen_size": "1080p",
    },
    "The.Matrix.1999.1080p.WEBRip.x264-GRP.mkv": {
        "title": "The Matrix",
        "year": "1999",
        "source": "Web",
        "screen_size": "1080p",
        "other": "Rip",
    },
    "The.Matrix.1999.HDCAM.x264-GRP.mkv": {
        "title": "The Matrix",
        "year": "1999",
    },
    "Some.Other.Movie.2010.1080p.WEB-DL.x264-GRP.mkv": {
        "title": "Some Other Movie",
        "year": "2010",
        "source": "Web",
        "screen_size": "1080p",
    },
    "The.Matrix.1999.1080p.BluRay.x264-CD1.mkv": {
        "title": "The Matrix",
        "year": "1999",
        "source": "Blu-ray",
        "screen_size": "1080p",
    },
    "sample.mkv": {"title": "sample"},
}


class FakeParser:
    """A ParserPort that maps known basenames through the real source_mapping."""

    def parse(self, release_name: str) -> ParsedRelease:
        fields = _FIELDS.get(release_name, {})
        return to_parsed_release(fields, release_name)


_GIB = 1024 * 1024 * 1024


def _validate(*files: VideoFile) -> ImportValidation:
    return validate_import(
        list(files),
        parser=FakeParser(),
        profile=default_profile(),
        expected_title="The Matrix",
        expected_year=1999,
        expected_tmdb_id=603,
    )


def test_clean_matching_1080p_webdl_accepts() -> None:
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv", 8 * _GIB),
    )
    assert result.accepted is True
    assert result.rejections == ()
    assert result.video is not None
    assert result.parsed is not None
    assert result.parsed.clean_title == "The Matrix"


def test_benign_source_drift_webrip_still_accepts() -> None:
    # The grab advertised WEB-DL; the finished file parses as WEBRip. The gate
    # keys on PROFILE-ALLOWED, not equal-to-grabbed, so honest variance imports.
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.WEBRip.x264-GRP.mkv", 6 * _GIB),
    )
    assert result.accepted is True
    assert result.rejections == ()


def test_cam_rejects_quality_not_wanted() -> None:
    result = _validate(
        VideoFile("The.Matrix.1999.HDCAM.x264-GRP.mkv", 4 * _GIB),
    )
    assert result.accepted is False
    reasons = {r.reason for r in result.rejections}
    assert ImportRejectionReason.QUALITY_NOT_WANTED in reasons


def test_wrong_title_rejects_wrong_media() -> None:
    result = _validate(
        VideoFile("Some.Other.Movie.2010.1080p.WEB-DL.x264-GRP.mkv", 7 * _GIB),
    )
    assert result.accepted is False
    reasons = {r.reason for r in result.rejections}
    assert ImportRejectionReason.WRONG_MEDIA in reasons


def test_wrong_year_rejects_wrong_media() -> None:
    # Right title, year far outside the +/-1 tolerance -> uncertain -> reject.
    result = validate_import(
        [VideoFile("The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv", 7 * _GIB)],
        parser=FakeParser(),
        profile=default_profile(),
        expected_title="The Matrix",
        expected_year=2020,
        expected_tmdb_id=603,
    )
    assert result.accepted is False
    reasons = {r.reason for r in result.rejections}
    assert ImportRejectionReason.WRONG_MEDIA in reasons


def test_tiny_file_rejects_sample() -> None:
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv", 30 * 1024 * 1024),
    )
    assert result.accepted is False
    reasons = {r.reason for r in result.rejections}
    assert ImportRejectionReason.SAMPLE in reasons


def test_unknown_size_rejects_sample() -> None:
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv", 0),
    )
    assert result.accepted is False
    reasons = {r.reason for r in result.rejections}
    assert ImportRejectionReason.SAMPLE in reasons


def test_no_video_file_rejects() -> None:
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.WEB-DL.x264-GRP.nfo", 1024),
        VideoFile("poster.jpg", 2048),
    )
    assert result.accepted is False
    assert result.video is None
    assert result.parsed is None
    assert [r.reason for r in result.rejections] == [ImportRejectionReason.NO_VIDEO_FILE]


def test_only_named_sample_video_rejects_no_video_file() -> None:
    # A lone "sample.mkv" is dropped by name, leaving nothing importable.
    result = _validate(VideoFile("sample.mkv", 40 * 1024 * 1024))
    assert result.accepted is False
    assert result.video is None
    assert [r.reason for r in result.rejections] == [ImportRejectionReason.NO_VIDEO_FILE]


def test_named_sample_not_chosen_over_real_feature() -> None:
    # The named sample would be irrelevant here (smaller), but the point is it is
    # never even a candidate; the real feature is chosen and accepted.
    result = _validate(
        VideoFile("sample.mkv", 40 * 1024 * 1024),
        VideoFile("The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv", 8 * _GIB),
    )
    assert result.accepted is True
    assert result.video is not None
    assert result.video.relative_path == "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"


def test_multi_part_rejects() -> None:
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.BluRay.x264-CD1.mkv", 4 * _GIB),
    )
    assert result.accepted is False
    reasons = {r.reason for r in result.rejections}
    assert ImportRejectionReason.MULTI_PART in reasons


def test_all_applicable_rejections_collected() -> None:
    # A tiny CAM of the wrong movie surfaces every applicable reason at once,
    # not just the first — honesty over silence.
    result = validate_import(
        [VideoFile("Some.Other.Movie.2010.1080p.WEB-DL.x264-GRP.mkv", 10 * 1024 * 1024)],
        parser=FakeParser(),
        profile=default_profile(),
        expected_title="The Matrix",
        expected_year=1999,
        expected_tmdb_id=603,
    )
    assert result.accepted is False
    reasons = {r.reason for r in result.rejections}
    assert ImportRejectionReason.WRONG_MEDIA in reasons
    assert ImportRejectionReason.SAMPLE in reasons


def test_largest_video_is_chosen_as_feature() -> None:
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv", 8 * _GIB),
        VideoFile("Some.Other.Movie.2010.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
    )
    assert result.video is not None
    assert result.video.relative_path == "The.Matrix.1999.1080p.WEB-DL.x264-GRP.mkv"
    assert result.accepted is True
