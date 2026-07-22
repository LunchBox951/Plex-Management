"""Tests for the pure season-pack scope classifier."""

from __future__ import annotations

from plex_manager.domain.quality import WEBDL720P, WEBDL1080P
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.release import ParsedRelease
from plex_manager.domain.season_pack import (
    MultiSeasonRequestIntent,
    MultiSeasonRequestMode,
    SeasonPackSeasonState,
    classify_release_scope,
    covers_requested_episodes,
    plan_multi_season_pack,
)


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


def test_covers_requested_episodes_multi_season_pack_passes_here_engine_rejects() -> None:
    # Division of authority (issue #24): this episode-overlap helper backs the
    # media-identity gate, which runs FIRST, so it must PASS a multi-season pack.
    # The pack is still refused for the beta, but that rejection is owned solely
    # by the decision-engine multi-season gate (see the test_multi_season_pack_*
    # cases in tests/domain/test_decision_engine.py), which surfaces the accurate
    # RejectionReason.MULTI_SEASON_PACK instead of a misleading WRONG_MEDIA.
    assert covers_requested_episodes(_parsed(season=[1, 2, 3], episode=None), requested=[4]) is True


def test_covers_requested_episodes_multi_episode_file_superset_kept() -> None:
    # A multi-episode file whose episodes are a SUPERSET of the request is kept --
    # the requested episode(s) are all present; the extras ride along (a file
    # cannot be split), mirroring validate_season_import's ``skipped_not_requested``.
    assert covers_requested_episodes(_parsed(season=2, episode=[4, 5]), requested=[4]) is True


def test_covers_requested_episodes_multi_episode_file_exact_cover_kept() -> None:
    # A multi-episode file that covers the WHOLE multi-episode request passes.
    assert covers_requested_episodes(_parsed(season=2, episode=[4, 5]), requested=[4, 5]) is True


def test_covers_requested_episodes_partial_single_episode_rejected_is_the_70_bug() -> None:
    # issue #70: a single-episode S02E04 release must NOT satisfy a request for
    # BOTH episodes {4, 5}. Any-overlap accepted it (E04 overlaps {4, 5}), so the
    # engine grabbed a partial release and import later blocked on the missing E05.
    # Full coverage (superset) is now required, so this is rejected at preview.
    assert covers_requested_episodes(_parsed(season=2, episode=4), requested=[4, 5]) is False


def test_covers_requested_episodes_multi_episode_file_partial_cover_rejected() -> None:
    # A file carrying {4, 6} does not COVER a request for {4, 5} -- E5 is missing.
    assert covers_requested_episodes(_parsed(season=2, episode=[4, 6]), requested=[4, 5]) is False


def test_covers_requested_episodes_multi_episode_file_no_overlap_rejected() -> None:
    assert covers_requested_episodes(_parsed(season=2, episode=[1, 2]), requested=[4]) is False


def test_covers_requested_episodes_unknown_scope_conservative_reject() -> None:
    # No season at all -> "unknown" scope -- conservative reject, mirroring the
    # missing-season posture already used elsewhere.
    assert covers_requested_episodes(_parsed(season=None, episode=None), requested=[4]) is False


# -- multi-season pack eligibility (issue #24 follow-up) --


def _intent(
    mode: MultiSeasonRequestMode,
    requested_seasons: list[int],
    states: list[SeasonPackSeasonState],
) -> MultiSeasonRequestIntent:
    return MultiSeasonRequestIntent(
        mode=mode,
        requested_seasons=tuple(requested_seasons),
        seasons=tuple(states),
    )


def _season(
    season_number: int,
    status: str = "pending",
    quality_id: int | None = None,
    profile_index: int | None = None,
) -> SeasonPackSeasonState:
    return SeasonPackSeasonState(
        season_number=season_number,
        status=status,
        installed_quality_id=quality_id,
        installed_profile_index=profile_index,
    )


def test_plan_whole_show_accepts_partial_pack_for_tracked_seasons() -> None:
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2, 3],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2, 3, 4, 5],
            [_season(1), _season(2), _season(3), _season(4), _season(5)],
        ),
    )

    assert plan.accepted is True
    assert plan.target_seasons == (1, 2, 3)
    assert plan.ignored_seasons == ()


def test_plan_explicit_seasons_rejects_pack_with_extra_season() -> None:
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2, 3],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "explicit_seasons",
            [1, 2],
            [_season(1), _season(2), _season(3)],
        ),
    )

    assert plan.accepted is False
    assert plan.reason == "explicit_season_set_mismatch"
    assert plan.target_seasons == ()
    assert plan.ignored_seasons == (3,)


