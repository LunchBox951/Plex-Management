"""Automatic-update scheduling across weekdays, overnight windows, and DST."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from plex_manager.domain.update_schedule import UpdateSchedule, Weekday


def _schedule(
    *,
    enabled: bool = True,
    timezone_name: str = "America/Toronto",
    weekdays: frozenset[Weekday] = frozenset(Weekday),
    start: time = time(3, 0),
    end: time = time(5, 0),
) -> UpdateSchedule:
    return UpdateSchedule(
        enabled=enabled,
        timezone_name=timezone_name,
        weekdays=weekdays,
        window_start=start,
        window_end=end,
    )


def test_disabled_schedule_has_no_current_or_next_window() -> None:
    schedule = _schedule(enabled=False)
    now = datetime(2026, 7, 13, 7, 0, tzinfo=UTC)
    assert schedule.current_window(now) is None
    assert schedule.next_window(now) is None
    assert schedule.is_open(now) is False


def test_window_is_half_open_and_next_returns_the_open_window() -> None:
    schedule = _schedule(timezone_name="UTC", weekdays=frozenset({Weekday.monday}))
    start = datetime(2026, 7, 13, 3, 0, tzinfo=UTC)
    middle = datetime(2026, 7, 13, 4, 0, tzinfo=UTC)
    end = datetime(2026, 7, 13, 5, 0, tzinfo=UTC)

    assert schedule.is_open(start)
    assert schedule.next_window(middle) == schedule.current_window(middle)
    assert not schedule.is_open(end)
    following = schedule.next_window(end)
    assert following is not None
    assert following.start == datetime(2026, 7, 20, 3, 0, tzinfo=UTC)


def test_overnight_window_belongs_to_its_starting_weekday() -> None:
    schedule = _schedule(
        timezone_name="UTC",
        weekdays=frozenset({Weekday.monday}),
        start=time(23, 0),
        end=time(2, 0),
    )

    tuesday_early = datetime(2026, 7, 14, 1, 30, tzinfo=UTC)
    assert schedule.is_open(tuesday_early)
    current = schedule.current_window(tuesday_early)
    assert current is not None
    assert current.start == datetime(2026, 7, 13, 23, 0, tzinfo=UTC)
    assert current.starting_weekday is Weekday.monday
    assert not schedule.is_open(datetime(2026, 7, 15, 1, 30, tzinfo=UTC))


def test_spring_gap_advances_start_to_first_real_local_minute() -> None:
    # Toronto jumps from 01:59 to 03:00 on 2026-03-08. A configured 02:30
    # boundary starts at 03:00, not at a fabricated fixed-offset instant.
    schedule = _schedule(start=time(2, 30), end=time(4, 0))
    window = schedule.next_window(datetime(2026, 3, 8, 0, 0, tzinfo=UTC))
    assert window is not None
    assert window.start.astimezone(UTC) == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert window.end.astimezone(UTC) == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
    assert schedule.is_open(datetime(2026, 3, 8, 7, 30, tzinfo=UTC))


def test_fall_fold_includes_both_occurrences_of_ambiguous_interval() -> None:
    # Toronto repeats 01:00-01:59 on 2026-11-01. An ambiguous start chooses the
    # first 01:30, while an ambiguous end chooses the second occurrence.
    schedule = _schedule(start=time(1, 30), end=time(1, 45))
    window = schedule.next_window(datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert window is not None
    assert window.start.astimezone(UTC) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert window.end.astimezone(UTC) == datetime(2026, 11, 1, 6, 45, tzinfo=UTC)
    assert schedule.is_open(datetime(2026, 11, 1, 6, 35, tzinfo=UTC))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timezone_name": "Not/A_Real_Zone"},
        {"weekdays": frozenset()},
        {"start": time(3, 0), "end": time(3, 0)},
        {"start": time(3, 0, 1)},
        {"start": time(3, 0, tzinfo=UTC)},
    ],
)
def test_invalid_policy_is_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _schedule(**kwargs)  # type: ignore[arg-type]


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _schedule().next_window(datetime(2026, 7, 13, 3, 0))
