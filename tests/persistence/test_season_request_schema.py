"""``uq_season_requests_media_season`` — the per-(show, season) uniqueness backstop.

Pins the DB-level constraint directly via the ORM, the same way
``test_fk_enforcement.py`` pins FK behaviour directly rather than through a
repository. ``SqlSeasonRequestRepository`` (which relies on this same index to
make ``ensure()`` race-safe) is exercised separately in
``test_season_request_repository.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import MediaRequest, SeasonRequest


async def test_duplicate_season_for_same_request_is_rejected(session: AsyncSession) -> None:
    # Unconditional (no WHERE) unique index: a show can never have two rows for
    # the same season, regardless of status. This is what makes a future
    # SeasonRequestRepository.ensure() race-safe under concurrent grabs.
    mr = MediaRequest(tmdb_id=900, media_type="tv", title="Show", status="pending")
    session.add(mr)
    await session.flush()

    session.add(SeasonRequest(media_request_id=mr.id, season_number=1, status="pending"))
    await session.flush()

    session.add(SeasonRequest(media_request_id=mr.id, season_number=1, status="searching"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_different_seasons_for_same_request_are_allowed(session: AsyncSession) -> None:
    mr = MediaRequest(tmdb_id=901, media_type="tv", title="Show", status="pending")
    session.add(mr)
    await session.flush()

    session.add(SeasonRequest(media_request_id=mr.id, season_number=1, status="pending"))
    session.add(SeasonRequest(media_request_id=mr.id, season_number=2, status="pending"))
    await session.flush()


async def test_same_season_number_across_different_requests_is_allowed(
    session: AsyncSession,
) -> None:
    # The index is scoped by media_request_id: season 1 of two DIFFERENT shows
    # is not a conflict.
    show_a = MediaRequest(tmdb_id=902, media_type="tv", title="Show A", status="pending")
    show_b = MediaRequest(tmdb_id=903, media_type="tv", title="Show B", status="pending")
    session.add_all([show_a, show_b])
    await session.flush()

    session.add(SeasonRequest(media_request_id=show_a.id, season_number=1, status="pending"))
    session.add(SeasonRequest(media_request_id=show_b.id, season_number=1, status="pending"))
    await session.flush()
