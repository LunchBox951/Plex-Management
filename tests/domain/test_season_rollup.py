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
