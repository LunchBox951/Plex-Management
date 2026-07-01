"""End-to-end tests for the decision engine with a fake parser (no guessit)."""

from __future__ import annotations

from datetime import UTC, datetime

from plex_manager.domain.blocklist import BlocklistedRelease, is_blocklisted
from plex_manager.domain.decision_engine import decide
from plex_manager.domain.quality import (
    BLURAY1080P,
    WEBDL720P,
    WEBDL1080P,
    WEBRIP1080P,
)
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.quality_service import RejectionReason
from plex_manager.domain.release import CandidateRelease, ParsedRelease
from plex_manager.domain.source_mapping import to_parsed_release

# title -> recorded guessit-style field mapping (keeps the test guessit-free).
_FIELDS: dict[str, dict[str, object]] = {
    "Movie.2024.1080p.BluRay.x264-GRP": {"source": "Blu-ray", "screen_size": "1080p"},
    "Movie.2024.1080p.WEB-DL.x264-A": {"source": "Web", "screen_size": "1080p"},
    "Movie.2024.1080p.WEB-DL.x264-B": {"source": "Web", "screen_size": "1080p"},
    "Movie.2024.720p.WEB-DL.x264-GRP": {"source": "Web", "screen_size": "720p"},
    "Movie.2024.1080p.WEBRip.x264-GRP": {
        "source": "Web",
        "screen_size": "1080p",
        "other": "Rip",
    },
    "Movie.2024.TELESYNC.x264-GRP": {"source": "Telesync"},
    "Movie.2024.HQCAM.x264-GRP": {"alternative_title": "HQCAM"},
    "Show.S02.1080p.WEB-DL.x264-GRP": {"source": "Web", "screen_size": "1080p", "season": 2},
    "Show.S02E05.1080p.WEB-DL.x264-GRP": {
        "source": "Web",
        "screen_size": "1080p",
        "season": 2,
        "episode": 5,
    },
    # A multi-season pack (S01-S03) — season is a LIST, so classify_release_scope
    # returns "multi_season_pack".
    "Show.S01-S03.COMPLETE.1080p.WEB-DL.x264-GRP": {
        "source": "Web",
        "screen_size": "1080p",
        "season": [1, 2, 3],
    },
}


class FakeParser:
    """A ParserPort that maps known titles through the real source_mapping."""

    def parse(self, release_name: str) -> ParsedRelease:
        fields = _FIELDS.get(release_name, {})
        return to_parsed_release(fields, release_name)


