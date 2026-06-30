"""ensure_system_settings — the install-state row is a true singleton.

Two workers racing to initialise an empty DB must not produce two rows: the row
is pinned to ``id=1`` (PK + CHECK constraint), so a second insert collides and is
resolved to a re-read of the one row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import SystemSettings
from plex_manager.web.deps import ensure_system_settings, load_system_settings

SessionMaker = async_sessionmaker[AsyncSession]


async def _row_count(sm: SessionMaker) -> int:
    async with sm() as session:
        result = await session.execute(select(func.count()).select_from(SystemSettings))
        return result.scalar_one()


async def test_ensure_system_settings_is_idempotent_single_row(
    sessionmaker_: SessionMaker,
) -> None:
    # First call creates the row; a second call (e.g. another worker) must be a
    # no-op that returns the same id=1 row — never a second insert.
    async with sessionmaker_() as session:
        first = await ensure_system_settings(session)
        await session.commit()
    assert first.id == 1

    async with sessionmaker_() as session:
        second = await ensure_system_settings(session)
        await session.commit()
    assert second.id == 1

    assert await _row_count(sessionmaker_) == 1


async def test_load_system_settings_orders_by_id(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        await ensure_system_settings(session)
        await session.commit()

    async with sessionmaker_() as session:
        row = await load_system_settings(session)
    assert row is not None
    assert row.id == 1


async def test_second_row_is_rejected_by_singleton_constraint(
    sessionmaker_: SessionMaker,
) -> None:
    # The CHECK(id = 1) constraint forbids any row other than id=1, so a stray
    # second insert cannot create a sibling install-state row.
    async with sessionmaker_() as session:
        session.add(SystemSettings(id=1, initialized=False))
        await session.commit()

    async with sessionmaker_() as session:
        session.add(SystemSettings(id=2, initialized=False))
        with pytest.raises(IntegrityError):
            await session.commit()
