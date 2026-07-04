"""Tests for the pure season-status -> parent-request rollup fold."""

from __future__ import annotations

import pytest

from plex_manager.domain.season_rollup import rollup_status

# -- precedence statuses win outright, regardless of the other seasons ---------

_PRECEDENCE = (
    "import_blocked",
    "downloading",
    "searching",
    "no_acceptable_release",
)


@pytest.mark.parametrize("winner", _PRECEDENCE)
def test_precedence_status_wins_outright_over_available(winner: str) -> None:
    assert rollup_status([winner, "available"]) == winner


@pytest.mark.parametrize("winner", _PRECEDENCE)
def test_precedence_status_wins_outright_over_pending_and_failed(winner: str) -> None:
    assert rollup_status(["pending", winner, "failed"]) == winner


@pytest.mark.parametrize("winner", _PRECEDENCE)
def test_precedence_status_wins_outright_over_a_completed_season(winner: str) -> None:
    # A needs-attention/in-flight season must win even when a sibling has finished
    # importing -- the show is not done while one season is still blocked/in flight.
    assert rollup_status(["completed", winner]) == winner


def test_precedence_order_import_blocked_beats_everything() -> None:
    # import_blocked is first in precedence; it must win even against another
    # precedence status.
    assert rollup_status(["import_blocked", "downloading"]) == "import_blocked"


def test_precedence_order_downloading_beats_searching_and_later() -> None:
    assert rollup_status(["downloading", "searching", "no_acceptable_release"]) == "downloading"


# -- completed is a DONE state, NOT a precedence winner -------------------------
# It must never outrank an unstarted/failed sibling: a terminal "completed" over a
# pending season both lies that the show is finished and blocks that season's grab.


def test_single_completed_season_is_completed() -> None:
    assert rollup_status(["completed"]) == "completed"


def test_all_done_all_completed_is_completed() -> None:
    assert rollup_status(["completed", "completed"]) == "completed"


def test_all_done_available_and_completed_is_completed() -> None:
    # Every season is done; some are Plex-confirmed (available), some still
    # finalizing (completed) -> the whole show is "completed" until the rest confirm.
    assert rollup_status(["available", "completed"]) == "completed"


def test_completed_mixed_with_pending_is_partially_available() -> None:
    # The regression guard: a finished S1 must NOT force the parent to terminal
    # "completed" while S2 is still pending (unstarted, and must stay grabbable).
    assert rollup_status(["completed", "pending"]) == "partially_available"


def test_completed_mixed_with_failed_is_partially_available() -> None:
    assert rollup_status(["completed", "failed"]) == "partially_available"


def test_completed_mixed_with_pending_and_failed_is_partially_available() -> None:
    assert rollup_status(["available", "completed", "pending", "failed"]) == "partially_available"


# -- no precedence status: available/pending/failed fold -----------------------


def test_all_available_is_available() -> None:
    assert rollup_status(["available"]) == "available"
    assert rollup_status(["available", "available"]) == "available"


def test_available_mixed_with_pending_is_partially_available() -> None:
    assert rollup_status(["available", "pending"]) == "partially_available"


def test_available_mixed_with_failed_is_partially_available() -> None:
    assert rollup_status(["available", "failed"]) == "partially_available"


def test_available_mixed_with_pending_and_failed_is_partially_available() -> None:
    assert rollup_status(["available", "pending", "failed"]) == "partially_available"


def test_any_pending_with_no_available_is_pending() -> None:
    assert rollup_status(["pending"]) == "pending"
    assert rollup_status(["pending", "failed"]) == "pending"
    assert rollup_status(["pending", "pending"]) == "pending"


def test_all_failed_is_failed() -> None:
    assert rollup_status(["failed"]) == "failed"
    assert rollup_status(["failed", "failed"]) == "failed"


def test_empty_input_raises() -> None:
    with pytest.raises(ValueError, match="at least one season status"):
        rollup_status([])


# -- evicted seasons (ADR-0012 disk-pressure sweep) -----------------------------


@pytest.mark.parametrize("winner", _PRECEDENCE)
def test_precedence_status_wins_outright_over_evicted(winner: str) -> None:
    assert rollup_status([winner, "evicted"]) == winner


def test_all_evicted_is_evicted() -> None:
    assert rollup_status(["evicted"]) == "evicted"
    assert rollup_status(["evicted", "evicted"]) == "evicted"


def test_evicted_mixed_with_available_is_partially_available() -> None:
    # Never "available": one season's file is actually gone, so reporting the
    # whole show as cleanly available would be dishonest.
    assert rollup_status(["available", "evicted"]) == "partially_available"


def test_evicted_mixed_with_completed_is_partially_available() -> None:
    assert rollup_status(["completed", "evicted"]) == "partially_available"


def test_evicted_mixed_with_available_and_pending_is_partially_available() -> None:
    assert rollup_status(["available", "evicted", "pending"]) == "partially_available"


# -- evicted with NO real-done (available/completed) season: never dishonestly
# "partially_available" -- nothing is actually watchable, so this folds evicted
# alongside failed and applies the ordinary pending/failed rule instead. -------


def test_evicted_mixed_with_pending_and_no_done_season_is_pending() -> None:
    # S1 evicted (file gone), S2 still pending: nothing is currently watchable,
    # but S2 might still complete -- "pending" is honest, "partially_available"
    # would not be (it implies something is available right now).
    assert rollup_status(["evicted", "pending"]) == "pending"


def test_evicted_mixed_with_failed_and_no_done_season_is_failed() -> None:
    # The regression this guards: S1 watched then evicted, S2 failed outright --
    # NOTHING is available, so this must never read "partially_available" (which
    # would render the show as watchable when it is not).
    assert rollup_status(["evicted", "failed"]) == "failed"


# -- cancelled seasons (ADR-0014 cancel verb) -----------------------------------
# ``cancelled`` folds identically to ``evicted`` for rollup purposes (both mean
# "nothing on disk for this season now"), EXCEPT all-cancelled rolls up to the
# settled ``cancelled`` (mirroring all-evicted -> evicted).


@pytest.mark.parametrize("winner", _PRECEDENCE)
def test_precedence_status_wins_outright_over_cancelled(winner: str) -> None:
    assert rollup_status([winner, "cancelled"]) == winner


def test_all_cancelled_is_cancelled() -> None:
    assert rollup_status(["cancelled"]) == "cancelled"
    assert rollup_status(["cancelled", "cancelled"]) == "cancelled"


def test_cancelled_mixed_with_available_is_partially_available() -> None:
    # Never "available": a cancelled season was never fetched, so reporting the
    # whole show as cleanly available would be dishonest.
    assert rollup_status(["available", "cancelled"]) == "partially_available"


def test_cancelled_mixed_with_completed_is_partially_available() -> None:
    assert rollup_status(["completed", "cancelled"]) == "partially_available"


def test_cancelled_mixed_with_pending_and_no_done_season_is_pending() -> None:
    assert rollup_status(["cancelled", "pending"]) == "pending"


def test_cancelled_mixed_with_failed_and_no_done_season_is_failed() -> None:
    assert rollup_status(["cancelled", "failed"]) == "failed"


def test_cancelled_and_evicted_mixed_with_no_done_season_is_failed() -> None:
    # Both "gone" statuses together, nothing watchable and nothing pending -> failed.
    assert rollup_status(["cancelled", "evicted"]) == "failed"
