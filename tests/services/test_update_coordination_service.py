"""Database-backed drain/critical leases and durable updater action state."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from plex_manager.db import Base, enable_sqlite_fk_enforcement
from plex_manager.models import MaintenanceLease
from plex_manager.services.update_coordination_service import (
    MaintenanceDrainingError,
    MaintenanceLeaseLostError,
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


async def test_check_ack_with_omitted_image_fields_preserves_heartbeat_observation(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    generation = await service.request_action(UpdateAction.check)
    await service.heartbeat(
        phase=UpdatePhase.checking,
        current_build="build-a",
        current_digest="sha256:a",
        available_build="build-b",
        available_digest="sha256:b",
        checked=True,
    )
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
    assert await service.drain_ready(drain.lease.token) is True
    assert not await service.acknowledge_outcome(
        drain.lease.token,
        expected_generation=generation + 1,
        result=UpdateResult.success,
    )
    assert await service.drain_ready(drain.lease.token) is True

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


async def test_drain_claim_rejects_a_stale_action_generation(
    sessionmaker_: SessionMaker,
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()
    stale = await service.request_action(UpdateAction.check)
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
) -> None:
    service = UpdateCoordinationService(sessionmaker_, token_factory=_tokens())
    await service.initialize()

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

    assert await service.drain_ready(drain.lease.token) is True


async def test_heartbeat_freshness_is_bounded(sessionmaker_: SessionMaker) -> None:
    clock = MutableClock(datetime(2026, 7, 12, 12, 0, tzinfo=UTC))
    service = UpdateCoordinationService(sessionmaker_, clock=clock)
    await service.initialize()
    await service.heartbeat(phase=UpdatePhase.idle, current_build="build-a")
    snapshot = await service.snapshot()
    assert service.updater_available(snapshot, max_age=timedelta(seconds=30))
    clock.advance(timedelta(seconds=31))
    assert not service.updater_available(snapshot, max_age=timedelta(seconds=30))


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
