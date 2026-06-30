"""request_service — active-request dedup recovers from the UNIQUE-index race.

A concurrent POST /requests for the same ``(tmdb_id, media_type)`` can both pass
the application-level ``find_active`` check, then both INSERT. The partial UNIQUE
index over active statuses rejects the loser; ``create_request`` catches the
``IntegrityError`` and resolves to the existing active request instead of crashing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import MediaRequest, MediaType, RequestStatus
from plex_manager.ports.metadata import MovieMetadata
from plex_manager.ports.repositories import RequestRecord
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import request_service
from tests.web.fakes import FakeTmdb

SessionMaker = async_sessionmaker[AsyncSession]


async def test_create_request_recovers_from_active_dedup_conflict(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An active request for this media already exists (committed by the "winner").
    async with sessionmaker_() as session:
        existing = MediaRequest(
            tmdb_id=777,
            media_type=MediaType.movie,
            title="Dune",
            status=RequestStatus.pending,
        )
        session.add(existing)
        await session.commit()
        existing_id = existing.id

    real_find_active = SqlRequestRepository.find_active
    calls = {"n": 0}

    async def racing_find_active(
        self: SqlRequestRepository, tmdb_id: int, media_type: str
    ) -> RequestRecord | None:
        # Simulate the race: the first lookup misses (the winner's row is not yet
        # visible to this transaction), forcing the INSERT that loses the UNIQUE
        # race; the recovery lookup then finds the committed winner.
        if calls["n"] == 0:
            calls["n"] = 1
            return None
        return await real_find_active(self, tmdb_id, media_type)

    monkeypatch.setattr(SqlRequestRepository, "find_active", racing_find_active)

    tmdb = FakeTmdb(movies={777: MovieMetadata(tmdb_id=777, title="Dune", year=2021)})
    async with sessionmaker_() as session:
        record = await request_service.create_request(
            session, tmdb, tmdb_id=777, media_type="movie"
        )
    assert record.id == existing_id  # returned the existing active request

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 777)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # no duplicate active request was created


@pytest.mark.parametrize(
    "terminal_status",
    [RequestStatus.completed, RequestStatus.available, RequestStatus.failed],
)
async def test_mark_no_acceptable_release_never_unterminates_finished_request(
    sessionmaker_: SessionMaker,
    terminal_status: RequestStatus,
) -> None:
    """A finished (terminal) request is left intact. ``no_acceptable_release`` is
    itself non-terminal and dedup-blocking, so writing it over a completed /
    available / failed request would resurrect a dead-end ghost that re-blocks a
    fresh request for the same media — never un-terminate a finished request."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=888,
            media_type=MediaType.movie,
            title="Arrival",
            status=terminal_status,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        await request_service.mark_no_acceptable_release(session, request_id)

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.status is terminal_status  # untouched, not no_acceptable_release