def _candidate(
    title: str,
    *,
    seeders: int = 10,
    size_bytes: int = 1_000_000_000,
    info_hash: str | None = None,
) -> CandidateRelease:
    return CandidateRelease(
        guid=title,
        title=title,
        size_bytes=size_bytes,
        info_hash=info_hash,
        seeders=seeders,
        indexer_id=1,
        indexer_name="Idx",
        publish_date=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _never_blocklisted(_candidate: CandidateRelease, _parsed: ParsedRelease) -> bool:
    return False


def _always_media(_candidate: CandidateRelease, _parsed: ParsedRelease) -> bool:
    """Media-identity gate that accepts everything (isolates the other gates)."""
    return True


def test_ranks_accepted_best_first_and_rejects_prerelease() -> None:
    candidates = [
        _candidate("Movie.2024.720p.WEB-DL.x264-GRP"),
        _candidate("Movie.2024.TELESYNC.x264-GRP"),
        _candidate("Movie.2024.1080p.BluRay.x264-GRP"),
        _candidate("Movie.2024.1080p.WEB-DL.x264-A"),
        _candidate("Movie.2024.HQCAM.x264-GRP"),
    ]
    result = decide(candidates, FakeParser(), default_profile(), _always_media, _never_blocklisted)

    assert result.no_acceptable_release is False
    accepted_qualities = [scored.quality for scored in result.accepted]
    assert accepted_qualities == [BLURAY1080P, WEBDL1080P, WEBDL720P]
    # Scores strictly decrease best-first.
    scores = [scored.score for scored in result.accepted]
    assert scores == sorted(scores, reverse=True)

    rejected_reasons = {c.title: reason for c, reason in result.rejected}
    assert rejected_reasons["Movie.2024.TELESYNC.x264-GRP"] is RejectionReason.QUALITY_NOT_WANTED
    assert rejected_reasons["Movie.2024.HQCAM.x264-GRP"] is RejectionReason.QUALITY_NOT_WANTED


def test_seeders_break_ties_within_same_quality() -> None:
    high = _candidate("Movie.2024.1080p.WEB-DL.x264-A", seeders=500)
    low = _candidate("Movie.2024.1080p.WEB-DL.x264-B", seeders=5)
    result = decide([low, high], FakeParser(), default_profile(), _always_media, _never_blocklisted)

    assert [s.candidate.title for s in result.accepted] == [
        "Movie.2024.1080p.WEB-DL.x264-A",
        "Movie.2024.1080p.WEB-DL.x264-B",
    ]


def test_size_breaks_ties_when_quality_and_seeders_equal() -> None:
    big = _candidate("Movie.2024.1080p.WEB-DL.x264-A", seeders=10, size_bytes=8_000_000_000)
    small = _candidate("Movie.2024.1080p.WEB-DL.x264-B", seeders=10, size_bytes=2_000_000_000)
    result = decide(
        [small, big], FakeParser(), default_profile(), _always_media, _never_blocklisted
    )

    assert result.accepted[0].candidate is big


def test_webdl_outranks_equal_webrip_at_same_resolution() -> None:
    # Radarr ranks WEBDL-Np and WEBRip-Np as equal (shared weight); flattening the
    # group must still prefer the cleaner WEBDL source. With seeders + size equal,
    # only the profile order decides — WEBDL-1080p must win.
    webdl = _candidate("Movie.2024.1080p.WEB-DL.x264-A", seeders=10, size_bytes=1_000_000_000)
    webrip = _candidate("Movie.2024.1080p.WEBRip.x264-GRP", seeders=10, size_bytes=1_000_000_000)
    result = decide(
        [webrip, webdl], FakeParser(), default_profile(), _always_media, _never_blocklisted
    )

    assert result.no_acceptable_release is False
    assert [s.quality for s in result.accepted] == [WEBDL1080P, WEBRIP1080P]
    assert result.accepted[0].candidate is webdl


def test_no_acceptable_release_when_all_candidates_are_prerelease() -> None:
    candidates = [
        _candidate("Movie.2024.TELESYNC.x264-GRP"),
        _candidate("Movie.2024.HQCAM.x264-GRP"),
    ]
    result = decide(candidates, FakeParser(), default_profile(), _always_media, _never_blocklisted)

    assert result.accepted == []
    assert result.no_acceptable_release is True
    assert len(result.rejected) == 2


def test_blocklisted_candidate_is_filtered_after_quality_gate() -> None:
    bad_hash = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    entries = [BlocklistedRelease(source_title="x", info_hash=bad_hash, indexer="Idx")]

    def _blocklist_check(candidate: CandidateRelease, _parsed: ParsedRelease) -> bool:
        return is_blocklisted(
            info_hash=candidate.info_hash,
            source_title=candidate.title,
            indexer=candidate.indexer_name,
            entries=entries,
        )

    blocked = _candidate("Movie.2024.1080p.BluRay.x264-GRP", info_hash=bad_hash)
    clean = _candidate("Movie.2024.1080p.WEB-DL.x264-A")
    result = decide(
        [blocked, clean], FakeParser(), default_profile(), _always_media, _blocklist_check
    )

    assert [s.candidate.title for s in result.accepted] == ["Movie.2024.1080p.WEB-DL.x264-A"]
    assert (blocked, RejectionReason.BLOCKLISTED) in result.rejected


def test_empty_candidate_set_surfaces_no_acceptable_release() -> None:
    result = decide([], FakeParser(), default_profile(), _always_media, _never_blocklisted)
    assert result.accepted == []
    assert result.no_acceptable_release is True
    assert result.rejected == []


def test_prefer_season_pack_default_is_byte_identical_to_no_season_pack_ranking() -> None:
    # Same quality, same seeders, DIFFERENT size: the plain size tiebreak (unaware
    # of season-pack scope) must decide, exactly as before prefer_season_pack
    # existed -- the bigger single-episode release ranks first.
    pack = _candidate("Show.S02.1080p.WEB-DL.x264-GRP", seeders=10, size_bytes=1_000_000_000)
    single = _candidate("Show.S02E05.1080p.WEB-DL.x264-GRP", seeders=10, size_bytes=2_000_000_000)
    result = decide(
        [pack, single], FakeParser(), default_profile(), _always_media, _never_blocklisted
    )

    assert [s.candidate.title for s in result.accepted] == [
        "Show.S02E05.1080p.WEB-DL.x264-GRP",
        "Show.S02.1080p.WEB-DL.x264-GRP",
    ]


def test_prefer_season_pack_breaks_ties_toward_the_pack() -> None:
    # Identical quality + seeders, and the pack is even SMALLER (so the default
    # size tiebreak would rank it second) -- with prefer_season_pack=True the
    # scope tiebreak fires BEFORE seeders/size and the pack wins outright.
    pack = _candidate("Show.S02.1080p.WEB-DL.x264-GRP", seeders=10, size_bytes=1_000_000_000)
    single = _candidate("Show.S02E05.1080p.WEB-DL.x264-GRP", seeders=10, size_bytes=2_000_000_000)
    result = decide(
        [pack, single],
        FakeParser(),
        default_profile(),
        _always_media,
        _never_blocklisted,
        prefer_season_pack=True,
    )

    assert [s.candidate.title for s in result.accepted] == [
        "Show.S02.1080p.WEB-DL.x264-GRP",
        "Show.S02E05.1080p.WEB-DL.x264-GRP",
    ]
    # The scope bonus never overrides the profile-order comparator: it is a purely
    # additive score component that must not itself decide acceptance/rejection.
    assert result.no_acceptable_release is False


def test_prefer_season_pack_prefers_a_multi_season_pack_over_a_single_episode() -> None:
    # A whole-season grab: no exact S02 pack, but an S01-S03 multi-season pack that
    # covers S02 and a HIGHER-seeded single episode. The multi-season pack must win --
    # it can satisfy the whole requested season, a single episode cannot. Before the
    # fix multi_season_pack earned no scope bonus and the single episode's seeder edge
    # made it the top grab.
    multi = _candidate("Show.S01-S03.COMPLETE.1080p.WEB-DL.x264-GRP", seeders=10)
    single = _candidate("Show.S02E05.1080p.WEB-DL.x264-GRP", seeders=500)
    result = decide(
        [multi, single],
        FakeParser(),
        default_profile(),
        _always_media,
        _never_blocklisted,
        prefer_season_pack=True,
    )
    assert result.accepted[0].candidate.title == "Show.S01-S03.COMPLETE.1080p.WEB-DL.x264-GRP"


def test_prefer_season_pack_never_beats_a_higher_quality_release() -> None:
    # prefer_season_pack only breaks a tie AFTER profile order -- a lower-quality
    # season pack must still lose to a higher-quality single episode.
    pack = _candidate("Show.S02.1080p.WEB-DL.x264-GRP", seeders=10)
    higher_quality_single = _candidate(
        "Movie.2024.1080p.BluRay.x264-GRP", seeders=10
    )  # BluRay-1080p outranks WEBDL-1080p in the default profile
    result = decide(
        [pack, higher_quality_single],
        FakeParser(),
        default_profile(),
        _always_media,
        _never_blocklisted,
        prefer_season_pack=True,
    )

    assert [s.candidate.title for s in result.accepted] == [
        "Movie.2024.1080p.BluRay.x264-GRP",
        "Show.S02.1080p.WEB-DL.x264-GRP",
    ]


def test_wrong_media_is_rejected_before_quality_even_if_top_quality() -> None:
    # A pristine BluRay-1080p release that the media-identity gate rejects (it
    # names a different movie) must be discarded WRONG_MEDIA and never scored —
    # its high quality must not let it out-rank the correct, lower-quality grab.
    wrong = _candidate("Movie.2024.1080p.BluRay.x264-GRP")
    right = _candidate("Movie.2024.720p.WEB-DL.x264-GRP")

    def _reject_the_bluray(candidate: CandidateRelease, _parsed: ParsedRelease) -> bool:
        return candidate is not wrong

    result = decide(
        [wrong, right], FakeParser(), default_profile(), _reject_the_bluray, _never_blocklisted
    )

    assert [s.candidate.title for s in result.accepted] == ["Movie.2024.720p.WEB-DL.x264-GRP"]
    assert (wrong, RejectionReason.WRONG_MEDIA) in result.rejected
