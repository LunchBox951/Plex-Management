"""ORM-level guarantees for the ``users`` entitlement columns (issue #391 PR-1).

The tri-state contract on ``entitled_section_keys`` (NULL = never captured,
``[]`` = captured/none) is only real if an *assigned* ``None`` round-trips as
SQL NULL. Bare ``sa.JSON`` would store it as the JSON text ``'null'``, which a
``WHERE entitled_section_keys IS NULL`` reset-to-never-captured query silently
misses -- hence ``none_as_null=True`` on the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import User


async def test_assigned_none_is_sql_null_not_json_null(session: AsyncSession) -> None:
    captured = User(username="captured", entitled_section_keys=["1", "5"])
    reset = User(username="reset", entitled_section_keys=["2"])
    session.add_all([captured, reset])
    await session.commit()

    # The design's "machine-id mismatch => not captured" rule resets by
    # assigning None; that write must land as SQL NULL.
    reset.entitled_section_keys = None
    await session.commit()

    null_names = (
        (
            await session.execute(
                sa.select(User.username).where(User.entitled_section_keys.is_(None))
            )
        )
        .scalars()
        .all()
    )
    assert "reset" in null_names
    assert "captured" not in null_names


async def test_empty_list_stays_distinct_from_sql_null(session: AsyncSession) -> None:
    session.add(User(username="none-entitled", entitled_section_keys=[]))
    await session.commit()

    row = (
        await session.execute(
            sa.select(User.entitled_section_keys, User.entitled_section_keys.is_(None)).where(
                User.username == "none-entitled"
            )
        )
    ).one()
    # Captured-but-empty is a real JSON [] -- NOT SQL NULL.
    assert row == ([], False)
