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
    SeasonImportValidation,
    VideoFile,
    validate_import,
    validate_season_import,
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
    # A token-rich release FOLDER over a generic feature file: the validator must
    # parse the full relative path (folder included), so only the folder-qualified
    # key resolves to The Matrix; the bare "movie.mkv" is deliberately generic.
    "movie.mkv": {"title": "movie"},
    "The.Matrix.1999.1080p.WEB-DL/movie.mkv": {
        "title": "The Matrix",
        "year": "1999",
        "source": "Web",
        "screen_size": "1080p",
    },
    # A real movie genuinely titled "...Part 1": the file's "Part.1" token matches
    # the expected title's own part number, so it must NOT be flagged MULTI_PART.
    "Harry.Potter.and.the.Deathly.Hallows.Part.1.2010.1080p.mkv": {
        "title": "Harry Potter and the Deathly Hallows Part 1",
        "year": "2010",
        "source": "Web",
        "screen_size": "1080p",
    },
    # A genuine 2-CD split whose title carries NO part number: still MULTI_PART.
    "The.Matrix.1999.1080p.BluRay.Part1.mkv": {
        "title": "The Matrix",
        "year": "1999",
        "source": "Blu-ray",
        "screen_size": "1080p",
    },
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


def test_part_in_real_title_accepts_not_multi_part() -> None:
    # The requested movie is genuinely titled "...Part 1"; the file's "Part.1"
    # token matches the expected title's own part number, so it is NOT a split and
    # the full, correct movie imports instead of being permanently rejected.
    result = validate_import(
        [VideoFile("Harry.Potter.and.the.Deathly.Hallows.Part.1.2010.1080p.mkv", 8 * _GIB)],
        parser=FakeParser(),
        profile=default_profile(),
        expected_title="Harry Potter and the Deathly Hallows: Part 1",
        expected_year=2010,
        expected_tmdb_id=12444,
    )
    assert result.accepted is True
    assert result.rejections == ()
    assert result.parsed is not None
    assert ImportRejectionReason.MULTI_PART not in {r.reason for r in result.rejections}


def test_genuine_part_split_still_rejects_multi_part() -> None:
    # The requested title carries no part number, so a lone "Part1" slice IS a
    # split-disk set and must still be surfaced as MULTI_PART.
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.BluRay.Part1.mkv", 4 * _GIB),
    )
    assert result.accepted is False
    reasons = {r.reason for r in result.rejections}
    assert ImportRejectionReason.MULTI_PART in reasons


def test_generic_file_under_token_rich_folder_accepts() -> None:
    # The release folder carries the title/year/quality; the feature file is generic
    # ("movie.mkv"). Parsing the FULL relative path recovers the tokens, so a valid
    # download is accepted instead of rejected as wrong/unknown media.
    result = _validate(
        VideoFile("The.Matrix.1999.1080p.WEB-DL/movie.mkv", 8 * _GIB),
    )
    assert result.accepted is True
    assert result.rejections == ()
    assert result.parsed is not None
    assert result.parsed.clean_title == "The Matrix"


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


# -- validate_season_import: TV season-pack files, every file gated on its own --

# basename -> recorded guessit-style field mapping for a "Breaking Bad" S02 pack.
_TV_FIELDS: dict[str, dict[str, object]] = {
    "Breaking.Bad.S02E01.1080p.WEB-DL.x264-GRP.mkv": {
        "title": "Breaking Bad",
        "season": 2,
        "episode": 1,
        "source": "Web",
        "screen_size": "1080p",
    },
    "Breaking.Bad.S02E02.1080p.WEB-DL.x264-GRP.mkv": {
        "title": "Breaking Bad",
        "season": 2,
        "episode": 2,
        "source": "Web",
        "screen_size": "1080p",
    },
    # A CAM release of one episode in an otherwise-clean pack: right show, right
    # season, right episode -- rejected on quality alone.
    "Breaking.Bad.S02E03.HDCAM.x264-GRP.mkv": {
        "title": "Breaking Bad",
        "season": 2,
        "episode": 3,
    },
    # Mislabeled/bonus file inside the "Season 2" pack that is actually Season 1.
    "Breaking.Bad.S01E01.1080p.WEB-DL.x264-GRP.mkv": {
        "title": "Breaking Bad",
        "season": 1,
        "episode": 1,
        "source": "Web",
        "screen_size": "1080p",
    },
    # A genuine multi-episode file.
    "Breaking.Bad.S02E04E05.1080p.WEB-DL.x264-GRP.mkv": {
        "title": "Breaking Bad",
        "season": 2,
        "episode": [4, 5],
        "source": "Web",
        "screen_size": "1080p",
    },
    # No episode token at all (a bonus/extra clip that slipped past the sample-name
    # filter): right show, right season, but nothing to place as a named episode.
    "Breaking.Bad.S02.Bonus.Content.1080p.WEB-DL.x264-GRP.mkv": {
        "title": "Breaking Bad",
        "season": 2,
        "source": "Web",
        "screen_size": "1080p",
    },
    "Breaking.Bad.S02.sample.mkv": {"title": "sample"},
    # A split-disk chunk of a single episode: it STILL parses with a valid
    # episode number, so without the same MULTI_PART guard validate_import uses
    # it would otherwise reach an accepted result.
    "Breaking.Bad.S02E01.CD1.1080p.WEB-DL.x264-GRP.mkv": {
        "title": "Breaking Bad",
        "season": 2,
        "episode": 1,
        "source": "Web",
        "screen_size": "1080p",
    },
}


