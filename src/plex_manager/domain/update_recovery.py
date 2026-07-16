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
    # Whether executing this recovery must bump ``action_generation`` to fence
    # a possibly-live abandoned worker. The rule, applied uniformly to KNOWN
    # and UNKNOWN phases alike: any recovery that declares a worker abandoned
    # WITHOUT preserving a real queued action (``check``/``install``) fences.
    # That is every ACTION_ONLY reset (it clears an unrecognized action) and
    # every REANCHOR whose requested_action is ``"none"`` or unrecognized; a
    # REANCHOR carrying a real queued action deliberately keeps the generation
    # so the reconnecting sidecar can still complete that exact action.
    fence_generation: bool


def dispatch_starts_work(action: str, blocker: str | None) -> bool:
    """Whether the sidecar runner will actually START work for this handout.

    The single source of truth shared by the runner's early-return guard
    (``plex_manager.updater.runner.UpdateRunner`` consumes eligibility with
    exactly this predicate) and the eligibility endpoint's work-dispatch
    anchor stamp, so the two can never drift: the recovery clock must restart
    exactly when work truly begins, and never for a handout the runner treats
    as advisory do-nothing.

    * ``action="none"`` hands out nothing.
    * ``action="install"`` with ANY blocker (e.g. ``active_critical_work``)
      is advisory: the runner returns without Docker work or a drain claim,
      so no work starts.
    * ``action="check"`` always starts a check -- its only blocker shape
      (``checking_for_update``, a queued install still awaiting its check) is
      informational and does not stop the runner.
    """
    if action == "none":
        return False
    return not (action == "install" and blocker is not None)


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
    live_drain: bool,
    phase_started_at: datetime | None,
    now: datetime,
    max_age: timedelta,
) -> RecoveryDecision:
    """Decide the recovery action for one observed coordinator shape.

    Work-in-flight evidence for a BUSY phase is deliberately limited to two
    signals that a merely-polling sidecar cannot refresh:

    * a live drain lease (its TTL bounds the wait), and
    * the bounded age of the phase's start anchor, which only moves on a real
      phase *transition* -- never on a same-phase heartbeat or eligibility
      poll.

    Heartbeat freshness is deliberately NOT evidence here: every eligibility
    poll refreshes ``updater_last_seen_at`` even when the app hands out no
    work (the fail-closed ``action="none"`` answer), so an idle sidecar that
    repolls forever would keep a wedged busy row permanently unrecoverable --
    an unbounded gate, which is exactly what issue #368 forbids. A genuinely
    in-flight operation is still protected: its start anchor is young for the
    full recovery window, and an install always holds a drain lease.
    """
    known_action = requested_action in KNOWN_REQUESTED_ACTIONS
    unknown_action = not known_action
    # A reanchor carries no real queued action to hand back to a sidecar when
    # the action is unrecognized OR absent ("none"); in both cases the worker
    # that owned the busy/unknown phase is being declared abandoned, so its
    # generation must be fenced -- regardless of whether the phase is known.
    reanchor_fences = unknown_action or requested_action == "none"
    if phase not in KNOWN_COORDINATOR_PHASES:
        if live_drain:
            return RecoveryDecision(
                RecoveryAction.LIVE_DRAIN, "live drain lease", unknown_action, known_action, False
            )
        return RecoveryDecision(
            RecoveryAction.REANCHOR,
            "unrecognized phase",
            unknown_action,
            known_action,
            reanchor_fences,
        )
    if live_drain:
        return RecoveryDecision(
            RecoveryAction.LIVE_DRAIN, "live drain lease", unknown_action, known_action, False
        )
    if phase not in BUSY_COORDINATOR_PHASES:
        if unknown_action:
            return RecoveryDecision(
                RecoveryAction.ACTION_ONLY, "unrecognized action", True, False, True
            )
        return RecoveryDecision(RecoveryAction.NOOP, "recognized state", False, True, False)
    if not _old_enough(phase_started_at, now, max_age):
        return RecoveryDecision(
            RecoveryAction.WAIT,
            "busy phase lacks bounded stale evidence",
            False,
            known_action,
            False,
        )
    return RecoveryDecision(
        RecoveryAction.REANCHOR,
        "busy phase is stale and old",
        unknown_action,
        known_action,
        reanchor_fences,
    )
