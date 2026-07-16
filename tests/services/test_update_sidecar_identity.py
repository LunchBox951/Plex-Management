"""ADR-0025 stage 0: sidecar identity + self-refresh persistence and surfacing."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.repositories.update_coordination import UnknownCoordinatorPhaseError
from plex_manager.services.update_coordination_service import (
    UpdateAction,
    UpdateCoordinationService,
    UpdatePhase,
    UpdateResult,
)

SessionMaker = async_sessionmaker[AsyncSession]


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def _tokens(prefix: str = "lease") -> Callable[[], str]:
    counter = 0

    def next_token() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}-{counter}"

    return next_token


async def test_record_updater_identity_persists_and_surfaces(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()

    # Absent by default -- the expected pre-stage-1 state.
    snapshot = await service.snapshot()
    assert snapshot.updater_observed_build is None
    assert snapshot.updater_observed_digest is None

    await service.record_updater_identity(
        observed_build="sidecar-build",
        observed_digest="sha256:" + "a" * 64,
    )
    snapshot = await service.snapshot()
    assert snapshot.updater_observed_build == "sidecar-build"
    assert snapshot.updater_observed_digest == "sha256:" + "a" * 64


async def test_identity_write_never_touches_phase_or_action(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.check)

    await service.record_updater_identity(observed_build="b", observed_digest="d")

    snapshot = await service.snapshot()
    # The no-coordination-decision invariant: liveness identity leaves the
    # queued action and its generation exactly as they were.
    assert snapshot.requested_action == "check"
    assert snapshot.action_generation == generation
    assert snapshot.phase == "idle"


async def test_failed_refresh_record_survives_later_identity_writes(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()

    await service.record_refresh_outcome(
        result="failed",
        detail_code="successor_never_pinged",
        from_build="old",
        to_build="new",
    )
    # A surviving predecessor keeps heartbeating; ordinary identity writes must
    # NOT mask the durable failure (north star #3).
    await service.record_updater_identity(observed_build="old", observed_digest="sha256:old")

    snapshot = await service.snapshot()
    assert snapshot.last_refresh_result == "failed"
    assert snapshot.last_refresh_detail_code == "successor_never_pinged"
    assert snapshot.last_refresh_from_build == "old"
    assert snapshot.last_refresh_to_build == "new"
    assert snapshot.last_refresh_at is not None


async def test_refresh_outcome_updates_prior_record(sessionmaker_: SessionMaker) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await service.record_refresh_outcome(result="failed", detail_code="socket_check_failed")
    await service.record_refresh_outcome(result="succeeded")

    snapshot = await service.snapshot()
    assert snapshot.last_refresh_result == "succeeded"
    assert snapshot.last_refresh_detail_code is None


async def test_identity_write_fails_closed_on_unknown_phase(
    sessionmaker_: SessionMaker,
) -> None:
    clock = MutableClock(datetime(2026, 7, 16, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()
    # Drive the row into a phase this build does not know (a version-skew wedge).
    async with sessionmaker_() as session:
        from sqlalchemy import update

        from plex_manager.models import UpdateCoordinatorState

        await session.execute(update(UpdateCoordinatorState).values(phase="future_unknown_phase"))
        await session.commit()

    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.record_updater_identity(observed_build="b", observed_digest="d")
    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.record_refresh_outcome(result="failed")


async def test_refresh_result_must_be_bounded_code(sessionmaker_: SessionMaker) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    with pytest.raises(ValueError, match="bounded lowercase code"):
        await service.record_refresh_outcome(result="Not A Code!")


async def test_ordinary_check_outcome_does_not_clear_refresh_record(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await service.record_refresh_outcome(result="failed", detail_code="successor_never_pinged")

    generation = await service.request_action(UpdateAction.check)
    assert await service.acknowledge_action(
        expected_generation=generation,
        result=UpdateResult.no_update,
    )

    snapshot = await service.snapshot()
    # The app/target check outcome (last_result) is independent of the sidecar's
    # own self-refresh record (last_refresh_*): the failure stays visible.
    assert snapshot.last_refresh_result == "failed"
    assert snapshot.phase == UpdatePhase.idle.value
