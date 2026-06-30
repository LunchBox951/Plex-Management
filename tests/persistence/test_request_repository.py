"""``SqlRequestRepository`` create / get / list / find_active / set_status."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.repositories import SqlRequestRepository


async def test_create_then_get_returns_persisted_record(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    created = await repo.create(
        tmdb_id=603,
        media_type="movie",
        title="The Matrix",
        status="pending",
        year=1999,
    )
    assert created.id > 0
    assert created.media_type == "movie"
    assert created.status == "pending"
    assert created.is_anime is False

    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched == created


async def test_get_missing_returns_none(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    assert await repo.get(999) is None


async def test_list_by_status_filters(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=1, media_type="movie", title="A", status="pending")
    await repo.create(tmdb_id=2, media_type="movie", title="B", status="downloading")
    await repo.create(tmdb_id=3, media_type="tv", title="C", status="pending")

    pending = await repo.list_by_status("pending")
    assert {r.tmdb_id for r in pending} == {1, 3}
    assert len(await repo.list_by_status()) == 3


async def test_find_active_uses_tmdb_media_composite_for_dedup(
    session: AsyncSession,
) -> None:
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=42, media_type="movie", title="Dup", status="searching")

    # Same tmdb_id but different media_type must NOT collide.
    assert await repo.find_active(42, "tv") is None
    active = await repo.find_active(42, "movie")
    assert active is not None
    assert active.tmdb_id == 42


async def test_find_active_ignores_terminal_requests(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    done = await repo.create(tmdb_id=7, media_type="movie", title="Done", status="completed")
    assert await repo.find_active(7, "movie") is None

    # A non-terminal request for the same media is found again.
    await repo.set_status(done.id, "searching")
    again = await repo.find_active(7, "movie")
    assert again is not None
    assert again.status == "searching"


async def test_partial_unique_index_blocks_second_active_request(
    session: AsyncSession,
) -> None:
    """The partial UNIQUE index serializes active-request dedup at the DB level: a
    second ACTIVE request for the same (tmdb_id, media_type) is rejected."""
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=500, media_type="movie", title="A", status="pending")
    with pytest.raises(IntegrityError):
        await repo.create(tmdb_id=500, media_type="movie", title="A again", status="searching")


async def test_partial_unique_index_allows_new_request_after_terminal(
    session: AsyncSession,
) -> None:
    """Terminal statuses are outside the partial index, so once a request finishes
    (completed/available/failed) a fresh active request for the same media is
    allowed — the index does not block legitimate re-requests."""
    repo = SqlRequestRepository(session)
    done = await repo.create(tmdb_id=600, media_type="movie", title="Done", status="completed")
    fresh = await repo.create(tmdb_id=600, media_type="movie", title="Again", status="pending")
    assert fresh.id != done.id


async def test_partial_unique_index_scoped_by_media_type(session: AsyncSession) -> None:
    """The index is on (tmdb_id, media_type): the same tmdb_id under a different
    media_type is not a conflict."""
    repo = SqlRequestRepository(session)
    await repo.create(tmdb_id=700, media_type="movie", title="M", status="pending")
    tv = await repo.create(tmdb_id=700, media_type="tv", title="T", status="pending")
    assert tv.id > 0


async def test_set_status_updates(session: AsyncSession) -> None:
    repo = SqlRequestRepository(session)
    created = await repo.create(tmdb_id=11, media_type="tv", title="Show", status="pending")
    await repo.set_status(created.id, "downloading")
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.status == "downloading"
