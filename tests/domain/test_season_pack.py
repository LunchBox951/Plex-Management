"""Tests for the pure season-pack scope classifier."""

from __future__ import annotations

from plex_manager.domain.release import ParsedRelease
from plex_manager.domain.season_pack import classify_release_scope, covers_requested_episodes


def _parsed(
    season: int | list[int] | None = None,
    episode: int | list[int] | None = None,
) -> ParsedRelease:
    return ParsedRelease(raw_title="x", clean_title="x", season=season, episode=episode)


def test_single_episode() -> None:
    assert classify_release_scope(_parsed(season=2, episode=5)) == "single_episode"


def test_multi_episode_file_is_still_single_episode_scope() -> None:
    # A multi-episode file (S02E05E06) is still a single-season release, not a
    # season pack -- the episode field just carries more than one number.
    assert classify_release_scope(_parsed(season=2, episode=[5, 6])) == "single_episode"


def test_season_pack() -> None:
    # A whole-season pack has a season but no episode token at all.
    assert classify_release_scope(_parsed(season=2, episode=None)) == "season_pack"


def test_multi_season_pack() -> None:
    assert classify_release_scope(_parsed(season=[1, 2, 3], episode=None)) == "multi_season_pack"


def test_multi_season_pack_wins_even_with_episode_set() -> None:
    # A multi-season release exposes no per-episode scope; the multi-season
    # classification takes precedence over any (spurious) episode field.
    assert classify_release_scope(_parsed(season=[1, 2], episode=5)) == "multi_season_pack"


def test_unknown_when_no_season_at_all() -> None:
    assert classify_release_scope(_parsed(season=None, episode=None)) == "unknown"
    assert classify_release_scope(_parsed(season=None, episode=5)) == "unknown"


def test_single_element_season_list_treated_as_single_season() -> None:
    # A one-element season list behaves exactly like the equivalent int.
    assert classify_release_scope(_parsed(season=[2], episode=None)) == "season_pack"
    assert classify_release_scope(_parsed(season=[2], episode=5)) == "single_episode"


# -- covers_requested_episodes (F4: episode-overlap gate) --


def test_covers_requested_episodes_single_episode_matching() -> None:
    assert covers_requested_episodes(_parsed(season=2, episode=4), requested=[4]) is True


def test_covers_requested_episodes_single_episode_wrong_episode_is_the_core_bug() -> None:
    # S02E01 does not cover a request for E04 -- the exact wrong-torrent bug F4
    # exists to stop: a tracker returning the wrong episode of the right season.
    assert covers_requested_episodes(_parsed(season=2, episode=1), requested=[4]) is False


def test_covers_requested_episodes_whole_season_pack_never_rejected() -> None:
    # A whole-season pack (no episode token) inherently contains whatever episode
    # is requested -- packs are never rejected by this gate.
    assert covers_requested_episodes(_parsed(season=2, episode=None), requested=[4]) is True


def test_covers_requested_episodes_multi_season_pack_rejected() -> None:
    # Issue #24 beta posture: a multi-season pack is never grab-eligible (the
    # decision engine permanently rejects it), so this gate must not wave one
    # through either -- the two gates must agree.
    assert (
        covers_requested_episodes(_parsed(season=[1, 2, 3], episode=None), requested=[4]) is False
    )


def test_covers_requested_episodes_multi_episode_file_partial_overlap_kept() -> None:
    # A multi-episode file overlapping even partially with the requested set is
    # kept -- a file cannot be split, mirroring validate_season_import's posture.
    assert covers_requested_episodes(_parsed(season=2, episode=[4, 5]), requested=[4]) is True


def test_covers_requested_episodes_multi_episode_file_no_overlap_rejected() -> None:
    assert covers_requested_episodes(_parsed(season=2, episode=[1, 2]), requested=[4]) is False


def test_covers_requested_episodes_unknown_scope_conservative_reject() -> None:
    # No season at all -> "unknown" scope -- conservative reject, mirroring the
    # missing-season posture already used elsewhere.
    assert covers_requested_episodes(_parsed(season=None, episode=None), requested=[4]) is False