def test_plan_same_quality_overlap_accepts_when_useful_outnumbers_waste() -> None:
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2, 3],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2, 3],
            [
                _season(1, "available", WEBDL1080P.id),
                _season(2),
                _season(3),
            ],
        ),
    )

    assert plan.accepted is True
    assert plan.target_seasons == (2, 3)
    assert plan.waste_seasons == (1,)


def test_plan_same_quality_overlap_rejects_when_waste_ties_or_wins() -> None:
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2, 3],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2, 3],
            [
                _season(1, "available", WEBDL1080P.id),
                _season(2, "available", WEBDL1080P.id),
                _season(3),
            ],
        ),
    )

    assert plan.accepted is False
    assert plan.reason == "useful_seasons_not_majority"
    assert plan.target_seasons == (3,)
    assert plan.waste_seasons == (1, 2)


def test_plan_higher_quality_overlap_does_not_target_upgrade_without_replacement() -> None:
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2],
            [
                _season(1, "available", WEBDL720P.id),
                _season(2),
            ],
        ),
    )

    assert plan.accepted is False
    assert plan.reason == "useful_seasons_not_majority"
    assert plan.target_seasons == (2,)
    assert plan.upgrade_seasons == ()
    assert plan.waste_seasons == (1,)


def test_plan_all_higher_quality_overlap_rejects_until_replacement_supported() -> None:
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2],
            [
                _season(1, "available", WEBDL720P.id),
                _season(2, "available", WEBDL720P.id),
            ],
        ),
    )

    assert plan.accepted is False
    assert plan.reason == "no_useful_seasons"
    assert plan.target_seasons == ()
    assert plan.upgrade_seasons == ()
    assert plan.waste_seasons == (1, 2)


def test_plan_unknown_legacy_quality_is_waste_not_upgrade() -> None:
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2],
            [
                _season(1, "available", None),
                _season(2),
            ],
        ),
    )

    assert plan.accepted is False
    assert plan.reason == "useful_seasons_not_majority"
    assert plan.target_seasons == (2,)
    assert plan.upgrade_seasons == ()
    assert plan.waste_seasons == (1,)


def test_plan_rejects_whole_show_pack_overlapping_in_flight_seasons() -> None:
    # Issue #409 (Suits): the reported S01-S09 pack surfaces after S1-S7 are already
    # grabbed as individual packs (``downloading``) and S8-S9 are still ``pending``.
    # The physical pack would re-download S1-S7, so it is REJECTED with the dedicated
    # in-flight reason -- the overlapping seasons are NOT laundered as ignored.
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2, 3, 4, 5, 6, 7, 8, 9],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2, 3, 4, 5, 6, 7, 8, 9],
            [
                *(_season(n, "downloading") for n in range(1, 8)),
                _season(8),
                _season(9),
            ],
        ),
    )

    assert plan.accepted is False
    assert plan.reason == "covered_season_in_flight"
    assert plan.in_flight_seasons == (1, 2, 3, 4, 5, 6, 7)
    assert plan.ignored_seasons == ()


def test_plan_accepts_whole_show_pack_when_all_seasons_pending() -> None:
    # The same nine-season pack IS accepted when nothing overlaps in flight: every
    # season is still ``pending`` (searchable), so all nine become targets.
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2, 3, 4, 5, 6, 7, 8, 9],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2, 3, 4, 5, 6, 7, 8, 9],
            [_season(n) for n in range(1, 10)],
        ),
    )

    assert plan.accepted is True
    assert plan.target_seasons == (1, 2, 3, 4, 5, 6, 7, 8, 9)
    assert plan.in_flight_seasons == ()


def test_plan_rejects_pack_overlapping_import_blocked_season() -> None:
    # ``import_blocked`` is in flight too (bytes on disk / mid-import): a pack that
    # also carries that season would collide, so the overlap rejects consistently.
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2],
            [_season(1, "import_blocked"), _season(2)],
        ),
    )

    assert plan.accepted is False
    assert plan.reason == "covered_season_in_flight"
    assert plan.in_flight_seasons == (1,)


def test_plan_null_installed_quality_ignores_stale_profile_index() -> None:
    plan = plan_multi_season_pack(
        pack_seasons=[1, 2],
        candidate_quality_id=WEBDL1080P.id,
        profile=default_profile(),
        intent=_intent(
            "whole_show",
            [1, 2],
            [
                _season(1, "available", None, profile_index=0),
                _season(2),
            ],
        ),
    )

    assert plan.accepted is False
    assert plan.upgrade_seasons == ()
    assert plan.waste_seasons == (1,)
