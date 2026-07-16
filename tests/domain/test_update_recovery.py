from datetime import UTC, datetime, timedelta

import pytest

from plex_manager.domain.update_recovery import RecoveryAction, decide_recovery

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
