"""Tests for the pure disk-pressure eviction candidate selection.

Inputs are built directly (no DB, no adapter, no Plex). ``grace_cutoff`` is
injected and fixed so the grace-window arithmetic is deterministic, mirroring
``test_reconciler.py``'s pattern of a fixed injected clock value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from plex_manager.domain.eviction import (
    EvictionCandidate,
    rank_eviction_candidates,
    select_evictions,
)

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
_GRACE_DAYS = 30
_GRACE_CUTOFF = _NOW - timedelta(days=_GRACE_DAYS)

# Well within the grace window (old enough to be past-grace).
_STALE = _GRACE_CUTOFF - timedelta(days=10)
# Recently watched -- inside the grace window, must never be evicted.
_RECENT = _GRACE_CUTOFF + timedelta(days=10)


def _candidate(
    *,
    request_id: int = 1,
    media_type: Literal["movie", "tv"] = "movie",
    title: str = "Some Movie",
    season: int | None = None,
    status: str = "available",
    watched: bool = True,
    last_viewed_at: datetime | None = _STALE,
    keep_forever: bool = False,
    in_flight: bool = False,
    watchlisted: bool = False,
    library_path: str | None = "/media/movies/Some Movie (2020)",
    size_percent: float = 5.0,
) -> EvictionCandidate:
    return EvictionCandidate(
        request_id=request_id,
        media_type=media_type,
        title=title,
        season=season,
        status=status,
        watched=watched,
        last_viewed_at=last_viewed_at,
        keep_forever=keep_forever,
        in_flight=in_flight,
        watchlisted=watchlisted,
        library_path=library_path,
        size_percent=size_percent,
    )


# --------------------------------------------------------------------------- #
# select_evictions: pressure gating
# --------------------------------------------------------------------------- #


def test_below_threshold_evicts_nothing_even_with_eligible_candidates() -> None:
    candidates = [_candidate()]
    result = select_evictions(
        candidates,
        used_pct=89.0,
        threshold_pct=90.0,
        target_pct=80.0,
        grace_cutoff=_GRACE_CUTOFF,
    )
    assert result == []


def test_used_pct_exactly_at_threshold_triggers_eviction() -> None:
    candidates = [_candidate()]
    result = select_evictions(
        candidates,
        used_pct=90.0,
        threshold_pct=90.0,
        target_pct=80.0,
        grace_cutoff=_GRACE_CUTOFF,
    )
    assert result == candidates


def test_no_candidates_under_pressure_evicts_nothing() -> None:
    result = select_evictions(
        [],
        used_pct=95.0,
        threshold_pct=90.0,
        target_pct=80.0,
        grace_cutoff=_GRACE_CUTOFF,
    )
    assert result == []


# --------------------------------------------------------------------------- #
# Eligibility gates (each holds even when every OTHER gate would allow eviction)
# --------------------------------------------------------------------------- #


def test_unwatched_is_never_evicted() -> None:
    candidates = [_candidate(watched=False)]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == []


def test_no_recorded_view_is_never_evicted_even_if_watched_flag_is_set() -> None:
    # Defensive: an inconsistent watched=True/last_viewed_at=None pair must never
    # be guessed into eligibility.
    candidates = [_candidate(watched=True, last_viewed_at=None)]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == []


def test_within_grace_window_is_never_evicted() -> None:
    candidates = [_candidate(last_viewed_at=_RECENT)]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == []


def test_last_viewed_at_exactly_at_grace_cutoff_is_never_evicted() -> None:
    # The comparison is strictly-less-than: a view AT the cutoff instant has not
    # yet cleared the grace period.
    candidates = [_candidate(last_viewed_at=_GRACE_CUTOFF)]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == []


def test_last_viewed_at_one_microsecond_past_cutoff_is_evicted() -> None:
    candidates = [_candidate(last_viewed_at=_GRACE_CUTOFF - timedelta(microseconds=1))]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == candidates


def test_keep_forever_is_never_evicted() -> None:
    candidates = [_candidate(keep_forever=True)]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == []


def test_watchlisted_is_never_evicted() -> None:
    candidates = [_candidate(watchlisted=True)]
    result = select_evictions(
        candidates,
        used_pct=95.0,
        threshold_pct=90.0,
        target_pct=0.0,
        grace_cutoff=_GRACE_CUTOFF,
    )
    assert result == []


def test_in_flight_is_never_evicted() -> None:
    candidates = [_candidate(in_flight=True)]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == []


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "searching",
        "no_acceptable_release",
        "downloading",
        "completed",
        "failed",
        "import_blocked",
        "evicted",
    ],
)
def test_ineligible_statuses_are_never_evicted(status: str) -> None:
    candidates = [_candidate(status=status)]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == []


@pytest.mark.parametrize("status", ["available", "partially_available"])
def test_eligible_statuses_are_evicted(status: str) -> None:
    candidates = [_candidate(status=status)]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == candidates


def test_combined_exclusions_all_hold_simultaneously() -> None:
    # A mixed batch: only the fully-eligible one survives.
    eligible = _candidate(request_id=1, last_viewed_at=_STALE)
    candidates = [
        _candidate(request_id=2, watched=False),
        _candidate(request_id=3, keep_forever=True),
        _candidate(request_id=4, in_flight=True),
        _candidate(request_id=5, status="downloading"),
        _candidate(request_id=6, last_viewed_at=_RECENT),
        eligible,
    ]
    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == [eligible]


# --------------------------------------------------------------------------- #
# Ordering: stalest last_viewed_at first
# --------------------------------------------------------------------------- #


def test_orders_stalest_last_viewed_at_first() -> None:
    oldest = _candidate(request_id=1, last_viewed_at=_STALE - timedelta(days=100))
    middle = _candidate(request_id=2, last_viewed_at=_STALE - timedelta(days=50))
    newest = _candidate(request_id=3, last_viewed_at=_STALE)
    # Deliberately unordered input.
    candidates = [newest, oldest, middle]

    result = select_evictions(
        candidates, used_pct=95.0, threshold_pct=90.0, target_pct=0.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == [oldest, middle, newest]


def test_stable_sort_preserves_input_order_for_identical_last_viewed_at() -> None:
    first = _candidate(request_id=1, last_viewed_at=_STALE)
    second = _candidate(request_id=2, last_viewed_at=_STALE)
    third = _candidate(request_id=3, last_viewed_at=_STALE)

    result = rank_eviction_candidates([third, first, second], _GRACE_CUTOFF)
    assert result == [third, first, second]


# --------------------------------------------------------------------------- #
# Target-based early stop
# --------------------------------------------------------------------------- #


def test_stops_once_projected_used_pct_reaches_target() -> None:
    # 95% used, target 80%: needs 15 points of relief. Stalest-first order is
    # a (10pts), b (10pts), c (10pts) -- picking a+b brings projection to 75%,
    # already <= target, so c must NOT be picked even though it is eligible.
    a = _candidate(request_id=1, last_viewed_at=_STALE - timedelta(days=30), size_percent=10.0)
    b = _candidate(request_id=2, last_viewed_at=_STALE - timedelta(days=20), size_percent=10.0)
    c = _candidate(request_id=3, last_viewed_at=_STALE - timedelta(days=10), size_percent=10.0)

    result = select_evictions(
        [c, b, a], used_pct=95.0, threshold_pct=90.0, target_pct=80.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == [a, b]


def test_target_reached_exactly_stops_without_extra_pick() -> None:
    a = _candidate(request_id=1, last_viewed_at=_STALE - timedelta(days=20), size_percent=15.0)
    b = _candidate(request_id=2, last_viewed_at=_STALE - timedelta(days=10), size_percent=15.0)

    # 95 - 15 = 80, exactly the target: b must not be picked.
    result = select_evictions(
        [a, b], used_pct=95.0, threshold_pct=90.0, target_pct=80.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == [a]


def test_undershoot_returns_every_eligible_candidate_when_not_enough_to_hit_target() -> None:
    a = _candidate(request_id=1, last_viewed_at=_STALE - timedelta(days=20), size_percent=2.0)
    b = _candidate(request_id=2, last_viewed_at=_STALE - timedelta(days=10), size_percent=2.0)

    # Only 4 points of relief available; target needs 15. Both are still
    # returned -- the function never invents a candidate that was not eligible.
    result = select_evictions(
        [b, a], used_pct=95.0, threshold_pct=90.0, target_pct=80.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == [a, b]


def test_zero_size_percent_candidates_are_still_all_picked_when_needed() -> None:
    # Unknown-size candidates (size_percent=0.0) never help close the gap on
    # their own, but they are still evicted in order -- the loop is a single
    # pass over a finite ranked list, never an infinite wait for "enough" size.
    a = _candidate(request_id=1, last_viewed_at=_STALE - timedelta(days=30), size_percent=0.0)
    b = _candidate(request_id=2, last_viewed_at=_STALE - timedelta(days=20), size_percent=0.0)

    result = select_evictions(
        [b, a], used_pct=95.0, threshold_pct=90.0, target_pct=80.0, grace_cutoff=_GRACE_CUTOFF
    )
    assert result == [a, b]


# --------------------------------------------------------------------------- #
# rank_eviction_candidates: pressure-independent preview / proactive building block
# --------------------------------------------------------------------------- #


def test_rank_eviction_candidates_ignores_disk_pressure_entirely() -> None:
    # No used_pct/threshold_pct/target_pct at all -- this is the "what WOULD be
    # evicted" preview / the proactive (non-pressure) sweep's building block.
    candidates = [_candidate(request_id=1), _candidate(request_id=2, watched=False)]
    result = rank_eviction_candidates(candidates, _GRACE_CUTOFF)
    assert [c.request_id for c in result] == [1]


def test_rank_eviction_candidates_excludes_no_view_defensively() -> None:
    candidates = [_candidate(watched=True, last_viewed_at=None)]
    assert rank_eviction_candidates(candidates, _GRACE_CUTOFF) == []


# --------------------------------------------------------------------------- #
# TV (per-season) candidates behave identically to movie candidates
# --------------------------------------------------------------------------- #


def test_tv_season_candidate_is_evicted_like_a_movie_candidate() -> None:
    season = _candidate(
        request_id=42,
        media_type="tv",
        title="Some Show",
        season=2,
        library_path="/media/tv/Some Show/Season 02",
    )
    result = select_evictions(
        [season],
        used_pct=95.0,
        threshold_pct=90.0,
        target_pct=0.0,
        grace_cutoff=_GRACE_CUTOFF,
    )
    assert result == [season]


def test_unwatched_season_is_never_evicted_while_a_watched_sibling_season_is() -> None:
    # Per-season eviction: a show with one watched, past-grace season and one
    # unwatched season must only ever surface the watched one.
    watched_season = _candidate(
        request_id=1, media_type="tv", title="Some Show", season=1, watched=True
    )
    unwatched_season = _candidate(
        request_id=2, media_type="tv", title="Some Show", season=2, watched=False
    )
    result = select_evictions(
        [unwatched_season, watched_season],
        used_pct=95.0,
        threshold_pct=90.0,
        target_pct=0.0,
        grace_cutoff=_GRACE_CUTOFF,
    )
    assert result == [watched_season]
