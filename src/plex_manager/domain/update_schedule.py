"""Pure local-time scheduling for automatic container updates.

Windows are expressed in an explicit IANA timezone.  Overnight windows belong
to the weekday on which they start, so ``Monday 23:00-02:00`` remains open into
Tuesday morning without also selecting Tuesday.

Daylight-saving transitions are resolved from wall time to real instants rather
than by attaching a fixed UTC offset:

* a nonexistent wall time in a forward jump advances to the first valid minute;
* an ambiguous start uses the earliest occurrence and an ambiguous end uses the
  latest occurrence, keeping the whole repeated interval inside the window.

The module is deliberately stdlib-only and performs no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "ScheduleWindow",
    "UpdateSchedule",
    "Weekday",
]


class Weekday(StrEnum):
    """Canonical weekday names accepted by the update policy."""

    monday = "monday"
    tuesday = "tuesday"
    wednesday = "wednesday"
    thursday = "thursday"
    friday = "friday"
    saturday = "saturday"
    sunday = "sunday"

    @property
    def weekday_index(self) -> int:
        """Return :meth:`datetime.date.weekday`'s integer for this member."""
        return tuple(Weekday).index(self)


@dataclass(frozen=True)
class ScheduleWindow:
    """One concrete update window represented by timezone-aware instants."""

    start: datetime
    end: datetime
    starting_weekday: Weekday

    def contains(self, instant: datetime) -> bool:
        """Whether ``instant`` lies in the half-open ``[start, end)`` window."""
        normalized = _aware_utc(instant)
        return self.start.astimezone(UTC) <= normalized < self.end.astimezone(UTC)


@dataclass(frozen=True)
class UpdateSchedule:
    """Validated recurring local-time update policy."""

    enabled: bool
    timezone_name: str
    weekdays: frozenset[Weekday]
    window_start: time
    window_end: time

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone_name must be a valid IANA timezone") from exc
        if not self.weekdays:
            raise ValueError("at least one weekday must be selected")
        if self.window_start == self.window_end:
            raise ValueError("window_start and window_end must differ")
        for field_name, value in (
            ("window_start", self.window_start),
            ("window_end", self.window_end),
        ):
            if value.tzinfo is not None:
                raise ValueError(f"{field_name} must be a local wall time without tzinfo")
            if value.second or value.microsecond:
                raise ValueError(f"{field_name} must use minute precision")

    @property
    def timezone(self) -> ZoneInfo:
        """The validated timezone used to materialize recurring windows."""
        return ZoneInfo(self.timezone_name)

    @property
    def overnight(self) -> bool:
        """Whether a window ends on the day after its selected weekday."""
        return self.window_end < self.window_start

    def window_for_start_date(self, start_date: date) -> ScheduleWindow | None:
        """Materialize the window assigned to ``start_date``, if it is selected.

        A DST jump can erase an entire short wall-time interval.  In that case
        both boundaries resolve to the same instant and there is no real window
        to return for that date.
        """
        # Numeric mapping is locale-independent; ``strftime('%A')`` could return
        # a translated name on an operator's host and break an otherwise-valid
        # schedule.
        starting_weekday = tuple(Weekday)[start_date.weekday()]
        if starting_weekday not in self.weekdays:
            return None

        end_date = start_date + timedelta(days=1) if self.overnight else start_date
        start = _resolve_wall_time(
            datetime.combine(start_date, self.window_start), self.timezone, boundary="start"
        )
        end = _resolve_wall_time(
            datetime.combine(end_date, self.window_end), self.timezone, boundary="end"
        )
        if end.astimezone(UTC) <= start.astimezone(UTC):
            return None
        return ScheduleWindow(start=start, end=end, starting_weekday=starting_weekday)

    def current_window(self, now: datetime) -> ScheduleWindow | None:
        """Return the currently-open window, or ``None`` when closed/disabled."""
        if not self.enabled:
            return None
        now_utc = _aware_utc(now)
        local_date = now_utc.astimezone(self.timezone).date()
        # Yesterday is needed only for overnight windows, but checking it
        # unconditionally keeps this small and makes the start-day rule explicit.
        for candidate_date in (local_date - timedelta(days=1), local_date):
            window = self.window_for_start_date(candidate_date)
            if window is not None and window.contains(now_utc):
                return window
        return None

    def next_window(self, now: datetime) -> ScheduleWindow | None:
        """Return the open or next future window, or ``None`` when disabled.

        At least one weekday is required, so looking ahead eight local dates is
        sufficient to encounter a selected start day after considering yesterday
        for an already-open overnight window.
        """
        if not self.enabled:
            return None
        now_utc = _aware_utc(now)
        local_date = now_utc.astimezone(self.timezone).date()
        candidates: list[ScheduleWindow] = []
        for offset in range(-1, 9):
            window = self.window_for_start_date(local_date + timedelta(days=offset))
            if window is not None and window.end.astimezone(UTC) > now_utc:
                candidates.append(window)
        if not candidates:  # pragma: no cover - validation + eight-day horizon guarantee one
            return None
        return min(candidates, key=lambda item: item.start.astimezone(UTC))

    def is_open(self, now: datetime) -> bool:
        """Whether automatic installation is permitted at ``now``."""
        return self.current_window(now) is not None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _wall_candidates(naive: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """Return every real instant whose local representation equals ``naive``."""
    by_instant: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        instant = candidate.astimezone(UTC)
        round_trip = instant.astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            by_instant[instant] = candidate
    return tuple(by_instant[key] for key in sorted(by_instant))


def _resolve_wall_time(
    naive: datetime,
    zone: ZoneInfo,
    *,
    boundary: str,
) -> datetime:
    """Resolve one minute-precision local wall time across DST gaps/folds."""
    if naive.tzinfo is not None:
        raise ValueError("wall time must be naive")
    if boundary not in {"start", "end"}:  # pragma: no cover - internal callers are fixed
        raise ValueError("boundary must be start or end")

    # A civil-time discontinuity can be larger than an hour (historic timezone
    # changes include skipped dates), so allow up to two days before declaring the
    # timezone data unusable for this boundary.
    probe = naive
    for _minute in range((48 * 60) + 1):
        candidates = _wall_candidates(probe, zone)
        if candidates:
            if boundary == "start":
                return min(candidates, key=lambda value: value.astimezone(UTC))
            return max(candidates, key=lambda value: value.astimezone(UTC))
        probe += timedelta(minutes=1)
    raise ValueError("could not resolve local wall time in configured timezone")
