"""ORM schema guarantees for updater state and maintenance leases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import MaintenanceLease, UpdateCoordinatorState


async def test_update_coordinator_defaults_and_build_history_round_trip(
    session: AsyncSession,
) -> None:
    row = UpdateCoordinatorState(id=1)
    session.add(row)
    await session.flush()
    await session.refresh(row)

    assert row.requested_action == "none"
    assert row.action_generation == 0
    assert row.acknowledged_generation == 0
    assert row.phase == "idle"
    assert row.last_from_build is None
    assert row.last_to_build is None
    assert row.last_outcome_token_hash is None

    row.last_from_build = "old-build"
    row.last_to_build = "new-build"
    await session.flush()
    fetched = await session.get(UpdateCoordinatorState, 1)
    assert fetched is not None
    assert (fetched.last_from_build, fetched.last_to_build) == ("old-build", "new-build")


async def test_update_coordinator_is_a_database_singleton(session: AsyncSession) -> None:
    session.add(UpdateCoordinatorState(id=2))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_many_critical_leases_but_only_one_drain_lease(session: AsyncSession) -> None:
    now = datetime.now(UTC)
    session.add(UpdateCoordinatorState(id=1))
    session.add_all(
        [
            MaintenanceLease(
                token_hash="a" * 64,
                kind="critical",
                owner="app",
                operation="import",
                created_at=now,
                renewed_at=now,
                expires_at=now + timedelta(minutes=1),
            ),
            MaintenanceLease(
                token_hash="b" * 64,
                kind="critical",
                owner="app",
                operation="grab",
                created_at=now,
                renewed_at=now,
                expires_at=now + timedelta(minutes=1),
            ),
            MaintenanceLease(
                token_hash="c" * 64,
                kind="drain",
                owner="updater-a",
                operation="container_update",
                created_at=now,
                renewed_at=now,
                expires_at=now + timedelta(minutes=1),
            ),
        ]
    )
    await session.flush()
    assert len((await session.execute(select(MaintenanceLease))).scalars().all()) == 3

    session.add(
        MaintenanceLease(
            token_hash="d" * 64,
            kind="drain",
            owner="updater-b",
            operation="container_update",
            created_at=now,
            renewed_at=now,
            expires_at=now + timedelta(minutes=1),
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
