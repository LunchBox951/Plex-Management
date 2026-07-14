"""Database-backed drain/critical leases and durable updater action state."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from plex_manager.db import Base, enable_sqlite_fk_enforcement
from plex_manager.models import AuditLog, MaintenanceLease, UpdateCoordinatorState
from plex_manager.repositories.update_coordination import (
    _BUSY_COORDINATOR_PHASES,  # pyright: ignore[reportPrivateUsage]
    _KNOWN_COORDINATOR_PHASES,  # pyright: ignore[reportPrivateUsage]
    _KNOWN_REQUESTED_ACTIONS,  # pyright: ignore[reportPrivateUsage]
)
from plex_manager.services.update_coordination_service import (
    DrainLeaseActiveError,
    MaintenanceDrainingError,
    MaintenanceLeaseLostError,
    UnknownCoordinatorPhaseError,
    UpdateAction,
    UpdateCoordinationService,
    UpdateOperationInProgressError,
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


async def test_check_action_cas_updates_builds_without_drain_lease(
    sessionmaker_: SessionMaker,
) -> None:
    clock = MutableClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()

    generation = await service.request_action(UpdateAction.check)
    assert generation == 1
    assert not await service.acknowledge_action(
        expected_generation=0,
        result=UpdateResult.no_update,
        current_build="stale",
    )

    acknowledged = await service.acknowledge_action(
        expected_generation=generation,
        result=UpdateResult.update_available,
        current_build="build-old",
        current_digest="sha256:old",
        available_build="build-new",
        available_digest="sha256:new",
    )
    assert acknowledged
    snapshot = await service.snapshot()
    assert snapshot.requested_action == "none"
    assert snapshot.acknowledged_generation == generation
    assert snapshot.phase == "available"
    assert snapshot.current_build == "build-old"
    assert snapshot.current_digest == "sha256:old"
    assert snapshot.available_build == "build-new"
    assert snapshot.available_digest == "sha256:new"
    assert snapshot.drain_owner is None


async def test_check_ack_with_omitted_image_fields_preserves_previous_observation(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    assert await service.acknowledge_action(
        expected_generation=0,
        result=UpdateResult.update_available,
        current_build="build-a",
        current_digest="sha256:a",
        available_build="build-b",
        available_digest="sha256:b",
    )
    generation = await service.request_action(UpdateAction.check)
    assert await service.acknowledge_action(
        expected_generation=generation,
        result=UpdateResult.update_available,
    )
    snapshot = await service.snapshot()
    assert snapshot.current_build == "build-a"
    assert snapshot.current_digest == "sha256:a"
    assert snapshot.available_build == "build-b"
    assert snapshot.available_digest == "sha256:b"


async def test_drain_blocks_new_work_until_existing_critical_lease_releases(
    sessionmaker_: SessionMaker,
) -> None:
    clock = MutableClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.install)

    critical = await service.acquire_critical("import", ttl=timedelta(minutes=1))
    assert critical is not None
    drain = await service.claim_drain(ttl=timedelta(minutes=1), action_generation=generation)
    assert drain is not None
    assert not drain.ready
    assert await service.claim_drain(ttl=timedelta(minutes=1)) is None
    assert await service.acquire_critical("grab", ttl=timedelta(minutes=1)) is None
    assert await service.renew("wrong-token", ttl=timedelta(minutes=1)) is False

    assert await service.release(critical.token)
    assert await service.renew_drain_progress(drain.lease.token, ttl=timedelta(minutes=1)) is True
    assert not await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation + 1,
        result=UpdateResult.success,
    )
    assert await service.renew_drain_progress(drain.lease.token, ttl=timedelta(minutes=1)) is True

    assert await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation,
        result=UpdateResult.success,
        from_build="build-old",
        to_build="build-new",
        current_build="build-new",
        current_digest="sha256:new",
    )
    snapshot = await service.snapshot()
    assert snapshot.drain_owner is None
    assert snapshot.last_result == "success"
    assert snapshot.last_from_build == "build-old"
    assert snapshot.last_to_build == "build-new"
    assert snapshot.current_build == "build-new"
    assert snapshot.acknowledged_generation == generation
    # Lost-response recovery: the exact same acknowledgement is idempotent even
    # though the first commit deleted the drain lease.
    assert await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation,
        result=UpdateResult.success,
        from_build="build-old",
        to_build="build-new",
        current_build="build-new",
        current_digest="sha256:new",
    )
    assert not await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation,
        result=UpdateResult.failed,
    )
    assert not await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation,
        result=UpdateResult.success,
        from_build="different-old",
        to_build="build-new",
        current_build="build-new",
        current_digest="sha256:new",
    )
    assert await service.release(drain.lease.token) is False


async def test_outcome_round_trips_long_private_registry_repo_digest(
    sessionmaker_: SessionMaker,
) -> None:
    """A digest for a >183-char repo path exceeds 255 but must still round-trip.

    Docker reports a pulled image as ``<repository>@sha256:<64 hex>``. The updater
    accepts image references up to 255 chars, so a long private-registry repo
    path yields a RepoDigest longer than the old 255-char digest bounds. It must
    reach durable storage instead of failing closed (issue #298).
    """
    long_repo = "registry.internal.example.com/" + "team/" * 40 + "plex-manager"
    assert len(long_repo) > 183
    long_digest = f"{long_repo}@sha256:{'a' * 64}"
    # The digest exceeds the historical 255-char bound but stays within the new one.
    assert 255 < len(long_digest) <= 400

    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.install)
    drain = await service.claim_drain(ttl=timedelta(minutes=1), action_generation=generation)
    assert drain is not None

    assert await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation,
        result=UpdateResult.success,
        from_build="build-old",
        to_build="build-new",
        current_build=long_digest,
        current_digest=long_digest,
    )
    snapshot = await service.snapshot()
    assert snapshot.current_build == long_digest
    assert snapshot.current_digest == long_digest


@pytest.mark.parametrize(
    "phase",
    [UpdatePhase.checking, UpdatePhase.draining, UpdatePhase.installing, UpdatePhase.rollback],
)
async def test_active_update_rejects_new_actions_without_invalidating_outcome(
    sessionmaker_: SessionMaker,
    phase: UpdatePhase,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    first_action = UpdateAction.check if phase is UpdatePhase.checking else UpdateAction.install
    generation = await service.request_action(first_action)
    assert generation is not None

    drain_token: str | None = None
    if phase is UpdatePhase.checking:
        assert (
            await service.touch_updater(
                phase=UpdatePhase.checking,
                expected_generation=generation,
            )
            is not None
        )
    else:
        drain = await service.claim_drain(
            ttl=timedelta(minutes=1),
            action_generation=generation,
        )
        assert drain is not None
        drain_token = drain.lease.token
        if phase is not UpdatePhase.draining:
            assert (
                await service.renew_drain_progress(
                    drain_token,
                    ttl=timedelta(minutes=1),
                    phase=phase,
                )
                is True
            )

    with pytest.raises(UpdateOperationInProgressError):
        await service.request_action(UpdateAction.check)
    with pytest.raises(UpdateOperationInProgressError):
        await service.request_action(UpdateAction.install)
    snapshot = await service.snapshot()
    assert snapshot.action_generation == generation
    assert snapshot.requested_action == first_action.value

    if phase is UpdatePhase.checking:
        assert await service.acknowledge_action(
            expected_generation=generation,
            result=UpdateResult.no_update,
        )
    else:
        assert drain_token is not None
        result = UpdateResult.rolled_back if phase is UpdatePhase.rollback else UpdateResult.success
        assert await service.acknowledge_outcome(
            drain_token,
            expected_generation=generation,
            result=result,
        )
        assert (await service.snapshot()).drain_owner is None


async def test_queued_install_is_not_clobbered_by_a_later_public_action(
    sessionmaker_: SessionMaker,
) -> None:
    # A prefetch check found an update and left an install queued (phase
    # "available", requested_action "install") awaiting the sidecar's claim.
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    assert await service.acknowledge_action(
        expected_generation=0,
        result=UpdateResult.update_available,
        available_digest="sha256:new",
    )
    queued = await service.request_action(UpdateAction.install)
    assert queued is not None
    snapshot = await service.snapshot()
    assert snapshot.phase == "available"
    assert snapshot.requested_action == "install"

    # A later check-now/update-when-ready must neither overwrite the queued
    # install nor bump the generation: doing so strands the drain lease the
    # sidecar is about to claim against ``queued``.
    with pytest.raises(UpdateOperationInProgressError):
        await service.request_action(UpdateAction.check)
    with pytest.raises(UpdateOperationInProgressError):
        await service.request_action(UpdateAction.install)
    after = await service.snapshot()
    assert after.requested_action == "install"
    assert after.action_generation == queued

    # The queued generation still claims cleanly and acknowledges its outcome.
    claim = await service.claim_drain(ttl=timedelta(minutes=1), action_generation=queued)
    assert claim is not None
    assert await service.acknowledge_outcome(
        claim.lease.token,
        expected_generation=queued,
        result=UpdateResult.success,
    )


async def test_queued_check_is_not_clobbered_before_the_sidecar_claims_it(
    sessionmaker_: SessionMaker,
) -> None:
    # A manual check is queued but not yet polled by the sidecar (phase "idle").
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    queued = await service.request_action(UpdateAction.check)
    assert queued is not None
    snapshot = await service.snapshot()
    assert snapshot.phase == "idle"
    assert snapshot.requested_action == "check"

    with pytest.raises(UpdateOperationInProgressError):
        await service.request_action(UpdateAction.install)
    with pytest.raises(UpdateOperationInProgressError):
        await service.request_action(UpdateAction.check)
    after = await service.snapshot()
    assert after.requested_action == "check"
    assert after.action_generation == queued

    # The original check still acknowledges against its own untouched generation.
    assert await service.acknowledge_action(
        expected_generation=queued,
        result=UpdateResult.no_update,
    )


async def test_drain_claim_rejects_a_stale_action_generation(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    stale = await service.request_action(UpdateAction.check)
    assert stale is not None
    # Complete the check so the action slot frees up; only then can a new install
    # be requested. Stacking two un-acknowledged actions is now refused outright.
    assert await service.acknowledge_action(
        expected_generation=stale,
        result=UpdateResult.no_update,
    )
    current = await service.request_action(UpdateAction.install)
    assert current == stale + 1

    assert (
        await service.claim_drain(
            ttl=timedelta(minutes=1),
            action_generation=stale,
        )
        is None
    )
    assert (await service.snapshot()).drain_owner is None


async def test_idle_only_claim_atomically_refuses_active_critical_work(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    critical = await service.acquire_critical("import")
    assert critical is not None

    assert await service.claim_drain(ttl=timedelta(minutes=1), require_idle=True) is None
    assert (await service.snapshot()).drain_owner is None

    draining = await service.claim_drain(ttl=timedelta(minutes=1), require_idle=False)
    assert draining is not None and not draining.ready


async def test_check_result_clears_install_history_and_old_receipt(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.install)
    drain = await service.claim_drain(ttl=timedelta(minutes=1), action_generation=generation)
    assert drain is not None
    assert await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation,
        result=UpdateResult.success,
        from_build="old",
        to_build="new",
    )

    check_generation = await service.request_action(UpdateAction.check)
    assert await service.acknowledge_action(
        expected_generation=check_generation,
        result=UpdateResult.no_update,
    )
    snapshot = await service.snapshot()
    assert snapshot.last_operation == "check"
    assert snapshot.last_from_build is None
    assert snapshot.last_to_build is None
    assert not await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation,
        result=UpdateResult.success,
        from_build="old",
        to_build="new",
    )


async def test_prefetch_check_records_result_without_consuming_install_intent(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.install)
    assert await service.acknowledge_action(
        expected_generation=generation,
        result=UpdateResult.update_available,
        available_digest="sha256:new",
        preserve_action=True,
    )
    snapshot = await service.snapshot()
    assert snapshot.requested_action == "install"
    assert snapshot.acknowledged_generation == 0
    assert snapshot.last_operation == "check"
    assert snapshot.last_result == "update_available"
    assert snapshot.last_completed_at is not None


async def test_expired_leases_are_cleaned_for_crash_recovery(
    sessionmaker_: SessionMaker,
) -> None:
    clock = MutableClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()

    critical = await service.acquire_critical("eviction", ttl=timedelta(seconds=10))
    assert critical is not None
    clock.advance(timedelta(seconds=11))
    drain = await service.claim_drain(ttl=timedelta(seconds=10))
    assert drain is not None and drain.ready
    assert await service.renew(critical.token, ttl=timedelta(seconds=10)) is False

    clock.advance(timedelta(seconds=11))
    replacement = await service.acquire_critical("correction", ttl=timedelta(seconds=10))
    assert replacement is not None
    snapshot = await service.snapshot()
    assert snapshot.drain_owner is None
    assert snapshot.phase == "idle"
    assert snapshot.active_critical_operations == 1


async def test_automatic_claim_materializes_recoverable_install_generation(
    sessionmaker_: SessionMaker,
) -> None:
    clock = MutableClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()

    claim = await service.claim_drain(
        ttl=timedelta(seconds=10),
        action_generation=0,
        materialize_install=True,
    )
    assert claim is not None
    assert claim.lease.action_generation == 1
    snapshot = await service.snapshot()
    assert snapshot.requested_action == "install"
    assert snapshot.action_generation == 1

    clock.advance(timedelta(seconds=11))
    expired = await service.snapshot()
    assert expired.drain_owner is None
    assert expired.phase == "idle"
    assert expired.requested_action == "install"
    assert expired.action_generation == 1


async def test_drain_progress_is_token_bound_and_refreshes_active_phase(
    sessionmaker_: SessionMaker,
) -> None:
    clock = MutableClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.install)
    claim = await service.claim_drain(
        ttl=timedelta(minutes=10),
        action_generation=generation,
    )
    assert claim is not None

    clock.advance(timedelta(seconds=46))
    assert (
        await service.renew_drain_progress(
            "wrong-token",
            ttl=timedelta(minutes=10),
            phase=UpdatePhase.installing,
        )
        is None
    )
    before = await service.snapshot()
    assert before.phase == "draining"

    assert await service.renew_drain_progress(
        claim.lease.token,
        ttl=timedelta(minutes=10),
        phase=UpdatePhase.installing,
    )
    installing = await service.snapshot()
    assert installing.phase == "installing"
    assert service.updater_available(installing, max_age=timedelta(seconds=45))

    assert await service.renew_drain_progress(
        claim.lease.token,
        ttl=timedelta(seconds=10),
        phase=UpdatePhase.rollback,
    )
    assert (await service.snapshot()).phase == "rollback"
    clock.advance(timedelta(seconds=11))
    assert (await service.snapshot()).phase == "idle"


async def test_plaintext_lease_token_is_never_persisted(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=lambda: "plain-lease-token")
    await service.initialize()
    grant = await service.acquire_critical("grab")
    assert grant is not None

    async with sessionmaker_() as session:
        row = (await session.execute(select(MaintenanceLease))).scalar_one()
        assert row.token_hash != grant.token
        assert row.token_hash == hashlib.sha256(grant.token.encode()).hexdigest()


async def test_renewable_context_holds_no_work_transaction_and_releases(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The clock is frozen (never advanced) so lease expiry -- which is decided
    # by comparing the DB row's expires_at to this clock's "now" -- can never
    # be reached, regardless of how long a stalled renew task takes to run
    # under a loaded CI runner (see #311). This makes expiry impossible by
    # construction rather than merely unlikely. Renewal actually firing is
    # still proven independently via the spy below, not inferred from
    # wall-clock survival.
    clock = MutableClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()

    original_renew = service.renew
    renewals = 0

    async def counting_renew(token: str, *, ttl: timedelta) -> bool:
        nonlocal renewals
        renewed = await original_renew(token, ttl=ttl)
        if renewed:
            renewals += 1
        return renewed

    monkeypatch.setattr(service, "renew", counting_renew)

    async with service.critical_operation(
        "import",
        ttl=timedelta(milliseconds=180),
        renew_every=timedelta(milliseconds=40),
    ):
        # This independent write proves the context did not retain its acquisition
        # transaction while the simulated long operation runs.
        assert await service.request_action(UpdateAction.check) == 1
        await asyncio.sleep(0.3)
        snapshot = await service.snapshot()
        assert snapshot.active_critical_operations == 1

    assert renewals >= 1
    assert (await service.snapshot()).active_critical_operations == 0


async def test_critical_context_cancels_work_immediately_when_renewal_is_rejected(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    mutation_completed = False

    async def reject_renewal(_token: str, *, ttl: timedelta) -> bool:
        del ttl
        return False

    monkeypatch.setattr(service, "renew", reject_renewal)
    with pytest.raises(MaintenanceLeaseLostError):
        async with service.critical_operation(
            "import",
            ttl=timedelta(milliseconds=200),
            renew_every=timedelta(milliseconds=20),
        ):
            await asyncio.sleep(1)
            mutation_completed = True

    assert mutation_completed is False
    assert (await service.snapshot()).active_critical_operations == 0


async def test_critical_context_defers_while_drain_is_active(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    assert await service.claim_drain(ttl=timedelta(minutes=1)) is not None

    with pytest.raises(MaintenanceDrainingError):
        async with service.critical_operation("grab"):
            raise AssertionError("drained operation must not start")


async def test_nested_critical_flow_reuses_outer_lease_after_drain_begins(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()

    async with service.critical_operation("correction") as outer:
        drain = await service.claim_drain(ttl=timedelta(minutes=1))
        assert drain is not None and not drain.ready
        async with service.critical_operation("grab") as nested:
            assert nested.token == outer.token
        # An independent task inherits the ContextVar but not the owning task
        # identity, so it still has to acquire and is correctly refused by drain.
        concurrent = await asyncio.create_task(service.acquire_critical("eviction"))
        assert concurrent is None

    assert await service.renew_drain_progress(drain.lease.token, ttl=timedelta(minutes=1)) is True


async def test_heartbeat_freshness_is_bounded(sessionmaker_: SessionMaker) -> None:
    clock = MutableClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock)
    await service.initialize()
    snapshot = await service.touch_updater(phase=UpdatePhase.idle)
    assert snapshot is not None
    assert service.updater_available(snapshot, max_age=timedelta(seconds=30))
    clock.advance(timedelta(seconds=31))
    assert not service.updater_available(snapshot, max_age=timedelta(seconds=30))


async def _plant_phase(sessionmaker_: SessionMaker, phase: str) -> None:
    """Write an unrecognized coordinator phase directly, bypassing the service.

    Models a concurrent writer (a newer/older app version, or the version-skew
    window issue #308 addressed) that lands a phase this app version's
    ``UpdatePhase`` enum does not know, independent of any legitimate service
    call.
    """
    async with sessionmaker_() as session:
        await session.execute(
            update(UpdateCoordinatorState).where(UpdateCoordinatorState.id == 1).values(phase=phase)
        )
        await session.commit()


async def _row(sessionmaker_: SessionMaker) -> UpdateCoordinatorState:
    async with sessionmaker_() as session:
        return (await session.execute(select(UpdateCoordinatorState))).scalar_one()


def test_known_coordinator_phases_track_update_phase_exactly() -> None:
    """Drift guard: the repo module cannot import ``UpdatePhase`` (repositories/
    must never depend on services/ -- hexagonal layering), so it keeps its own
    duplicated frozenset of known phase literals. This asserts the duplicate can
    never silently diverge from the authoritative enum.
    """
    assert frozenset(phase.value for phase in UpdatePhase) == _KNOWN_COORDINATOR_PHASES


async def test_touch_updater_rejects_unknown_phase_inside_the_lock(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await _plant_phase(sessionmaker_, "future_checking")

    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.touch_updater(phase=UpdatePhase.checking, expected_generation=0)

    row = await _row(sessionmaker_)
    assert row.phase == "future_checking"
    assert row.updater_last_seen_at is None


async def test_request_action_rejects_unknown_phase_inside_the_lock(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await _plant_phase(sessionmaker_, "future_available")

    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.request_action(UpdateAction.install)

    row = await _row(sessionmaker_)
    assert row.phase == "future_available"
    assert row.requested_action == "none"
    assert row.action_generation == 0


async def test_claim_drain_rejects_unknown_phase_inside_the_lock(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await _plant_phase(sessionmaker_, "future_checking")

    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.claim_drain(ttl=timedelta(minutes=1))

    row = await _row(sessionmaker_)
    assert row.phase == "future_checking"
    async with sessionmaker_() as session:
        leases = (await session.execute(select(MaintenanceLease))).scalars().all()
    assert leases == []


async def test_renew_drain_progress_rejects_unknown_phase_inside_the_lock(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    claim = await service.claim_drain(ttl=timedelta(minutes=5))
    assert claim is not None
    await _plant_phase(sessionmaker_, "future_installing")

    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.renew_drain_progress(
            claim.lease.token,
            ttl=timedelta(minutes=5),
            phase=UpdatePhase.installing,
        )

    row = await _row(sessionmaker_)
    assert row.phase == "future_installing"
    async with sessionmaker_() as session:
        lease = (await session.execute(select(MaintenanceLease))).scalar_one()
    assert lease.expires_at.replace(tzinfo=UTC) == claim.lease.expires_at


async def test_release_rejects_unknown_phase_inside_the_lock(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    claim = await service.claim_drain(ttl=timedelta(minutes=5))
    assert claim is not None
    await _plant_phase(sessionmaker_, "future_installing")

    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.release(claim.lease.token)

    row = await _row(sessionmaker_)
    assert row.phase == "future_installing"
    async with sessionmaker_() as session:
        lease = (await session.execute(select(MaintenanceLease))).scalar_one()
    assert lease.token_hash == hashlib.sha256(claim.lease.token.encode()).hexdigest()


async def test_release_of_critical_lease_succeeds_under_unknown_phase(
    sessionmaker_: SessionMaker,
) -> None:
    """A critical release never rewrites ``phase``; it must stay reliable even
    when a concurrent writer has left an unrecognized phase (issue #322). Failing
    it closed would leak the lease to TTL and needlessly block idle-only claims
    after the phase recovers, so the lease is cleared and the phase left as-is.
    """
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    grant = await service.acquire_critical("import", ttl=timedelta(minutes=5))
    assert grant is not None
    await _plant_phase(sessionmaker_, "future_available")

    assert await service.release(grant.token) is True

    row = await _row(sessionmaker_)
    assert row.phase == "future_available"
    async with sessionmaker_() as session:
        leases = (await session.execute(select(MaintenanceLease))).scalars().all()
    assert leases == []


async def test_acknowledge_action_rejects_unknown_phase_inside_the_lock(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.check)
    await _plant_phase(sessionmaker_, "future_checking")

    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.acknowledge_action(
            expected_generation=generation,
            result=UpdateResult.no_update,
        )

    row = await _row(sessionmaker_)
    assert row.phase == "future_checking"
    assert row.requested_action == "check"
    assert row.last_result is None


async def test_acknowledge_outcome_rejects_unknown_phase_inside_the_lock(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.install)
    claim = await service.claim_drain(ttl=timedelta(minutes=5), action_generation=generation)
    assert claim is not None
    await _plant_phase(sessionmaker_, "future_installing")

    with pytest.raises(UnknownCoordinatorPhaseError):
        await service.acknowledge_outcome(
            claim.lease.token,
            expected_generation=generation,
            result=UpdateResult.success,
        )

    row = await _row(sessionmaker_)
    assert row.phase == "future_installing"
    assert row.requested_action == "install"
    async with sessionmaker_() as session:
        lease = (await session.execute(select(MaintenanceLease))).scalar_one()
    assert lease.token_hash == hashlib.sha256(claim.lease.token.encode()).hexdigest()


async def _audit_rows(sessionmaker_: SessionMaker) -> list[AuditLog]:
    async with sessionmaker_() as session:
        result = await session.execute(select(AuditLog).order_by(AuditLog.id))
        return list(result.scalars().all())


async def test_force_reset_recovers_unknown_phase_to_idle_with_audit(
    sessionmaker_: SessionMaker,
) -> None:
    """The in-app exit from the wedge (issue #354): an unrecognized phase that
    would otherwise 409 every locked write forever is re-anchored to idle, and
    the recovery is durably recorded (north star #3)."""
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    # A queued install must survive the reset for retry, exactly as expiry
    # cleanup preserves it.
    generation = await service.request_action(UpdateAction.install)
    async with sessionmaker_() as session:
        await session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(phase="future_installing", available_digest="sha256:new")
        )
        await session.commit()

    result = await service.force_reset_coordinator_phase(actor_user_id=None)
    assert result is not None
    assert result.old_phase == "future_installing"
    # The queued action was KNOWN ("install"), so it was preserved, not cleared.
    assert result.cleared_requested_action is None

    row = await _row(sessionmaker_)
    assert row.phase == "idle"
    assert row.requested_action == "install"
    assert row.action_generation == generation
    assert row.available_digest == "sha256:new"

    audit = await _audit_rows(sessionmaker_)
    assert len(audit) == 1
    assert audit[0].action_type == "update.coordinator_phase_force_reset"
    assert audit[0].entity_type == "update_coordinator"
    assert audit[0].entity_id == 1
    assert audit[0].user_id is None
    assert audit[0].old_value == {"phase": "future_installing"}
    assert audit[0].new_value == {"phase": "idle"}


@pytest.mark.parametrize("known_phase", sorted(_KNOWN_COORDINATOR_PHASES))
async def test_force_reset_refuses_every_known_phase_without_touching_state(
    sessionmaker_: SessionMaker, known_phase: str
) -> None:
    """The footgun guard: force-reset acts ONLY on an unrecognized phase. A
    known phase -- including a live draining/installing/rollback -- is a no-op
    refusal, never a reset of an in-flight update, and is never audited."""
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await _plant_phase(sessionmaker_, known_phase)

    assert await service.force_reset_coordinator_phase(actor_user_id=None) is None

    row = await _row(sessionmaker_)
    assert row.phase == known_phase
    assert await _audit_rows(sessionmaker_) == []


async def test_force_reset_refuses_while_an_unexpired_drain_lease_exists(
    sessionmaker_: SessionMaker,
) -> None:
    """An unrecognized phase paired with a LIVE drain lease is the version-skew
    shape where a NEWER updater generation may be legitimately mid-install in a
    phase this build doesn't know. Force-reset must refuse -- lease intact,
    phase intact, nothing audited -- and let the bounded TTL run out instead of
    tearing a possibly-live operation."""
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    claim = await service.claim_drain(ttl=timedelta(minutes=5))
    assert claim is not None
    await _plant_phase(sessionmaker_, "future_installing")

    with pytest.raises(DrainLeaseActiveError):
        await service.force_reset_coordinator_phase(actor_user_id=None)

    row = await _row(sessionmaker_)
    assert row.phase == "future_installing"
    async with sessionmaker_() as session:
        lease = (await session.execute(select(MaintenanceLease))).scalar_one()
    assert lease.kind == "drain"
    assert await _audit_rows(sessionmaker_) == []


async def test_force_reset_proceeds_once_the_drain_lease_has_expired(
    sessionmaker_: SessionMaker,
) -> None:
    """Once the wedged generation's drain lease expires, its ownership is gone
    by definition and the reset proceeds: the EXPIRED drain lease is swept, the
    phase re-anchors to idle, and an independent (unexpired) critical lease is
    left entirely alone."""
    clock = MutableClock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()
    critical = await service.acquire_critical("import", ttl=timedelta(minutes=60))
    assert critical is not None
    claim = await service.claim_drain(ttl=timedelta(minutes=1))
    assert claim is not None
    await _plant_phase(sessionmaker_, "future_installing")

    clock.advance(timedelta(minutes=2))
    result = await service.force_reset_coordinator_phase(actor_user_id=None)
    assert result is not None
    assert result.old_phase == "future_installing"

    async with sessionmaker_() as session:
        leases = (await session.execute(select(MaintenanceLease))).scalars().all()
    kinds = sorted(lease.kind for lease in leases)
    assert kinds == ["critical"]
    assert (await _row(sessionmaker_)).phase == "idle"


def test_known_requested_actions_track_update_action_exactly() -> None:
    """Drift guard, mirroring the phases guard above: the repo's duplicated
    known-requested-action literals can never silently diverge from ``"none"``
    plus the authoritative ``UpdateAction`` enum."""
    expected = frozenset({"none"}) | frozenset(action.value for action in UpdateAction)
    assert expected == _KNOWN_REQUESTED_ACTIONS


async def test_force_reset_clears_an_unrecognized_requested_action(
    sessionmaker_: SessionMaker,
) -> None:
    """The same skew that wedges the phase can leave requested_action
    unrecognized too. After a reset the phase is known again, but any
    non-``none`` action makes ``request_action`` refuse new intent while
    eligibility can't interpret it -- a second, quieter wedge. A successful
    reset therefore normalizes an UNKNOWN action to ``none`` (and audits it);
    the preserve-known-action case is covered by the recovery test above."""
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    async with sessionmaker_() as session:
        await session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(phase="future_installing", requested_action="future_action")
        )
        await session.commit()

    result = await service.force_reset_coordinator_phase(actor_user_id=None)
    assert result is not None
    assert result.old_phase == "future_installing"
    assert result.cleared_requested_action == "future_action"

    row = await _row(sessionmaker_)
    assert row.phase == "idle"
    assert row.requested_action == "none"
    # The un-wedged coordinator accepts fresh operator intent again.
    assert await service.request_action(UpdateAction.check) == row.action_generation + 1

    audit = await _audit_rows(sessionmaker_)
    assert len(audit) == 1
    assert audit[0].old_value == {
        "phase": "future_installing",
        "requested_action": "future_action",
    }
    assert audit[0].new_value == {"phase": "idle", "requested_action": "none"}


async def test_force_reset_double_click_is_idempotent(
    sessionmaker_: SessionMaker,
) -> None:
    """A double-submit is honest: the first click recovers, the second finds a
    now-known (idle) phase and refuses. Exactly one reset, one audit row."""
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await _plant_phase(sessionmaker_, "future_checking")

    first = await service.force_reset_coordinator_phase(actor_user_id=None)
    assert first is not None
    assert first.old_phase == "future_checking"
    assert await service.force_reset_coordinator_phase(actor_user_id=None) is None

    assert (await _row(sessionmaker_)).phase == "idle"
    assert len(await _audit_rows(sessionmaker_)) == 1


async def _plant_action(sessionmaker_: SessionMaker, phase: str, requested_action: str) -> None:
    """Write a phase + requested_action pair directly, bypassing the service --
    models the rollback window where a NEWER version's writes survive in a
    database an older build now reads (Codex round 2 on #357)."""
    async with sessionmaker_() as session:
        await session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(phase=phase, requested_action=requested_action)
        )
        await session.commit()


@pytest.mark.parametrize(
    "known_phase", sorted(_KNOWN_COORDINATOR_PHASES - _BUSY_COORDINATOR_PHASES)
)
async def test_force_reset_clears_unknown_action_under_every_known_non_busy_phase(
    sessionmaker_: SessionMaker, known_phase: str
) -> None:
    """The action-only wedge (Codex round 2 on #357): a rollback can leave a
    KNOWN, safe phase paired with a requested_action this build does not know.
    That action makes request_action refuse ALL new intent as in-progress while
    meaning nothing to eligibility -- and the phase being known means the full
    phase reset would refuse before helping. The action-only reset clears just
    the action, preserves the phase, audits it, and un-wedges request_action."""
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await _plant_action(sessionmaker_, known_phase, "future_action")

    # The quieter wedge: the phase guard passes, but the unrecognized action
    # reads as in-progress and refuses all new operator intent.
    with pytest.raises(UpdateOperationInProgressError):
        await service.request_action(UpdateAction.check)

    result = await service.force_reset_coordinator_phase(actor_user_id=None)
    assert result is not None
    assert result.old_phase is None
    assert result.cleared_requested_action == "future_action"

    row = await _row(sessionmaker_)
    assert row.phase == known_phase
    assert row.requested_action == "none"

    audit = await _audit_rows(sessionmaker_)
    assert len(audit) == 1
    assert audit[0].old_value == {"requested_action": "future_action"}
    assert audit[0].new_value == {"requested_action": "none"}

    # The wedge is gone: fresh operator intent is accepted again.
    assert await service.request_action(UpdateAction.check) == row.action_generation + 1


@pytest.mark.parametrize("busy_phase", sorted(_BUSY_COORDINATOR_PHASES - {"checking"}))
async def test_force_reset_refuses_unknown_action_while_a_leased_operation_is_in_flight(
    sessionmaker_: SessionMaker, busy_phase: str
) -> None:
    """LEASED busy phase + unrecognized action: the in-flight operation's own
    acknowledgement (generation CAS) or drain-lease expiry is the legitimate
    writer of requested_action, so recovery must not race it -- refuse, change
    nothing, audit nothing, regardless of sidecar liveness. Both of those
    exits converge on a cell where the action-only reset becomes available.
    ``checking`` is leaseless and is governed by the heartbeat predicate
    instead (tests below)."""
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await _plant_action(sessionmaker_, busy_phase, "future_action")

    assert await service.force_reset_coordinator_phase(actor_user_id=None) is None

    row = await _row(sessionmaker_)
    assert row.phase == busy_phase
    assert row.requested_action == "future_action"
    assert await _audit_rows(sessionmaker_) == []


async def _plant_checking_wedge(sessionmaker_: SessionMaker, last_seen: datetime | None) -> None:
    async with sessionmaker_() as session:
        await session.execute(
            update(UpdateCoordinatorState)
            .where(UpdateCoordinatorState.id == 1)
            .values(
                phase="checking",
                requested_action="future_action",
                updater_last_seen_at=last_seen,
            )
        )
        await session.commit()


@pytest.mark.parametrize("staleness", ["never_seen", "stale"])
async def test_force_reset_clears_checking_action_wedge_once_heartbeat_is_stale(
    sessionmaker_: SessionMaker, staleness: str
) -> None:
    """Codex round 3 on #357: ``checking`` is busy but LEASELESS -- nothing
    ever expires it, so a blanket busy refusal left checking + unrecognized
    action permanently unrecoverable with the sidecar gone. Once the heartbeat
    is stale (or was never seen) beyond the 45s liveness contract, no check
    can be in flight: the action-only reset proceeds, clearing the action but
    deliberately leaving the phase ``checking`` -- the next completed check's
    normal generation-CAS acknowledgement is the legitimate phase writer."""
    clock = MutableClock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()
    last_seen = None if staleness == "never_seen" else clock.now - timedelta(minutes=2)
    await _plant_checking_wedge(sessionmaker_, last_seen)

    result = await service.force_reset_coordinator_phase(actor_user_id=None)
    assert result is not None
    assert result.old_phase is None
    assert result.cleared_requested_action == "future_action"

    row = await _row(sessionmaker_)
    assert row.phase == "checking"
    assert row.requested_action == "none"
    audit = await _audit_rows(sessionmaker_)
    assert len(audit) == 1
    assert audit[0].old_value == {"requested_action": "future_action"}


async def test_force_reset_refuses_checking_action_wedge_with_fresh_heartbeat(
    sessionmaker_: SessionMaker,
) -> None:
    """While the sidecar heartbeat is FRESH, a check could genuinely be in
    flight (or its outcome about to land), so the checking-phase action wedge
    is still refused -- nothing changed, nothing audited. The predicate is the
    same 45s contract updater_available uses, so 'refused' here always
    coincides with the UI reporting a connected sidecar."""
    clock = MutableClock(datetime(2026, 7, 14, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock, token_factory=_tokens())
    await service.initialize()
    await _plant_checking_wedge(sessionmaker_, clock.now - timedelta(seconds=10))

    assert await service.force_reset_coordinator_phase(actor_user_id=None) is None

    row = await _row(sessionmaker_)
    assert row.phase == "checking"
    assert row.requested_action == "future_action"
    assert await _audit_rows(sessionmaker_) == []

    # The same wedge becomes recoverable as soon as the heartbeat crosses the
    # liveness contract -- the bounded wait the refusal imposes.
    clock.advance(timedelta(minutes=2))
    recovered = await service.force_reset_coordinator_phase(actor_user_id=None)
    assert recovered is not None
    assert recovered.cleared_requested_action == "future_action"
    assert (await _row(sessionmaker_)).requested_action == "none"


async def test_action_only_force_reset_double_click_is_idempotent(
    sessionmaker_: SessionMaker,
) -> None:
    """The action-only variant's second click finds requested_action already
    known ('none') under a known phase -- the true-no-op refusal. One reset,
    one audit row."""
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    await _plant_action(sessionmaker_, "idle", "future_action")

    first = await service.force_reset_coordinator_phase(actor_user_id=None)
    assert first is not None
    assert first.cleared_requested_action == "future_action"
    assert await service.force_reset_coordinator_phase(actor_user_id=None) is None

    row = await _row(sessionmaker_)
    assert row.phase == "idle"
    assert row.requested_action == "none"
    assert len(await _audit_rows(sessionmaker_)) == 1


async def test_concurrent_action_only_force_resets_have_exactly_one_winner(
    tmp_path: Path,
) -> None:
    """The same lock + in-lock re-check protocol serializes the action-only
    variant: the loser re-reads the already-cleared ('none', known) action and
    refuses. Exactly one reset, one audit row."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'action-reset.db'}")
    enable_sqlite_fk_enforcement(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    first = UpdateCoordinationService(maker, token_factory=_tokens("first"))
    second = UpdateCoordinationService(maker, token_factory=_tokens("second"))
    await first.initialize()
    try:
        async with maker() as session:
            await session.execute(
                update(UpdateCoordinatorState)
                .where(UpdateCoordinatorState.id == 1)
                .values(phase="idle", requested_action="future_action")
            )
            await session.commit()

        results = await asyncio.gather(
            first.force_reset_coordinator_phase(actor_user_id=None),
            second.force_reset_coordinator_phase(actor_user_id=None),
        )
        assert sorted(r is None for r in results) == [False, True]
        row = await _row(maker)
        assert row.phase == "idle"
        assert row.requested_action == "none"
        async with maker() as session:
            audit = (await session.execute(select(AuditLog))).scalars().all()
        assert len(audit) == 1
    finally:
        await engine.dispose()


async def test_concurrent_force_resets_have_exactly_one_winner(tmp_path: Path) -> None:
    """The lock + in-lock re-check serialize recovery: two admins clicking at
    once produce exactly one reset (and one audit row), never a double-reset or
    a second reset clobbering a phase the first already re-anchored."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reset.db'}")
    enable_sqlite_fk_enforcement(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    first = UpdateCoordinationService(maker, token_factory=_tokens("first"))
    second = UpdateCoordinationService(maker, token_factory=_tokens("second"))
    await first.initialize()
    try:
        async with maker() as session:
            await session.execute(
                update(UpdateCoordinatorState)
                .where(UpdateCoordinatorState.id == 1)
                .values(phase="future_installing")
            )
            await session.commit()

        results = await asyncio.gather(
            first.force_reset_coordinator_phase(actor_user_id=None),
            second.force_reset_coordinator_phase(actor_user_id=None),
        )
        assert sorted(r is None for r in results) == [False, True]
        assert (await _row(maker)).phase == "idle"
        async with maker() as session:
            audit = (await session.execute(select(AuditLog))).scalars().all()
        assert len(audit) == 1
    finally:
        await engine.dispose()


async def test_concurrent_drain_claims_have_exactly_one_winner(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}")
    enable_sqlite_fk_enforcement(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    first = UpdateCoordinationService(maker, token_factory=_tokens("first"))
    second = UpdateCoordinationService(maker, token_factory=_tokens("second"))
    await first.initialize()
    try:
        claims = await asyncio.gather(
            first.claim_drain(ttl=timedelta(minutes=1)),
            second.claim_drain(ttl=timedelta(minutes=1)),
        )
        assert sum(claim is not None for claim in claims) == 1
        snapshot = await first.snapshot()
        assert snapshot.drain_owner == "container-updater"
    finally:
        await engine.dispose()
