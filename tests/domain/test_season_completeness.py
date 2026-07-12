"""Pure arithmetic tests for episode-level season completeness (ADR-0020)."""

from __future__ import annotations

from datetime import date

from plex_manager.domain.season_completeness import (
    aired_target,
    compute_missing,
    season_is_complete,
)


def test_aired_target_excludes_future_and_unknown_air_dates() -> None:
    today = date(2026, 7, 11)
    episodes = {
        1: date(2026, 1, 1),  # past
        2: today,  # today counts as aired
        3: date(2026, 12, 25),  # future
        4: None,  # unknown -> not yet aired
    }

    assert aired_target(episodes, today) == frozenset({1, 2})


def test_aired_target_empty_when_no_dated_episodes() -> None:
    assert aired_target({}, date(2026, 7, 11)) == frozenset()
    assert aired_target({1: None}, date(2026, 7, 11)) == frozenset()


def test_compute_missing_excludes_imported_and_downloading() -> None:
    missing = compute_missing(target={1, 2, 3}, imported={1}, downloading={2})
    assert missing == frozenset({3})


def test_compute_missing_empty_when_fully_covered() -> None:
    assert compute_missing(target={1, 2}, imported={1, 2}, downloading=set()) == frozenset()


def test_season_is_complete_true_when_imported_covers_target() -> None:
    assert season_is_complete(target={1, 2}, imported={1, 2}) is True
    assert season_is_complete(target={1, 2}, imported={1, 2, 3}) is True


def test_season_is_complete_false_when_partial() -> None:
    assert season_is_complete(target={1, 2}, imported={1}) is False


def test_season_is_complete_false_when_target_empty() -> None:
    assert season_is_complete(target=set(), imported={1, 2}) is False
