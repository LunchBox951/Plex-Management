"""Tests for the release DTOs (frozen-ness, defaults, computed property)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from plex_manager.domain.quality import (
    WEBDL1080P,
    Modifier,
    QualitySource,
    Resolution,
)
from plex_manager.domain.release import (
    CandidateRelease,
    IndexerSearchRequest,
    ParsedRelease,
    Revision,
    ScoredRelease,
)


def _parsed(source: QualitySource) -> ParsedRelease:
    return ParsedRelease(raw_title="x", clean_title="x", source=source)


def test_revision_defaults() -> None:
    rev = Revision()
    assert (rev.version, rev.is_repack, rev.real) == (1, False, 0)


def test_parsed_release_defaults() -> None:
    parsed = ParsedRelease(raw_title="Some.Movie.2023", clean_title="Some Movie")
    assert parsed.source is QualitySource.UNKNOWN
    assert parsed.resolution is Resolution.UNKNOWN
    assert parsed.modifier is Modifier.NONE
    assert parsed.languages == []
    assert parsed.revision == Revision()
    assert parsed.season is None
    assert parsed.episode is None


def test_parsed_release_episode_single() -> None:
    parsed = ParsedRelease(raw_title="Show.S02E05", clean_title="Show", season=2, episode=5)
    assert parsed.season == 2
    assert parsed.episode == 5


def test_parsed_release_episode_multi() -> None:
    parsed = ParsedRelease(
        raw_title="Show.S02E05E06", clean_title="Show", season=2, episode=[5, 6]
    )
    assert parsed.episode == [5, 6]


def test_parsed_release_season_pack_has_no_episode() -> None:
    # A whole-season pack has a season but no episode at all.
    parsed = ParsedRelease(raw_title="Show.S02.1080p.WEB-DL", clean_title="Show", season=2)
    assert parsed.season == 2
    assert parsed.episode is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (QualitySource.CAM, True),
        (QualitySource.TELESYNC, True),
        (QualitySource.TELECINE, True),
        (QualitySource.WORKPRINT, True),
        (QualitySource.WEBDL, False),
        (QualitySource.BLURAY, False),
        (QualitySource.UNKNOWN, False),
    ],
)
def test_is_cam_or_prerelease(source: QualitySource, expected: bool) -> None:
    assert _parsed(source).is_cam_or_prerelease is expected


def test_parsed_release_is_frozen() -> None:
    parsed = _parsed(QualitySource.WEBDL)
    with pytest.raises(ValidationError):
        parsed.clean_title = "mutated"  # type: ignore[misc]


def test_candidate_release_defaults() -> None:
    candidate = CandidateRelease(
        guid="g1",
        title="Some.Movie.2023.1080p.WEB-DL",
        size_bytes=1024,
        indexer_id=1,
        indexer_name="idx",
        publish_date=datetime(2023, 1, 1, tzinfo=UTC),
    )
    assert candidate.protocol == "torrent"
    assert candidate.indexer_priority == 25
    assert candidate.categories == []
    assert candidate.magnet_url is None


def test_indexer_search_request_defaults() -> None:
    request = IndexerSearchRequest()
    assert request.media_type == "search"
    assert request.categories == []
    assert request.indexer_ids == []


def test_scored_release_holds_quality_dataclass() -> None:
    candidate = CandidateRelease(
        guid="g1",
        title="t",
        size_bytes=1,
        indexer_id=1,
        indexer_name="idx",
        publish_date=datetime(2023, 1, 1, tzinfo=UTC),
    )
    scored = ScoredRelease(
        candidate=candidate,
        parsed=_parsed(QualitySource.WEBDL),
        quality=WEBDL1080P,
        profile_index=19,
        score=42.5,
    )
    assert scored.quality is WEBDL1080P
    assert scored.profile_index == 19
