"""Decision-engine tests for the Pass-2 episode-fallback gate (issue #178, ADR-0018).

Mirrors ``test_decision_engine.py``'s fake-parser pattern (no guessit) and
exercises ``decide(..., episode_subset=...)`` in isolation from the
``prefer_season_pack`` (Pass 1, issue #167) gate it deliberately never overlaps.
"""

from __future__ import annotations

from datetime import UTC, datetime

from plex_manager.domain.decision_engine import decide
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.quality_service import RejectionReason
from plex_manager.domain.release import CandidateRelease, ParsedRelease
from plex_manager.domain.source_mapping import to_parsed_release

_FIELDS: dict[str, dict[str, object]] = {
    "Show.S02.1080p.WEB-DL.x264-GRP": {"source": "Web", "screen_size": "1080p", "season": 2},
    "Show.S02E04.1080p.WEB-DL.x264-GRP": {
        "source": "Web",
        "screen_size": "1080p",
        "season": 2,
        "episode": 4,
    },
    "Show.S02E05.1080p.WEB-DL.x264-GRP": {
        "source": "Web",
        "screen_size": "1080p",
        "season": 2,
        "episode": 5,
    },
    "Show.S02E06.1080p.WEB-DL.x264-GRP": {
        "source": "Web",
        "screen_size": "1080p",
        "season": 2,
        "episode": 6,
    },
    "Show.S02E04E05.1080p.WEB-DL.x264-GRP": {
        "source": "Web",
        "screen_size": "1080p",
        "season": 2,
        "episode": [4, 5],
    },
    "Show.S02E04E05E06.1080p.WEB-DL.x264-GRP": {
        "source": "Web",
        "screen_size": "1080p",
        "season": 2,
        "episode": [4, 5, 6],
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
) -> CandidateRelease:
    return CandidateRelease(
        guid=title,
        title=title,
        size_bytes=size_bytes,
        info_hash=None,
        seeders=seeders,
        indexer_id=1,
        indexer_name="Idx",
        publish_date=datetime(2024, 1, 1, tzinfo=UTC),
    )


def _never_blocklisted(_candidate: CandidateRelease, _parsed: ParsedRelease) -> bool:
    return False


def _always_media(_candidate: CandidateRelease, _parsed: ParsedRelease) -> bool:
    return True


def test_subset_accept_reject() -> None:
    e04 = _candidate("Show.S02E04.1080p.WEB-DL.x264-GRP")
    e06 = _candidate("Show.S02E06.1080p.WEB-DL.x264-GRP")
    e04e05 = _candidate("Show.S02E04E05.1080p.WEB-DL.x264-GRP")
    e04e05e06 = _candidate("Show.S02E04E05E06.1080p.WEB-DL.x264-GRP")

    result = decide(
        [e04, e06, e04e05, e04e05e06],
        FakeParser(),
        default_profile(),
        _always_media,
        _never_blocklisted,
        prefer_season_pack=False,
        episode_subset=frozenset({4, 5}),
    )

    accepted_titles = {s.candidate.title for s in result.accepted}
    assert accepted_titles == {
        "Show.S02E04.1080p.WEB-DL.x264-GRP",
        "Show.S02E04E05.1080p.WEB-DL.x264-GRP",
    }
    rejected_reasons = {c.title: reason for c, reason in result.rejected}
    assert (
        rejected_reasons["Show.S02E06.1080p.WEB-DL.x264-GRP"] is RejectionReason.EPISODE_NOT_NEEDED
    )
    assert (
        rejected_reasons["Show.S02E04E05E06.1080p.WEB-DL.x264-GRP"]
        is RejectionReason.EPISODE_NOT_NEEDED
    )


def test_empty_episode_pack_rejected_in_pass_two() -> None:
    pack = _candidate("Show.S02.1080p.WEB-DL.x264-GRP")

    result = decide(
        [pack],
        FakeParser(),
        default_profile(),
        _always_media,
        _never_blocklisted,
        prefer_season_pack=False,
        episode_subset=frozenset({1, 2}),
    )

    assert result.accepted == ()
    assert result.rejected[0][1] is RejectionReason.EPISODE_NOT_NEEDED


def test_no_redundant_grab_of_already_imported_episode() -> None:
    # Episode 4 is already imported (not in the missing set); a release
    # overlapping it must be rejected even though it also covers episode 5.
    e04e05 = _candidate("Show.S02E04E05.1080p.WEB-DL.x264-GRP")
    e05 = _candidate("Show.S02E05.1080p.WEB-DL.x264-GRP")

    result = decide(
        [e04e05, e05],
        FakeParser(),
        default_profile(),
        _always_media,
        _never_blocklisted,
        prefer_season_pack=False,
        episode_subset=frozenset({5}),
    )

    accepted_titles = {s.candidate.title for s in result.accepted}
    assert accepted_titles == {"Show.S02E05.1080p.WEB-DL.x264-GRP"}
    rejected_reasons = {c.title: reason for c, reason in result.rejected}
    assert (
        rejected_reasons["Show.S02E04E05.1080p.WEB-DL.x264-GRP"]
        is RejectionReason.EPISODE_NOT_NEEDED
    )


def test_pack_first_precedence_unaffected_by_episode_subset_param() -> None:
    pack = _candidate("Show.S02.1080p.WEB-DL.x264-GRP")
    single = _candidate("Show.S02E04.1080p.WEB-DL.x264-GRP")

    result = decide(
        [pack, single],
        FakeParser(),
        default_profile(),
        _always_media,
        _never_blocklisted,
        prefer_season_pack=True,
        episode_subset=None,
    )

    assert [s.candidate.title for s in result.accepted] == ["Show.S02.1080p.WEB-DL.x264-GRP"]
    rejected_reasons = {c.title: reason for c, reason in result.rejected}
    assert rejected_reasons["Show.S02E04.1080p.WEB-DL.x264-GRP"] is RejectionReason.NOT_SEASON_PACK


def test_episode_subset_default_is_byte_identical() -> None:
    e04 = _candidate("Show.S02E04.1080p.WEB-DL.x264-GRP")
    e05 = _candidate("Show.S02E05.1080p.WEB-DL.x264-GRP")

    without_kw = decide(
        [e04, e05], FakeParser(), default_profile(), _always_media, _never_blocklisted
    )
    with_none = decide(
        [e04, e05],
        FakeParser(),
        default_profile(),
        _always_media,
        _never_blocklisted,
        episode_subset=None,
    )

    assert [s.candidate.title for s in without_kw.accepted] == [
        s.candidate.title for s in with_none.accepted
    ]
    assert without_kw.no_acceptable_release == with_none.no_acceptable_release
