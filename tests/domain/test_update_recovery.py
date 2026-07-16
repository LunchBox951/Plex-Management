from datetime import UTC, datetime, timedelta

import pytest

from plex_manager.domain.update_recovery import (
    BUSY_COORDINATOR_PHASES,
    KNOWN_COORDINATOR_PHASES,
    KNOWN_REQUESTED_ACTIONS,
    RecoveryAction,
    RecoveryDecision,
    decide_recovery,
    dispatch_starts_work,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
MAX_AGE = timedelta(minutes=10)


@pytest.mark.parametrize(
    ("phase", "action", "drain", "started", "expected"),
    [
        ("future_phase", "install", False, None, RecoveryAction.REANCHOR),
        ("future_phase", "future_action", False, None, RecoveryAction.REANCHOR),
        ("future_phase", "install", True, None, RecoveryAction.LIVE_DRAIN),
        ("idle", "none", False, None, RecoveryAction.NOOP),
        ("idle", "future_action", False, None, RecoveryAction.ACTION_ONLY),
        ("idle", "future_action", True, None, RecoveryAction.LIVE_DRAIN),
        # Anchor age is the ONLY clock for a leaseless busy phase: an old
        # anchor recovers no matter how recently a merely-polling sidecar
        # refreshed its heartbeat (issue #368 -- polling must never extend the
        # gate), and a young anchor protects a genuinely in-flight operation.
        ("checking", "check", False, NOW - timedelta(hours=1), RecoveryAction.REANCHOR),
        ("checking", "none", False, NOW - MAX_AGE, RecoveryAction.REANCHOR),
        ("checking", "none", False, NOW - timedelta(minutes=5), RecoveryAction.WAIT),
        ("checking", "future_action", False, NOW - timedelta(minutes=5), RecoveryAction.WAIT),
        ("checking", "future_action", False, NOW - MAX_AGE, RecoveryAction.REANCHOR),
        ("draining", "install", False, NOW - MAX_AGE, RecoveryAction.REANCHOR),
        ("installing", "install", False, NOW - MAX_AGE, RecoveryAction.REANCHOR),
        ("rollback", "future_action", False, NOW - MAX_AGE, RecoveryAction.REANCHOR),
        ("rollback", "install", True, NOW - timedelta(hours=1), RecoveryAction.LIVE_DRAIN),
        ("checking", "check", False, None, RecoveryAction.WAIT),
        ("checking", "check", False, NOW + timedelta(seconds=1), RecoveryAction.WAIT),
        (
            "checking",
            "check",
            False,
            (NOW - MAX_AGE).replace(tzinfo=None),
            RecoveryAction.REANCHOR,
        ),
        (
            "checking",
            "check",
            False,
            NOW - MAX_AGE + timedelta(seconds=1),
            RecoveryAction.WAIT,
        ),
    ],
)
def test_recovery_matrix(
    phase: str,
    action: str,
    drain: bool,
    started: datetime | None,
    expected: RecoveryAction,
) -> None:
    decision = decide_recovery(
        phase=phase,
        requested_action=action,
        live_drain=drain,
        phase_started_at=started,
        now=NOW,
        max_age=MAX_AGE,
    )
    assert decision.action is expected
    if expected in {RecoveryAction.ACTION_ONLY, RecoveryAction.REANCHOR}:
        assert decision.clear_unknown_action is (action == "future_action")
        assert decision.preserve_known_action is (action != "future_action")


# --- The exhaustive recovery truth table -------------------------------------
#
# Every reachable (phase x requested_action x anchor x drain) combination is
# asserted below against an INDEPENDENTLY STATED specification, so any change
# to the recovery contract fails a named cell here rather than surfacing as a
# reviewer-found hole. The dimensions:
#
#   phase   -- every phase this build knows, plus one unrecognized string
#              (what a newer/rolled-back generation may have written).
#   action  -- "none" plus both real intents, plus one unrecognized string.
#   anchor  -- fresh (younger than the bound), aged (at/past the bound), and
#              NULL (the pre-anchor legacy-row shape).
#   drain   -- a LIVE lease or none. An EXPIRED lease is deliberately not a
#              domain-level state: ``_cleanup_expired`` runs under the same
#              lock BEFORE every recovery decision, sweeping the expired row
#              and re-anchoring a leased busy phase to idle, so by decision
#              time "expired" has already reduced to "absent" (asserted at the
#              service level by
#              test_force_reset_proceeds_once_the_drain_lease_has_expired and
#              test_refused_and_noop_force_resets_durably_commit_expired_drain_cleanup).
#
# Unreachable-by-construction combinations (still asserted, fail-closed):
#
#   * KNOWN NON-BUSY phase with a non-NULL anchor: every writer that lands a
#     non-busy phase clears ``last_started_at`` in the same statement
#     (``_phase_timestamp_values``). The decision must not (and does not)
#     depend on the anchor there -- asserted by identical outcomes across all
#     three anchor values.
#   * UNKNOWN phase with any anchor value: this build's writers never stamp an
#     anchor under a phase they don't know; a newer build's discipline is
#     unknowable. The decision therefore ignores the anchor entirely for
#     unknown phases -- again asserted by identical outcomes across anchors.
#
# The fence column is the round-3 rule, applied uniformly to KNOWN and UNKNOWN
# phases: any recovery that declares a worker abandoned without preserving a
# real queued action must bump the generation. ``fence_generation`` is what
# the repository executes as the CAS bump, so "late worker outcome with the
# pre-recovery generation is fenced" == this column (the CAS mechanics are
# integration-asserted by test_reanchor_generation_fence_matrix at the
# service level).

_TABLE_PHASES = [*sorted(KNOWN_COORDINATOR_PHASES), "future_phase"]
_TABLE_ACTIONS = ["none", "check", "install", "future_action"]
_TABLE_ANCHORS = {
    "fresh": NOW - timedelta(minutes=1),
    "aged": NOW - MAX_AGE,
    "null_legacy": None,
}


def _specified_cell(
    phase: str, action: str, anchor_kind: str, live_drain: bool
) -> tuple[RecoveryAction, bool]:
    """The recovery contract, stated independently of the implementation.

    Returns ``(decision, late_worker_fenced)`` for one cell. Rules, in
    priority order:

    1. A LIVE drain lease always wins: refuse, touch nothing, fence nothing --
       the lease TTL bounds the wait (#346 fail-closed install protection).
    2. An unrecognized phase is recoverable immediately (nothing this build
       runs can ever exit it); anchor evidence is ignored because no writer of
       this build stamps anchors it can't interpret.
    3. A recognized non-busy phase recovers nothing except an unrecognized
       action (the action-only reset); with a known action it is a no-op.
    4. A busy phase waits until its anchor is at/past the bound (fresh and
       NULL-legacy anchors both wait -- fail-closed; the repo durably
       backfills legacy anchors so the bound always arrives), then reanchors.
    5. Fencing: every action-only reset fences (it clears an unrecognized
       action a worker may still hold); a reanchor fences unless it preserves
       a real queued action ("check"/"install") for retry, in which case the
       generation is deliberately kept so the reconnecting sidecar can
       complete that exact action.
    """
    real_queued_action = action in KNOWN_REQUESTED_ACTIONS and action != "none"
    if live_drain:
        return RecoveryAction.LIVE_DRAIN, False
    if phase not in KNOWN_COORDINATOR_PHASES:
        return RecoveryAction.REANCHOR, not real_queued_action
    if phase not in BUSY_COORDINATOR_PHASES:
        if action not in KNOWN_REQUESTED_ACTIONS:
            return RecoveryAction.ACTION_ONLY, True
        return RecoveryAction.NOOP, False
    if anchor_kind == "aged":
        return RecoveryAction.REANCHOR, not real_queued_action
    return RecoveryAction.WAIT, False


def test_recovery_truth_table_is_exhaustive() -> None:
    cells = 0
    for phase in _TABLE_PHASES:
        for action in _TABLE_ACTIONS:
            for anchor_kind, anchor in _TABLE_ANCHORS.items():
                for live_drain in (True, False):
                    cells += 1
                    expected_action, expected_fence = _specified_cell(
                        phase, action, anchor_kind, live_drain
                    )
                    decision = decide_recovery(
                        phase=phase,
                        requested_action=action,
                        live_drain=live_drain,
                        phase_started_at=anchor,
                        now=NOW,
                        max_age=MAX_AGE,
                    )
                    cell = f"({phase}, {action}, {anchor_kind}, drain={live_drain})"
                    assert decision.action is expected_action, cell
                    assert decision.fence_generation is expected_fence, cell
                    # A fence is only ever executed by a recovery mutation.
                    if decision.fence_generation:
                        assert decision.action in {
                            RecoveryAction.REANCHOR,
                            RecoveryAction.ACTION_ONLY,
                        }, cell
                    # An unrecognized action is cleared exactly when a recovery
                    # mutation runs; a real queued action is preserved exactly
                    # when it is not fenced away with the reset.
                    if decision.action in {RecoveryAction.REANCHOR, RecoveryAction.ACTION_ONLY}:
                        assert decision.clear_unknown_action is (
                            action not in KNOWN_REQUESTED_ACTIONS
                        ), cell
                        complementary = (
                            decision.clear_unknown_action is not decision.preserve_known_action
                        )
                        assert complementary, cell
    assert cells == len(_TABLE_PHASES) * len(_TABLE_ACTIONS) * len(_TABLE_ANCHORS) * 2


@pytest.mark.parametrize(
    ("action", "blocker", "starts_work"),
    [
        # Every (action, blocker) shape the eligibility endpoint can emit,
        # asserted against whether the runner's guard lets work start. The
        # recovery-clock stamp uses this same predicate, so this table IS the
        # "stamp exactly when work starts" contract.
        ("none", None, False),
        ("none", "coordinator_state_unknown", False),
        ("none", "requested_action_unknown", False),
        ("none", "coordinator_phase_busy", False),
        ("none", "outside_update_window", False),
        ("check", None, True),
        # A queued install still awaiting its check: informational only, the
        # runner performs the check.
        ("check", "checking_for_update", True),
        ("install", None, True),
        # idle_only + active critical work: advisory, the runner returns
        # without Docker work or a drain claim -- no work starts, no stamp.
        ("install", "active_critical_work", False),
    ],
)
def test_dispatch_actionability_table(action: str, blocker: str | None, starts_work: bool) -> None:
    assert dispatch_starts_work(action, blocker) is starts_work


@pytest.mark.parametrize("phase", sorted(KNOWN_COORDINATOR_PHASES - BUSY_COORDINATOR_PHASES))
@pytest.mark.parametrize("action", _TABLE_ACTIONS)
def test_non_busy_and_unknown_phase_decisions_ignore_anchor_evidence(
    phase: str, action: str
) -> None:
    """The unreachable-cell guard: non-busy known phases never carry an anchor
    (writers clear it by construction) and unknown phases' anchors are
    uninterpretable, so in both regions the decision must be identical across
    every anchor value -- no hidden anchor dependence can develop."""
    outcomes: set[tuple[RecoveryAction, bool]] = set()
    for candidate in (phase, "future_phase"):
        per_phase: list[RecoveryDecision] = [
            decide_recovery(
                phase=candidate,
                requested_action=action,
                live_drain=False,
                phase_started_at=anchor,
                now=NOW,
                max_age=MAX_AGE,
            )
            for anchor in _TABLE_ANCHORS.values()
        ]
        assert len({(d.action, d.fence_generation) for d in per_phase}) == 1
        outcomes.add((per_phase[0].action, per_phase[0].fence_generation))
    assert len(outcomes) == 2  # known-non-busy vs unknown-phase regions differ
