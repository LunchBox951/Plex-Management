"""Pure evidence matrix for updater coordinator recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

KNOWN_COORDINATOR_PHASES = frozenset(
    {
        "idle",
        "checking",
        "available",
        "draining",
        "installing",
        "rollback",
        "succeeded",
        "failed",
        "rolled_back",
    }
)
KNOWN_REQUESTED_ACTIONS = frozenset({"none", "check", "install"})
BUSY_COORDINATOR_PHASES = frozenset({"checking", "draining", "installing", "rollback"})


class RecoveryAction(StrEnum):
    NOOP = "noop"
    WAIT = "wait"
    LIVE_DRAIN = "live_drain"
    ACTION_ONLY = "action_only"
    REANCHOR = "reanchor"


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    reason: str
    clear_unknown_action: bool
    preserve_known_action: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _old_enough(anchor: datetime | None, now: datetime, max_age: timedelta) -> bool:
    if anchor is None:
        return False
    age = _utc(now) - _utc(anchor)
    return timedelta(0) <= age and age >= max_age


def decide_recovery(
    *,
    phase: str,
    requested_action: str,
    updater_heartbeat_fresh: bool,
    live_drain: bool,
    phase_started_at: datetime | None,
    now: datetime,
    max_age: timedelta,
) -> RecoveryDecision:
    known_action = requested_action in KNOWN_REQUESTED_ACTIONS
    unknown_action = not known_action
    if phase not in KNOWN_COORDINATOR_PHASES:
        if live_drain:
            return RecoveryDecision(
                RecoveryAction.LIVE_DRAIN, "live drain lease", unknown_action, known_action
            )
        return RecoveryDecision(
            RecoveryAction.REANCHOR, "unrecognized phase", unknown_action, known_action
        )
    if live_drain:
        return RecoveryDecision(
            RecoveryAction.LIVE_DRAIN, "live drain lease", unknown_action, known_action
        )
    if phase not in BUSY_COORDINATOR_PHASES:
        if unknown_action:
            return RecoveryDecision(RecoveryAction.ACTION_ONLY, "unrecognized action", True, False)
        return RecoveryDecision(RecoveryAction.NOOP, "recognized state", False, True)
    if updater_heartbeat_fresh:
        return RecoveryDecision(
            RecoveryAction.WAIT, "updater heartbeat is fresh", False, known_action
        )
    if not _old_enough(phase_started_at, now, max_age):
        return RecoveryDecision(
            RecoveryAction.WAIT, "busy phase lacks bounded stale evidence", False, known_action
        )
    return RecoveryDecision(
        RecoveryAction.REANCHOR, "busy phase is stale and old", unknown_action, known_action
    )
