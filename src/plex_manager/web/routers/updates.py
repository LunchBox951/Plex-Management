"""Public update controls and the private sidecar coordination protocol."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.config import Settings, get_settings
from plex_manager.db import get_session, get_sessionmaker
from plex_manager.repositories.update_coordination import CoordinatorSnapshot
from plex_manager.services.update_coordination_service import (
    UpdateAction,
    UpdateCoordinationService,
    UpdatePhase,
    UpdateResult,
)
from plex_manager.services.update_policy import AutomaticUpdatePolicy, load_update_policy
from plex_manager.web.deps import require_admin
from plex_manager.web.errors import AppError
from plex_manager.web.events import current_build_id, publish_realtime
from plex_manager.web.schemas import (
    UpdateActionRequest,
    UpdateClaimResponse,
    UpdateEligibilityResponse,
    UpdateLeaseRequest,
    UpdateLeaseResponse,
    UpdateOutcomeRequest,
    UpdateResultItem,
    UpdateStatusResponse,
)
from plex_manager.web.updater_auth import require_updater

__all__ = ["internal_router", "router"]

_UPDATER_HEARTBEAT_MAX_AGE = timedelta(seconds=45)
_SIDE_CAR_POLL_SECONDS = 15
_AUTOMATIC_CHECK_INTERVAL = timedelta(minutes=15)
_DRAIN_TTL = timedelta(minutes=10)

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
    value = getattr(request.app.state, "update_coordinator", None)
    if isinstance(value, UpdateCoordinationService):
        return value
    maker_obj = getattr(request.app.state, "sessionmaker", None)
    maker: async_sessionmaker[AsyncSession] = (
        cast("async_sessionmaker[AsyncSession]", maker_obj)
        if isinstance(maker_obj, async_sessionmaker)
        else get_sessionmaker()
    )
    value = UpdateCoordinationService(maker)
    try:
        await value.initialize()
    except Exception as exc:
        raise AppError(
            status_code=503,
            code="updater_coordinator_unavailable",
            message="The update coordinator is not available.",
        ) from exc
    request.app.state.update_coordinator = value
    return value


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
        "install"
        if snapshot.last_from_build is not None or snapshot.last_to_build is not None
        else "check"
    )
    return UpdateResultItem(
        operation=operation,
        outcome=outcome,
        finished_at=snapshot.last_completed_at,
        from_build=snapshot.last_from_build,
        to_build=snapshot.last_to_build,
        detail_code=snapshot.last_error_code,
    )


def _state_and_blocker(
    snapshot: CoordinatorSnapshot,
    policy: AutomaticUpdatePolicy,
    *,
    updater_available: bool,
    now: datetime,
) -> tuple[str, str | None]:
    if not updater_available:
        return "unavailable", "updater_unavailable"
    if snapshot.phase == "draining":
        return "draining", "critical_work_draining" if snapshot.active_critical_operations else None
    if snapshot.phase == "installing":
        return "installing", None
    if snapshot.phase == "checking" or snapshot.requested_action == "check":
        return "checking", None
    if snapshot.phase == "failed":
        return "failed", snapshot.last_error_code
    if snapshot.phase == "rolled_back":
        return "failed", snapshot.last_error_code or "update_rolled_back"
    if snapshot.phase == "succeeded":
        return "succeeded", None
    if snapshot.requested_action == "install":
        if policy.idle_only and snapshot.active_critical_operations:
            return "waiting_for_idle", "active_critical_work"
        if snapshot.available_digest is not None:
            return "update_available", None
    if snapshot.available_digest is not None:
        if policy.schedule.enabled and not policy.schedule.is_open(now):
            return "waiting_for_window", "outside_update_window"
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
    if not coordinator.updater_available(snapshot, max_age=_UPDATER_HEARTBEAT_MAX_AGE):
        raise AppError(
            status_code=503,
            code="updater_unavailable",
            message="The updater sidecar is not connected.",
            hint="Enable the automatic-update Compose profile, then try again.",
        )
    await coordinator.request_action(action)
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


async def _eligibility(
    request: Request,
    session: AsyncSession,
    *,
    touch: bool,
) -> tuple[UpdateEligibilityResponse, CoordinatorSnapshot]:
    coordinator = await _coordinator(request)
    snapshot = await coordinator.snapshot()
    if touch:
        try:
            phase = UpdatePhase(snapshot.phase)
        except ValueError:
            phase = UpdatePhase.idle
        snapshot = await coordinator.heartbeat(
            phase=phase,
            current_build=snapshot.current_build or current_build_id(),
            current_digest=snapshot.current_digest,
            available_build=snapshot.available_build,
            available_digest=snapshot.available_digest,
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
        if snapshot.available_digest is not None:
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
    return (
        UpdateEligibilityResponse(
            action=action,
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


@internal_router.post("/claim")
async def claim_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    _body: Annotated[UpdateActionRequest | None, Body()] = None,
) -> UpdateClaimResponse:
    eligibility, snapshot = await _eligibility(request, session, touch=True)
    if eligibility.action != "install":
        raise AppError(
            status_code=409,
            code="update_not_eligible",
            message="No container update is currently eligible to install.",
        )
    if eligibility.idle_only and snapshot.active_critical_operations:
        return UpdateClaimResponse(
            ready=False,
            lease_seconds=int(_DRAIN_TTL.total_seconds()),
            blocker="active_critical_work",
        )
    claim = await (await _coordinator(request)).claim_drain(
        ttl=_DRAIN_TTL,
        action_generation=snapshot.action_generation,
    )
    if claim is None:
        return UpdateClaimResponse(
            ready=False,
            lease_seconds=int(_DRAIN_TTL.total_seconds()),
            blocker="concurrent_update_claim",
        )
    publish_realtime(request.app, ("updates",), reason="update_drain_claimed")
    return UpdateClaimResponse(
        lease_token=claim.lease.token,
        ready=claim.ready,
        lease_seconds=int(_DRAIN_TTL.total_seconds()),
        blocker=None if claim.ready else "critical_work_draining",
    )


@internal_router.post("/renew")
async def renew_endpoint(body: UpdateLeaseRequest, request: Request) -> UpdateLeaseResponse:
    coordinator = await _coordinator(request)
    if not await coordinator.renew(body.lease_token, ttl=_DRAIN_TTL):
        raise AppError(
            status_code=409,
            code="update_lease_expired",
            message="The update maintenance lease is no longer valid.",
        )
    ready = await coordinator.drain_ready(body.lease_token)
    if ready is None:
        raise AppError(
            status_code=409,
            code="update_lease_expired",
            message="The update maintenance lease is no longer valid.",
        )
    return UpdateLeaseResponse(
        ready=ready,
        lease_seconds=int(_DRAIN_TTL.total_seconds()),
        blocker=None if ready else "critical_work_draining",
    )


@internal_router.post("/release")
async def release_endpoint(body: UpdateLeaseRequest, request: Request) -> UpdateLeaseResponse:
    released = await (await _coordinator(request)).release(body.lease_token)
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
    result = _service_result(body.outcome)
    if body.operation == "check":
        phase = (
            UpdatePhase.available if result is UpdateResult.update_available else UpdatePhase.idle
        )
        if result is UpdateResult.failed:
            phase = UpdatePhase.failed
        await coordinator.heartbeat(
            phase=phase,
            current_build=body.current_build or snapshot.current_build or current_build_id(),
            current_digest=body.current_digest,
            available_build=body.available_build,
            available_digest=body.available_digest,
            checked=True,
        )
        if snapshot.requested_action == "check" or result is not UpdateResult.update_available:
            await coordinator.acknowledge_action(
                expected_generation=snapshot.action_generation,
                result=result,
                error_code=body.detail_code,
                current_build=body.current_build,
                current_digest=body.current_digest,
                available_build=body.available_build,
                available_digest=body.available_digest,
            )
    else:
        token = body.lease_token
        if token is None:  # schema validation guarantees this branch is unreachable
            raise AppError(
                status_code=400,
                code="missing_update_lease",
                message="An install outcome requires a lease.",
            )
        if not await coordinator.acknowledge_outcome(
            token,
            result=result,
            error_code=body.detail_code,
            from_build=body.from_build,
            to_build=body.to_build,
            current_build=body.current_build,
            current_digest=body.current_digest,
        ):
            raise AppError(
                status_code=409,
                code="update_lease_expired",
                message="The update outcome did not match an active maintenance lease.",
            )
        latest = await coordinator.snapshot()
        await coordinator.heartbeat(
            phase=UpdatePhase(latest.phase),
            current_build=body.current_build or latest.current_build,
            current_digest=body.current_digest or latest.current_digest,
            available_build=None,
            available_digest=None,
        )
    publish_realtime(request.app, ("updates",), reason=f"update_{body.outcome}")
    settings = get_settings()
    maker = request.app.state.sessionmaker
    async with maker() as session:
        return await _status(request, session, settings)
