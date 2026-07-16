"""Public update controls and the private sidecar coordination protocol."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.config import Settings, get_settings
from plex_manager.db import get_session
from plex_manager.domain.update_recovery import (
    BUSY_COORDINATOR_PHASES,
    KNOWN_REQUESTED_ACTIONS,
    RecoveryAction,
    decide_recovery,
    dispatch_starts_work,
)
from plex_manager.repositories.update_coordination import CoordinatorSnapshot
from plex_manager.services.update_coordination_service import (
    COORDINATOR_RECOVERY_MAX_AGE,
    UPDATER_HEARTBEAT_MAX_AGE,
    CoordinatorRecoveryNotReadyError,
    DrainLeaseActiveError,
    UnknownCoordinatorPhaseError,
    UpdateAction,
    UpdateCoordinationService,
    UpdateOperationInProgressError,
    UpdatePhase,
    UpdateResult,
)
from plex_manager.services.update_policy import AutomaticUpdatePolicy, load_update_policy
from plex_manager.web.deps import AuthContext, require_admin
from plex_manager.web.errors import AppError
from plex_manager.web.events import current_build_id, publish_realtime
from plex_manager.web.schemas import (
    UpdateActionRequest,
    UpdateClaimRequest,
    UpdateClaimResponse,
    UpdateEligibilityResponse,
    UpdateHeartbeatRequest,
    UpdateLeaseRequest,
    UpdateLeaseResponse,
    UpdateOutcomeRequest,
    UpdateRefreshItem,
    UpdateRefreshOutcomeRequest,
    UpdateRenewRequest,
    UpdateResultItem,
    UpdateStatusResponse,
)
from plex_manager.web.update_coordinator import ensure_update_coordinator
from plex_manager.web.updater_auth import require_updater

__all__ = ["internal_router", "router"]

# The liveness contract lives in the coordination service; this alias keeps
# the router's ``updater_available`` call sites while guaranteeing the two
# cannot drift. It gates AVAILABILITY only -- recovery decisions deliberately
# ignore heartbeat freshness (see ``decide_recovery``), because eligibility
# polls refresh it even when no work is handed out.
_UPDATER_HEARTBEAT_MAX_AGE = UPDATER_HEARTBEAT_MAX_AGE
_SIDE_CAR_POLL_SECONDS = 15
_AUTOMATIC_CHECK_INTERVAL = timedelta(minutes=15)
_DRAIN_TTL = timedelta(minutes=10)
_COORDINATOR_RECOVERY_MAX_AGE = COORDINATOR_RECOVERY_MAX_AGE
_KNOWN_PHASES = frozenset(phase.value for phase in UpdatePhase)

router = APIRouter(
    prefix="/api/v1/updates",
    tags=["updates"],
    dependencies=[Depends(require_admin)],
)
internal_router = APIRouter(
    prefix="/api/v1/internal/updates",
    tags=["internal-updates"],
    dependencies=[Depends(require_updater)],
)


async def _coordinator(request: Request) -> UpdateCoordinationService:
    try:
        return await ensure_update_coordinator(request.app)
    except Exception as exc:
        raise AppError(
            status_code=503,
            code="updater_coordinator_unavailable",
            message="The update coordinator is not available.",
        ) from exc


async def _guard_unknown_phase[T](operation: Awaitable[T]) -> T:
    """Translate a locked repo write's fail-closed unknown-phase refusal to a 409.

    Each endpoint below already fast-paths a 409 from its own pre-call
    ``snapshot()`` check (below); this covers the narrow TOCTOU window where the
    row's phase changes AFTER that snapshot but before the locked write commits
    (issue #322) by catching the identical guard the repository now enforces
    inside its own lock, and rendering it exactly like the fast path does.
    """
    try:
        return await operation
    except UnknownCoordinatorPhaseError as exc:
        raise AppError(
            status_code=409,
            code="coordinator_state_unknown",
            message="The update coordinator is in an unrecognized state.",
        ) from exc


def _channel(image: str) -> str:
    if "@" in image:
        return "digest"
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else "latest"


def _last_result(snapshot: CoordinatorSnapshot) -> UpdateResultItem | None:
    if snapshot.last_result is None or snapshot.last_completed_at is None:
        return None
    outcomes: dict[
        str, Literal["no_update", "update_available", "succeeded", "failed", "rolled_back"]
    ] = {
        "no_update": "no_update",
        "update_available": "update_available",
        "success": "succeeded",
        "failed": "failed",
        "rolled_back": "rolled_back",
        "cancelled": "failed",
    }
    outcome = outcomes.get(snapshot.last_result)
    if outcome is None:
        return None
    operation: Literal["check", "install"] = (
        "install" if snapshot.last_operation == "install" else "check"
    )
    return UpdateResultItem(
        operation=operation,
        outcome=outcome,
        finished_at=snapshot.last_completed_at,
        from_build=snapshot.last_from_build,
        to_build=snapshot.last_to_build,
        detail_code=snapshot.last_error_code,
    )


def _refresh_item(snapshot: CoordinatorSnapshot) -> UpdateRefreshItem | None:
    """The durable self-refresh record, or ``None`` when none was ever recorded."""
    if snapshot.last_refresh_result is None:
        return None
    return UpdateRefreshItem(
        result=snapshot.last_refresh_result,
        detail_code=snapshot.last_refresh_detail_code,
        from_build=snapshot.last_refresh_from_build,
        to_build=snapshot.last_refresh_to_build,
        at=snapshot.last_refresh_at,
    )


# Runtime build ids that cannot distinguish one build from another: the empty
# string, and the unstamped package-version fallback a dev/source run reports
# (the promote gate stamps a real version at release; ``current_build_id``'s
# env var is CI-injected into every published image). Comparing against these
# could fabricate a match OR a mismatch, so both read as unknown instead.
_UNSTAMPED_RUNTIME_BUILDS = frozenset({"", "0.0.0"})


def _updater_build_matches(snapshot: CoordinatorSnapshot) -> bool | None:
    """Whether the sidecar runs the same build as the RUNNING app (ADR-0025 stage 0).

    The comparison anchor is deliberately the running process's OWN build
    identifier (``current_build_id()``: the CI-baked ``PLEX_MANAGER_BUILD_ID``,
    falling back to the release-stamped package version) and NOT the
    coordinator row's ``current_build``/``current_digest`` (Codex round 1 on
    PR #384): those columns record the last TARGET observation a check/install
    reported, which an operator rollback leaves pointing at an image the
    process is no longer running -- a sidecar on N+1 would then read as a
    clean match while the app actually runs N.

    Honest limitation, documented rather than papered over: the app holds no
    Docker authority (ADR-0025 C2), so its own RUNNING image digest is
    unknowable from inside the container -- the comparison is build-id-only,
    and a same-build-id rebuild with different bytes reads as a match. The
    observed digest is still persisted and surfaced verbatim for the operator
    and for stage 1 (which compares digests sidecar-side, where the socket
    lives -- Q3).

    R2: an ABSENT observed identity (the sidecar has not reported it -- the
    expected state until the stage-1 emitting sidecar ships) is ``None``
    ("unknown / refresh recommended"), NEVER a clean ``True``. The same
    ``None`` covers an UNSTAMPED runtime id (a dev/source run's ``0.0.0``
    fallback): it cannot distinguish builds, so no match or mismatch is
    fabricated from it. R3: a present-but-different build id is ``False``
    ("version mismatch") with NO direction -- C7 lets the sidecar run a build
    AHEAD of an app that rolled back, so this never asserts older/newer.
    """
    observed_build = snapshot.updater_observed_build
    if observed_build is None:
        return None
    runtime_build = current_build_id()
    if runtime_build in _UNSTAMPED_RUNTIME_BUILDS:
        return None
    return observed_build == runtime_build


def _state_and_blocker(
    snapshot: CoordinatorSnapshot,
    policy: AutomaticUpdatePolicy,
    *,
    updater_available: bool,
    now: datetime,
) -> tuple[str, str | None]:
    phase_started_at = (
        snapshot.last_started_at or snapshot.requested_at
        if snapshot.phase in BUSY_COORDINATOR_PHASES
        else None
    )
    decision = decide_recovery(
        phase=snapshot.phase,
        requested_action=snapshot.requested_action,
        live_drain=snapshot.drain_owner is not None,
        phase_started_at=phase_started_at,
        now=now,
        max_age=_COORDINATOR_RECOVERY_MAX_AGE,
    )
    if decision.action is RecoveryAction.LIVE_DRAIN and (
        snapshot.phase not in _KNOWN_PHASES
        or snapshot.requested_action not in KNOWN_REQUESTED_ACTIONS
    ):
        return "unavailable", "coordinator_drain_active"
    if decision.action is RecoveryAction.WAIT and (
        snapshot.requested_action not in KNOWN_REQUESTED_ACTIONS or not updater_available
    ):
        return "unavailable", "coordinator_recovery_not_ready"
    if decision.action is RecoveryAction.REANCHOR and snapshot.phase not in _KNOWN_PHASES:
        return "unavailable", "coordinator_state_unknown"
    if decision.action is RecoveryAction.ACTION_ONLY:
        return "unavailable", "requested_action_unknown"
    if decision.action is RecoveryAction.REANCHOR:
        return "unavailable", "coordinator_state_stale"
    if not updater_available:
        return "unavailable", "updater_unavailable"
    if snapshot.phase == "draining":
        return "draining", "critical_work_draining" if snapshot.active_critical_operations else None
    if snapshot.phase == "installing":
        return "installing", None
    if snapshot.phase == "rollback":
        return "rollback", None
    if snapshot.phase == "checking" or snapshot.requested_action == "check":
        return "checking", None
    if snapshot.phase == "failed":
        return "failed", snapshot.last_error_code
    if snapshot.phase == "rolled_back":
        return "failed", snapshot.last_error_code or "update_rolled_back"
    if snapshot.phase == "succeeded":
        return "succeeded", None
    if snapshot.requested_action == "install":
        if snapshot.available_digest is None:
            return "checking", "checking_for_update"
        if policy.idle_only and snapshot.active_critical_operations:
            return "waiting_for_idle", "active_critical_work"
        return "update_available", None
    if snapshot.available_digest is not None:
        if policy.schedule.enabled and not policy.schedule.is_open(now):
            return "waiting_for_window", "outside_update_window"
        if policy.schedule.enabled and policy.idle_only and snapshot.active_critical_operations:
            return "waiting_for_idle", "active_critical_work"
        return "update_available", None
    if not policy.schedule.enabled:
        return "disabled", "automatic_updates_disabled"
    return "idle", None


async def _status(
    request: Request,
    session: AsyncSession,
    settings: Settings,
) -> UpdateStatusResponse:
    coordinator = await _coordinator(request)
    snapshot = await coordinator.snapshot()
    policy = await load_update_policy(session)
    now = datetime.now(UTC)
    available = coordinator.updater_available(snapshot, max_age=_UPDATER_HEARTBEAT_MAX_AGE)
    state, blocker = _state_and_blocker(snapshot, policy, updater_available=available, now=now)
    window = policy.schedule.next_window(now)
    return UpdateStatusResponse(
        state=state,  # type: ignore[arg-type]
        updater_available=available,
        current_build=snapshot.current_build or current_build_id(),
        current_digest=snapshot.current_digest,
        available_build=snapshot.available_build,
        available_digest=snapshot.available_digest,
        channel=_channel(settings.image),
        next_window_start=window.start if window is not None else None,
        next_window_end=window.end if window is not None else None,
        blocker=blocker,
        last_checked_at=snapshot.last_checked_at,
        last_result=_last_result(snapshot),
        updater_build_matches_app=_updater_build_matches(snapshot),
        updater_observed_build=snapshot.updater_observed_build,
        updater_observed_digest=snapshot.updater_observed_digest,
        last_refresh=_refresh_item(snapshot),
    )


@router.get("/status")
async def update_status_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> UpdateStatusResponse:
    return await _status(request, session, settings)


async def _request_action(
    action: UpdateAction,
    request: Request,
    session: AsyncSession,
    settings: Settings,
) -> UpdateStatusResponse:
    coordinator = await _coordinator(request)
    snapshot = await coordinator.snapshot()
    if snapshot.phase not in _KNOWN_PHASES:
        raise AppError(
            status_code=409,
            code="coordinator_state_unknown",
            message="The update coordinator is in an unrecognized state.",
        )
    if not coordinator.updater_available(snapshot, max_age=_UPDATER_HEARTBEAT_MAX_AGE):
        raise AppError(
            status_code=503,
            code="updater_unavailable",
            message="The updater sidecar is not connected.",
            hint="Enable the automatic-update Compose profile, then try again.",
        )
    # ``snapshot.phase not in _KNOWN_PHASES`` was fast-pathed above; this covers
    # the TOCTOU window where the phase turns unknown between that snapshot and
    # the locked ``request_action`` write (issue #322).
    try:
        await _guard_unknown_phase(coordinator.request_action(action))
    except UpdateOperationInProgressError as exc:
        raise AppError(
            status_code=409,
            code="update_operation_in_progress",
            message="An update operation is already in progress.",
        ) from exc
    publish_realtime(request.app, ("updates",), reason=f"update_{action.value}_requested")
    return await _status(request, session, settings)


@router.post("/check-now")
async def check_now_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    _body: Annotated[UpdateActionRequest | None, Body()] = None,
) -> UpdateStatusResponse:
    return await _request_action(UpdateAction.check, request, session, settings)


@router.post("/update-when-ready")
async def update_when_ready_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    _body: Annotated[UpdateActionRequest | None, Body()] = None,
) -> UpdateStatusResponse:
    return await _request_action(UpdateAction.install, request, session, settings)


@router.post("/force-reset")
async def force_reset_endpoint(
    request: Request,
    auth: Annotated[AuthContext, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    _body: Annotated[UpdateActionRequest | None, Body()] = None,
) -> UpdateStatusResponse:
    """Break-glass recovery for a wedged coordinator state (issue #354).

    The in-app exit from the fail-closed unknown-state guards (PR #346): a
    version-skew/rollback window can leave the coordinator row in a PHASE this
    app version does not know (every locked coordination write then 409s
    permanently), or in a known phase paired with a queued ACTION it does not
    know (every new check/install request is then refused as in-progress).
    Both are recovered here -- audited, inside the coordination lock, and only
    after re-reading the row so a state that healed on its own is never blindly
    clobbered (north stars #1/#2: a button, never a terminal).

    The locked decision matrix (see ``decide_recovery``):

    * Known non-busy phase + known action: 409 ``coordinator_phase_known`` --
      a true no-op, nothing wedged to recover. Also the honest, idempotent
      answer to a double-click: the second call finds the recovered state
      already known.
    * ANY shape under an UNEXPIRED drain lease: 409
      ``coordinator_drain_active`` -- an updater generation may be
      legitimately mid-install; the lease TTL bounds the wait, and an expired
      lease is swept (durably, even on a refused attempt) so a retry then
      proceeds.
    * Known non-busy phase + unrecognized action: clears the action to
      ``none`` under a bumped generation (the action-only reset), leaving the
      phase untouched.
    * BUSY phase whose start anchor is younger than the recovery bound: 409
      ``coordinator_recovery_not_ready`` -- the operation could genuinely be
      in flight. The anchor is the ONLY clock: heartbeat freshness is
      deliberately not evidence, because eligibility polls refresh it even
      when no work is handed out, so a merely-polling sidecar can never
      extend this gate (issue #368). Legacy busy rows with no anchor get one
      durably backfilled on first observation -- including by this refusal
      itself -- so the clock always starts and the bound always arrives.
    * BUSY phase past the bound, or an unknown phase: re-anchors to ``idle``.
      A KNOWN queued action is preserved (with its generation) for retry; an
      unrecognized or absent one has its generation bumped, fencing any late
      worker still holding the old generation.
    """
    coordinator = await _coordinator(request)
    try:
        result = await coordinator.force_reset_coordinator_phase(
            actor_user_id=auth.user_id,
            recovery_max_age=_COORDINATOR_RECOVERY_MAX_AGE,
        )
    except CoordinatorRecoveryNotReadyError as exc:
        raise AppError(
            status_code=409,
            code="coordinator_recovery_not_ready",
            message="Recovery is waiting for bounded stale evidence; try again shortly.",
        ) from exc
    except DrainLeaseActiveError as exc:
        raise AppError(
            status_code=409,
            code="coordinator_drain_active",
            message="An update maintenance lease is still active; the update may be mid-flight.",
            hint="Wait for the lease to expire (a few minutes), then try again.",
        ) from exc
    if result is None:
        raise AppError(
            status_code=409,
            code="coordinator_phase_known",
            message="The update coordinator is in a recognized state; there is nothing to recover.",
        )
    publish_realtime(request.app, ("updates",), reason="update_coordinator_force_reset")
    return await _status(request, session, settings)


async def _eligibility(
    request: Request,
    session: AsyncSession,
    *,
    touch: bool,
) -> tuple[UpdateEligibilityResponse, CoordinatorSnapshot]:
    coordinator = await _coordinator(request)
    if touch:
        # This endpoint is legacy/pre-identity: neither ``/eligibility`` nor
        # ``/claim`` ever carries a sidecar-reported build/digest, so an
        # authenticated contact through here is definitionally an
        # identity-less beat. Clear any previously stored identity FIRST,
        # mirroring the heartbeat endpoint's absence-clears contract (Codex
        # round 1 on PR #384) -- otherwise a sidecar that reported identity
        # and is later replaced/downgraded by one whose idle loop only calls
        # ``coordinator.eligibility()`` (``updater/runner.py``) would keep
        # liveness looking fresh via the touch below while
        # ``updater_observed_*`` still describes the dead container, so
        # status keeps asserting a stale match/mismatch. Best-effort, exactly
        # like the touch below: an unknown-phase refusal here is the SAME
        # soft condition the ``except ValueError`` further down already
        # reports as ``coordinator_state_unknown`` for this polling endpoint,
        # so swallow it rather than hard-failing the poll. This write touches
        # only the identity columns -- never ``updater_last_seen_at`` -- so it
        # adds no second pre-snapshot write of the heartbeat anchor:
        # ``touch_updater`` below now backfills the legacy busy-row anchor
        # BEFORE its own liveness write under the same lock (issue #387), so
        # there is only ever the one heartbeat write to reason about here.
        with contextlib.suppress(UnknownCoordinatorPhaseError):
            await coordinator.record_updater_identity(observed_build=None, observed_digest=None)
        # A locked-write refusal here is the SAME unknown-phase condition the
        # ``except ValueError`` below already reports as a soft
        # ``coordinator_state_unknown`` blocker for this polling endpoint -- fall
        # back to reading the (untouched) row instead of hard-failing the poll.
        try:
            touched = await coordinator.touch_updater()
        except UnknownCoordinatorPhaseError:
            touched = None
        snapshot = touched if touched is not None else await coordinator.snapshot()
    else:
        snapshot = await coordinator.snapshot()
    try:
        UpdatePhase(snapshot.phase)
    except ValueError:
        policy = await load_update_policy(session)
        now = datetime.now(UTC)
        return (
            UpdateEligibilityResponse(
                action="none",
                action_generation=snapshot.action_generation,
                automatic_enabled=policy.schedule.enabled,
                window_open=policy.schedule.is_open(now),
                idle_only=policy.idle_only,
                blocker="coordinator_state_unknown",
                poll_after_seconds=_SIDE_CAR_POLL_SECONDS,
            ),
            snapshot,
        )
    # Fail closed on an unrecognized requested_action, exactly like the
    # unknown-phase branch above (issue #354, Codex round 3). Without this, an
    # action a newer generation queued reads as absent to the policy branches
    # below, so an older sidecar with automatic updates enabled would be handed
    # a check -- or an install, with an image available -- whose completion
    # then rewrites requested_action OVER the state it cannot interpret,
    # bypassing the audited force-reset recovery path entirely. Withhold all
    # work (action="none" is the sidecar's ordinary sleep-and-repoll answer,
    # per the #346 fail-closed contract) until the operator runs the audited
    # action-only reset.
    if snapshot.requested_action not in KNOWN_REQUESTED_ACTIONS:
        policy = await load_update_policy(session)
        now = datetime.now(UTC)
        return (
            UpdateEligibilityResponse(
                action="none",
                action_generation=snapshot.action_generation,
                automatic_enabled=policy.schedule.enabled,
                window_open=policy.schedule.is_open(now),
                idle_only=policy.idle_only,
                blocker="requested_action_unknown",
                poll_after_seconds=_SIDE_CAR_POLL_SECONDS,
            ),
            snapshot,
        )
    policy = await load_update_policy(session)
    now = datetime.now(UTC)
    window_open = policy.schedule.is_open(now)
    action: Literal["none", "check", "install"] = "none"
    blocker: str | None = None
    if snapshot.requested_action == "check":
        action = "check"
    elif snapshot.requested_action == "install":
        action = "install" if snapshot.available_digest is not None else "check"
        if action == "check":
            blocker = "checking_for_update"
    elif policy.schedule.enabled:
        # AUTOMATIC dispatch only ever starts from a recognized NON-BUSY
        # phase. A busy phase means either an operation is genuinely in
        # flight (the single sidecar never polls eligibility mid-operation --
        # its loop is sequential and mid-check liveness goes through the
        # generation-bound /heartbeat) or the row is stale and must age out
        # into recovery (automatic sweep or the operator button) first.
        # Without this gate, a crash-looping sidecar (poll -> handed an
        # automatic check -> dies -> repolls) would restamp the recovery
        # anchor every window and keep a stuck busy row perpetually
        # unrecoverable -- exactly the unbounded wedge issue #368 forbids.
        # MANUAL queued intent (the requested_action branches above) is
        # deliberately NOT gated: a queued action is preserved across
        # recovery anyway, and its handout legitimately restarts the clock.
        if snapshot.phase in BUSY_COORDINATOR_PHASES:
            blocker = "coordinator_phase_busy"
        elif snapshot.available_digest is not None:
            if window_open:
                action = "install"
            else:
                blocker = "outside_update_window"
        elif (
            snapshot.last_checked_at is None
            or now - snapshot.last_checked_at >= _AUTOMATIC_CHECK_INTERVAL
        ):
            action = "check"
    if action == "install" and policy.idle_only and snapshot.active_critical_operations:
        blocker = "active_critical_work"
    if (
        touch
        and dispatch_starts_work(action, blocker)
        and snapshot.phase in BUSY_COORDINATOR_PHASES
    ):
        # Handing real work to the sidecar over a row already in a busy phase
        # (necessarily a MANUAL queued action -- automatic dispatch is gated
        # on a non-busy phase above) is a genuine work-START even though the
        # sidecar's subsequent same-phase heartbeat cannot move the age
        # anchor. Restart the recovery clock here, in the handout, so an
        # operator button press cannot fence the work that was just
        # dispatched. ``dispatch_starts_work`` is the SAME predicate the
        # runner's early-return guard derives from, so the stamp fires
        # exactly when the runner will act: no-work polls and advisory
        # blocked-install answers (idle_only + active critical work) never
        # move the anchor.
        await coordinator.mark_busy_work_dispatched()
    return (
        UpdateEligibilityResponse(
            action=action,
            action_generation=snapshot.action_generation,
            automatic_enabled=policy.schedule.enabled,
            window_open=window_open,
            idle_only=policy.idle_only,
            blocker=blocker,
            poll_after_seconds=_SIDE_CAR_POLL_SECONDS,
        ),
        snapshot,
    )


@internal_router.post("/eligibility")
async def eligibility_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _body: Annotated[UpdateActionRequest | None, Body()] = None,
) -> UpdateEligibilityResponse:
    eligibility, _snapshot = await _eligibility(request, session, touch=True)
    return eligibility


@internal_router.post("/heartbeat")
async def heartbeat_endpoint(body: UpdateHeartbeatRequest, request: Request) -> UpdateLeaseResponse:
    coordinator = await _coordinator(request)
    before = await coordinator.snapshot()
    if before.phase not in _KNOWN_PHASES:
        raise AppError(
            status_code=409,
            code="coordinator_state_unknown",
            message="The update coordinator is in an unrecognized state.",
        )
    # ADR-0025 stage 0 (issue #299): identity is PER-HEARTBEAT self-description.
    # Persist exactly what this authenticated beat carried -- and when it
    # carried no identity, CLEAR any previously stored one (Codex round 1): a
    # replaced/downgraded sidecar that doesn't report identity (every
    # pre-stage-1 sidecar) must never inherit its predecessor's stored
    # build/digest, or the match state would keep asserting the DEAD
    # container's identity. Absence honestly reverts to unknown
    # (``updater_build_matches_app=None`` -> no banner). Runs BEFORE the
    # CAS-guarded phase touch below: the identity claim is orthogonal to the
    # check-generation CAS, so a stale-generation ``checking`` beat still
    # updates/clears identity even though its phase touch is refused.
    # ``/eligibility`` and ``/claim`` also clear (never set) identity on their
    # own liveness touch, per ``_eligibility`` above -- this remains the ONLY
    # endpoint that can ever WRITE a non-``None`` observed build/digest, since
    # it is the only body shape that carries one.
    await _guard_unknown_phase(
        coordinator.record_updater_identity(
            observed_build=body.updater_build,
            observed_digest=body.updater_digest,
        )
    )
    if body.phase is None:
        # The phase-less liveness heartbeat (the C7 expand direction): refresh
        # ``updater_last_seen_at`` only -- deliberately NEVER rewrite the
        # coordinator phase (the no-phase-writes invariant). ``touch_updater``
        # with no phase/generation is an unconditional liveness write.
        await _guard_unknown_phase(coordinator.touch_updater())
        return UpdateLeaseResponse(
            ready=False,
            lease_seconds=int(_DRAIN_TTL.total_seconds()),
            blocker=None,
        )
    # The pre-expand ``checking`` heartbeat: unchanged CAS-guarded phase touch.
    # The schema validator guarantees ``action_generation`` is present here.
    after = await _guard_unknown_phase(
        coordinator.touch_updater(
            phase=UpdatePhase(body.phase),
            expected_generation=body.action_generation,
        )
    )
    if after is None:
        raise AppError(
            status_code=409,
            code="update_action_generation_mismatch",
            message="The update check no longer matches the pending action.",
        )
    if before.phase != after.phase:
        publish_realtime(request.app, ("updates",), reason="update_checking")
    return UpdateLeaseResponse(
        ready=False,
        lease_seconds=int(_DRAIN_TTL.total_seconds()),
        blocker=None,
    )


@internal_router.post("/claim")
async def claim_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[UpdateClaimRequest | None, Body()] = None,
) -> UpdateClaimResponse:
    coordinator = await _coordinator(request)
    if body is not None and body.recovery:
        snapshot = await coordinator.snapshot()
        if snapshot.phase not in _KNOWN_PHASES:
            raise AppError(
                status_code=409,
                code="coordinator_state_unknown",
                message="The update coordinator is in an unrecognized state.",
            )
        if (
            snapshot.requested_action != "install"
            or snapshot.action_generation != body.expected_generation
        ):
            raise AppError(
                status_code=409,
                code="update_recovery_generation_mismatch",
                message="The interrupted update no longer matches the pending action.",
            )
        policy = await load_update_policy(session)
        eligibility = UpdateEligibilityResponse(
            action="install",
            action_generation=snapshot.action_generation,
            automatic_enabled=policy.schedule.enabled,
            window_open=policy.schedule.is_open(datetime.now(UTC)),
            idle_only=policy.idle_only,
            blocker=None,
            poll_after_seconds=_SIDE_CAR_POLL_SECONDS,
        )
    else:
        eligibility, snapshot = await _eligibility(request, session, touch=True)
        if (
            body is not None
            and body.expected_generation is not None
            and body.expected_generation != snapshot.action_generation
        ):
            raise AppError(
                status_code=409,
                code="update_action_generation_mismatch",
                message="The update claim no longer matches the pending action.",
            )
    if eligibility.action != "install":
        raise AppError(
            status_code=409,
            code="update_not_eligible",
            message="No container update is currently eligible to install.",
        )
    if eligibility.idle_only and snapshot.active_critical_operations:
        return UpdateClaimResponse(
            action_generation=snapshot.action_generation,
            ready=False,
            lease_seconds=int(_DRAIN_TTL.total_seconds()),
            blocker="active_critical_work",
        )
    claim = await _guard_unknown_phase(
        (await _coordinator(request)).claim_drain(
            ttl=_DRAIN_TTL,
            action_generation=snapshot.action_generation,
            materialize_install=(
                not (body is not None and body.recovery) and snapshot.requested_action == "none"
            ),
            require_idle=eligibility.idle_only,
        )
    )
    if claim is None:
        latest = await (await _coordinator(request)).snapshot()
        blocker = (
            "active_critical_work"
            if eligibility.idle_only and latest.active_critical_operations
            else "concurrent_update_claim"
        )
        return UpdateClaimResponse(
            action_generation=latest.action_generation,
            ready=False,
            lease_seconds=int(_DRAIN_TTL.total_seconds()),
            blocker=blocker,
        )
    publish_realtime(request.app, ("updates",), reason="update_drain_claimed")
    return UpdateClaimResponse(
        lease_token=claim.lease.token,
        action_generation=claim.lease.action_generation,
        ready=claim.ready,
        lease_seconds=int(_DRAIN_TTL.total_seconds()),
        blocker=None if claim.ready else "critical_work_draining",
    )


@internal_router.post("/renew")
async def renew_endpoint(body: UpdateRenewRequest, request: Request) -> UpdateLeaseResponse:
    coordinator = await _coordinator(request)
    before = await coordinator.snapshot()
    if before.phase not in _KNOWN_PHASES:
        raise AppError(
            status_code=409,
            code="coordinator_state_unknown",
            message="The update coordinator is in an unrecognized state.",
        )
    phase = UpdatePhase(body.phase) if body.phase is not None else None
    ready = await _guard_unknown_phase(
        coordinator.renew_drain_progress(
            body.lease_token,
            ttl=_DRAIN_TTL,
            phase=phase,
        )
    )
    if ready is None:
        raise AppError(
            status_code=409,
            code="update_lease_expired",
            message="The update maintenance lease is no longer valid.",
        )
    if body.phase is not None:
        after = await coordinator.snapshot()
        if before.phase != after.phase:
            publish_realtime(request.app, ("updates",), reason=f"update_{body.phase}")
    return UpdateLeaseResponse(
        ready=ready,
        lease_seconds=int(_DRAIN_TTL.total_seconds()),
        blocker=None if ready else "critical_work_draining",
    )


@internal_router.post("/release")
async def release_endpoint(body: UpdateLeaseRequest, request: Request) -> UpdateLeaseResponse:
    coordinator = await _coordinator(request)
    snapshot = await coordinator.snapshot()
    if snapshot.phase not in _KNOWN_PHASES:
        raise AppError(
            status_code=409,
            code="coordinator_state_unknown",
            message="The update coordinator is in an unrecognized state.",
        )
    released = await _guard_unknown_phase(coordinator.release(body.lease_token))
    if released:
        publish_realtime(request.app, ("updates",), reason="update_drain_released")
    return UpdateLeaseResponse(
        ready=False,
        lease_seconds=int(_DRAIN_TTL.total_seconds()),
        blocker=None if released else "lease_not_found",
    )


def _service_result(outcome: str) -> UpdateResult:
    mapping = {
        "no_update": UpdateResult.no_update,
        "update_available": UpdateResult.update_available,
        "succeeded": UpdateResult.success,
        "failed": UpdateResult.failed,
        "rolled_back": UpdateResult.rolled_back,
    }
    return mapping[outcome]


@internal_router.post("/outcome")
async def outcome_endpoint(body: UpdateOutcomeRequest, request: Request) -> UpdateStatusResponse:
    coordinator = await _coordinator(request)
    snapshot = await coordinator.snapshot()
    if snapshot.phase not in _KNOWN_PHASES:
        raise AppError(
            status_code=409,
            code="coordinator_state_unknown",
            message="The update coordinator is in an unrecognized state.",
        )
    result = _service_result(body.outcome)
    if body.operation == "check":
        has_update = result is UpdateResult.update_available
        acknowledged = await _guard_unknown_phase(
            coordinator.acknowledge_action(
                expected_generation=body.action_generation,
                result=result,
                error_code=body.detail_code,
                current_build=body.current_build or snapshot.current_build or current_build_id(),
                current_digest=(
                    body.current_digest
                    if body.current_digest is not None
                    else snapshot.current_digest
                ),
                available_build=body.available_build if has_update else None,
                available_digest=body.available_digest if has_update else None,
                preserve_action=(
                    snapshot.requested_action == "install"
                    and result is UpdateResult.update_available
                ),
            )
        )
        if not acknowledged:
            raise AppError(
                status_code=409,
                code="update_action_generation_mismatch",
                message="The update check no longer matches the pending action.",
            )
    else:
        token = body.lease_token
        if token is None:  # schema validation guarantees this branch is unreachable
            raise AppError(
                status_code=400,
                code="missing_update_lease",
                message="An install outcome requires a lease.",
            )
        outcome_acknowledged = await _guard_unknown_phase(
            coordinator.acknowledge_outcome(
                token,
                result=result,
                expected_generation=body.action_generation,
                error_code=body.detail_code,
                from_build=body.from_build,
                to_build=body.to_build,
                current_build=body.current_build,
                current_digest=body.current_digest,
                available_build=body.available_build,
                available_digest=body.available_digest,
            )
        )
        if not outcome_acknowledged:
            raise AppError(
                status_code=409,
                code="update_lease_expired",
                message="The update outcome did not match an active maintenance lease.",
            )
    publish_realtime(request.app, ("updates",), reason=f"update_{body.outcome}")
    settings = get_settings()
    maker = request.app.state.sessionmaker
    async with maker() as session:
        return await _status(request, session, settings)


@internal_router.post("/refresh-outcome")
async def refresh_outcome_endpoint(
    body: UpdateRefreshOutcomeRequest, request: Request
) -> UpdateStatusResponse:
    """Record the sidecar's self-refresh outcome (ADR-0025 stage 0, issue #299).

    The accept side of the stage-1 self-refresh ladder, shipped WITH the C7
    expand release (Codex round 1 on PR #384): without this route, the first
    emitting sidecar's failure report would 404 against an app that already
    accepts its heartbeats, and -- because a surviving predecessor keeps
    heartbeating and looks healthy -- the failed refresh would be masked
    entirely (north star #3). Same sidecar-secret auth as every other internal
    updater endpoint; nothing emits it in stage 0. The record is durable: only
    a later refresh outcome overwrites it (ordinary heartbeats never do -- see
    ``record_refresh_outcome``), and the status endpoint surfaces it as
    ``last_refresh``.
    """
    coordinator = await _coordinator(request)
    snapshot = await coordinator.snapshot()
    if snapshot.phase not in _KNOWN_PHASES:
        raise AppError(
            status_code=409,
            code="coordinator_state_unknown",
            message="The update coordinator is in an unrecognized state.",
        )
    await _guard_unknown_phase(
        coordinator.record_refresh_outcome(
            result=body.result,
            detail_code=body.detail_code,
            from_build=body.from_build,
            to_build=body.to_build,
        )
    )
    # ``body.result`` is pattern-bounded lowercase, so the reason stays a
    # bounded code (never free text) exactly like the other publish reasons.
    publish_realtime(request.app, ("updates",), reason=f"updater_refresh_{body.result}")
    settings = get_settings()
    maker = request.app.state.sessionmaker
    async with maker() as session:
        return await _status(request, session, settings)