class FakeTvParser:
    """A ParserPort that maps known basenames through the real source_mapping."""

    def parse(self, release_name: str) -> ParsedRelease:
        fields = _TV_FIELDS.get(release_name, {})
        return to_parsed_release(fields, release_name)


def _validate_season(
    *files: VideoFile,
    requested_episodes: list[int] | None = None,
) -> SeasonImportValidation:
    return validate_season_import(
        list(files),
        parser=FakeTvParser(),
        profile=default_profile(),
        expected_title="Breaking Bad",
        expected_tmdb_id=1396,
        expected_season=2,
        requested_episodes=requested_episodes,
    )


def test_full_season_pack_all_accept() -> None:
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E01.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
        VideoFile("Breaking.Bad.S02E02.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
    )
    assert result.rejected == ()
    assert result.skipped_not_requested == ()
    assert {frozenset(r.episodes) for r in result.accepted} == {
        frozenset({1}),
        frozenset({2}),
    }


def test_mixed_accept_reject_one_cam_episode() -> None:
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E01.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
        VideoFile("Breaking.Bad.S02E03.HDCAM.x264-GRP.mkv", 2 * _GIB),
    )
    assert len(result.accepted) == 1
    assert result.accepted[0].episodes == (1,)
    assert len(result.rejected) == 1
    assert result.rejected[0].reason is ImportRejectionReason.QUALITY_NOT_WANTED
    assert result.rejected[0].relative_path == "Breaking.Bad.S02E03.HDCAM.x264-GRP.mkv"


def test_wrong_season_in_pack_rejects_wrong_media() -> None:
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E01.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
        VideoFile("Breaking.Bad.S01E01.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
    )
    assert len(result.accepted) == 1
    assert len(result.rejected) == 1
    assert result.rejected[0].reason is ImportRejectionReason.WRONG_MEDIA
    assert result.rejected[0].relative_path == "Breaking.Bad.S01E01.1080p.WEB-DL.x264-GRP.mkv"


def test_no_episode_number_rejects() -> None:
    result = _validate_season(
        VideoFile("Breaking.Bad.S02.Bonus.Content.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
    )
    assert result.accepted == ()
    assert len(result.rejected) == 1
    assert result.rejected[0].reason is ImportRejectionReason.NO_EPISODE_NUMBER


def test_sample_and_nfo_are_silently_dropped() -> None:
    # Neither the named sample nor a non-video (.nfo) file is a candidate at all:
    # not accepted, not rejected, not skipped -- they were never episodes.
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E01.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
        VideoFile("Breaking.Bad.S02.sample.mkv", 40 * 1024 * 1024),
        VideoFile("Breaking.Bad.S02E01.1080p.WEB-DL.x264-GRP.nfo", 1024),
    )
    assert len(result.accepted) == 1
    assert result.rejected == ()
    assert result.skipped_not_requested == ()


def test_requested_episodes_skip_not_requested_ones() -> None:
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E01.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
        VideoFile("Breaking.Bad.S02E02.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
        requested_episodes=[1],
    )
    assert len(result.accepted) == 1
    assert result.accepted[0].episodes == (1,)
    assert result.rejected == ()
    assert len(result.skipped_not_requested) == 1
    assert result.skipped_not_requested[0].episodes == (2,)


def test_multi_episode_file_kept_in_full_on_partial_overlap() -> None:
    # Only episode 5 was requested, but the file also covers episode 4 (they
    # cannot be split); the WHOLE file is accepted, not skipped.
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E04E05.1080p.WEB-DL.x264-GRP.mkv", 3 * _GIB),
        requested_episodes=[5],
    )
    assert result.skipped_not_requested == ()
    assert result.rejected == ()
    assert len(result.accepted) == 1
    assert result.accepted[0].episodes == (4, 5)


def test_multi_episode_file_skipped_when_no_overlap_at_all() -> None:
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E04E05.1080p.WEB-DL.x264-GRP.mkv", 3 * _GIB),
        requested_episodes=[1, 2],
    )
    assert result.accepted == ()
    assert result.rejected == ()
    assert len(result.skipped_not_requested) == 1
    assert result.skipped_not_requested[0].episodes == (4, 5)


def test_split_part_episode_rejects_multi_part() -> None:
    # F5: a split TV episode chunk (S02E01.CD1) still parses with a valid episode
    # number, so without applying the SAME split-part guard validate_import uses,
    # it would reach an accepted result here; the duplicate-destination logic
    # would then keep only the largest chunk and mark the season completed with
    # an incomplete episode file. It must be rejected MULTI_PART instead.
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E01.CD1.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
    )
    assert result.accepted == ()
    assert len(result.rejected) == 1
    assert result.rejected[0].reason is ImportRejectionReason.MULTI_PART
    assert result.rejected[0].relative_path == "Breaking.Bad.S02E01.CD1.1080p.WEB-DL.x264-GRP.mkv"


def test_split_part_episode_rejected_others_in_pack_still_accepted() -> None:
    # Partial success stays legit: a split chunk for E01 is rejected while a
    # clean E02 file in the SAME pack is still accepted -- the season import is
    # not all-or-nothing.
    result = _validate_season(
        VideoFile("Breaking.Bad.S02E01.CD1.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
        VideoFile("Breaking.Bad.S02E02.1080p.WEB-DL.x264-GRP.mkv", 2 * _GIB),
    )
    assert len(result.accepted) == 1
    assert result.accepted[0].episodes == (2,)
    assert len(result.rejected) == 1
    assert result.rejected[0].reason is ImportRejectionReason.MULTI_PART
    assert result.rejected[0].relative_path == "Breaking.Bad.S02E01.CD1.1080p.WEB-DL.x264-GRP.mkv"
