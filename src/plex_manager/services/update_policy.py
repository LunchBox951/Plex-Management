"""Load and fail-safe the persisted automatic-update policy."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import time
from typing import Final, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.domain.update_schedule import UpdateSchedule, Weekday
from plex_manager.models import Setting

__all__ = [
    "AUTOMATIC_UPDATES_ENABLED_DEFAULT",
    "AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT",
    "AUTOMATIC_UPDATE_TIMEZONE_DEFAULT",
    "AUTOMATIC_UPDATE_WEEKDAYS_DEFAULT",
    "AUTOMATIC_UPDATE_WINDOW_END_DEFAULT",
    "AUTOMATIC_UPDATE_WINDOW_START_DEFAULT",
    "AutomaticUpdatePolicy",
    "ResolvedUpdatePolicy",
    "load_update_policy",
    "resolve_update_policy",
]

AUTOMATIC_UPDATES_ENABLED_DEFAULT: Final = False
AUTOMATIC_UPDATE_TIMEZONE_DEFAULT: Final = "UTC"
AUTOMATIC_UPDATE_WEEKDAYS_DEFAULT: Final[tuple[str, ...]] = tuple(day.value for day in Weekday)
AUTOMATIC_UPDATE_WINDOW_START_DEFAULT: Final = "03:00"
AUTOMATIC_UPDATE_WINDOW_END_DEFAULT: Final = "05:00"
AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT: Final = True

UPDATE_POLICY_SETTING_KEYS: Final[tuple[str, ...]] = (
    "automatic_updates_enabled",
    "automatic_update_timezone",
    "automatic_update_weekdays",
    "automatic_update_window_start",
    "automatic_update_window_end",
    "automatic_update_idle_only",
)

_TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "t", "y"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "f", "n"})


@dataclass(frozen=True)
class AutomaticUpdatePolicy:
    """The recurring schedule plus whether a claim may begin while work is active."""

    schedule: UpdateSchedule
    idle_only: bool


@dataclass(frozen=True)
class ResolvedUpdatePolicy:
    """A safe policy and the stored fields that were accepted verbatim."""

    policy: AutomaticUpdatePolicy
    honored_fields: frozenset[str]


def _bool(value: str | None, default: bool) -> tuple[bool, bool]:
    if value is None:
        return default, False
    token = value.strip().lower()
    if token in _TRUE_VALUES:
        return True, True
    if token in _FALSE_VALUES:
        return False, True
    return default, False


def _timezone(value: str | None) -> tuple[str, bool]:
    if value is None:
        return AUTOMATIC_UPDATE_TIMEZONE_DEFAULT, False
    token = value.strip()
    try:
        ZoneInfo(token)
    except (ValueError, ZoneInfoNotFoundError):
        return AUTOMATIC_UPDATE_TIMEZONE_DEFAULT, False
    return token, True


def _weekdays(value: str | None) -> tuple[frozenset[Weekday], bool]:
    default = frozenset(Weekday)
    if value is None:
        return default, False
    try:
        decoded: object = json.loads(value)
    except (TypeError, ValueError):
        return default, False
    if not isinstance(decoded, list) or not decoded:
        return default, False
    items = cast("list[object]", decoded)
    if not all(isinstance(item, str) for item in items):
        return default, False
    try:
        parsed = tuple(Weekday(cast(str, item)) for item in items)
    except ValueError:
        return default, False
    if len(parsed) != len(set(parsed)):
        return default, False
    return frozenset(parsed), True


def _wall_time(value: str | None, default: str) -> tuple[time, bool]:
    token = value.strip() if value is not None else ""
    if _TIME_RE.fullmatch(token) is None:
        token = default
        honored = False
    else:
        honored = True
    hour, minute = (int(part) for part in token.split(":"))
    return time(hour, minute), honored


def resolve_update_policy(raw: dict[str, str | None]) -> ResolvedUpdatePolicy:
    """Resolve corrupt/unset storage to the documented safe defaults."""
    honored: set[str] = set()
    enabled, enabled_ok = _bool(
        raw.get("automatic_updates_enabled"), AUTOMATIC_UPDATES_ENABLED_DEFAULT
    )
    if enabled_ok:
        honored.add("automatic_updates_enabled")
    idle_only, idle_ok = _bool(
        raw.get("automatic_update_idle_only"), AUTOMATIC_UPDATE_IDLE_ONLY_DEFAULT
    )
    if idle_ok:
        honored.add("automatic_update_idle_only")
    timezone_name, timezone_ok = _timezone(raw.get("automatic_update_timezone"))
    if timezone_ok:
        honored.add("automatic_update_timezone")
    weekdays, weekdays_ok = _weekdays(raw.get("automatic_update_weekdays"))
    if weekdays_ok:
        honored.add("automatic_update_weekdays")
    window_start, start_ok = _wall_time(
        raw.get("automatic_update_window_start"), AUTOMATIC_UPDATE_WINDOW_START_DEFAULT
    )
    window_end, end_ok = _wall_time(
        raw.get("automatic_update_window_end"), AUTOMATIC_UPDATE_WINDOW_END_DEFAULT
    )
    if window_start == window_end:
        window_start, _ = _wall_time(None, AUTOMATIC_UPDATE_WINDOW_START_DEFAULT)
        window_end, _ = _wall_time(None, AUTOMATIC_UPDATE_WINDOW_END_DEFAULT)
        start_ok = end_ok = False
    if start_ok:
        honored.add("automatic_update_window_start")
    if end_ok:
        honored.add("automatic_update_window_end")
    return ResolvedUpdatePolicy(
        policy=AutomaticUpdatePolicy(
            schedule=UpdateSchedule(
                enabled=enabled,
                timezone_name=timezone_name,
                weekdays=weekdays,
                window_start=window_start,
                window_end=window_end,
            ),
            idle_only=idle_only,
        ),
        honored_fields=frozenset(honored),
    )


async def load_update_policy(session: AsyncSession) -> AutomaticUpdatePolicy:
    """Read the six policy rows in one query and return the fail-safe policy."""
    result = await session.execute(
        select(Setting).where(Setting.key.in_(UPDATE_POLICY_SETTING_KEYS))
    )
    raw: dict[str, str | None] = dict.fromkeys(UPDATE_POLICY_SETTING_KEYS)
    for row in result.scalars():
        raw[row.key] = row.value
    return resolve_update_policy(raw).policy
