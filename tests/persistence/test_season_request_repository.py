"""``SqlSeasonRequestRepository`` ensure / get / list / set_status / mark_*.

``ensure()`` idempotency and its IntegrityError-catch-and-reread race handling are
the focus here (mirrors ``request_service.py:159-184``); the raw DB-level
uniqueness constraint itself is pinned separately in
``test_season_request_schema.py``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import MediaRequest, RequestStatus, SeasonRequest
from plex_manager.repositories import SqlSeasonRequestRepository


async def _make_show(session: AsyncSession, tmdb_id: int = 900) -> MediaRequest:
    mr = MediaRequest(tmdb_id=tmdb_id, media_type="tv", title="Show", status="pending")
    session.add(mr)
    await session.flush()
    return mr


async def test_ensure_creates_a_new_season_row(session: AsyncSession) -> None:
    show = await _make_show(session)
    repo = SqlSeasonRequestRepository(session)

    created = await repo.ensure(show.id, 1, status="pending")
    assert created.id > 0
    assert created.media_request_id == show.id
    assert created.season_number == 1
    assert created.status == "pending"
    # tmdb_id is denormalized from the parent show, not a season_requests column.
    assert created.tmdb_id == show.tmdb_id


async def test_ensure_is_idempotent_and_never_overwrites_an_established_season(
    session: AsyncSession,
) -> None:
    show = await _make_show(session)
    repo = SqlSeasonRequestRepository(session)

    first = await repo.ensure(show.id, 1, status="pending")
    # A second ensure() call for the SAME season returns the SAME row -- the
    # ``status`` argument is only used on first creation, never applied to an
    # already-established season (a later re-request for a whole series must not
    # regress an in-flight/finished season back to "pending").
    again = await repo.ensure(show.id, 1, status="downloading")
    assert again.id == first.id
    assert again.status == "pending"

    rows = await repo.list_for_request(show.id)
    assert len(rows) == 1


async def test_ensure_different_seasons_creates_distinct_rows(session: AsyncSession) -> None:
    show = await _make_show(session)
    repo = SqlSeasonRequestRepository(session)

    s1 = await repo.ensure(show.id, 1, status="pending")
    s2 = await repo.ensure(show.id, 2, status="available")
    assert s1.id != s2.id

    rows = await repo.list_for_request(show.id)
    assert {(r.season_number, r.status) for r in rows} == {(1, "pending"), (2, "available")}


async def test_ensure_scopes_by_show_not_just_season_number(session: AsyncSession) -> None:
    show_a = await _make_show(session, tmdb_id=901)
    show_b = await _make_show(session, tmdb_id=902)
    repo = SqlSeasonRequestRepository(session)

    a1 = await repo.ensure(show_a.id, 1, status="pending")
    b1 = await repo.ensure(show_b.id, 1, status="pending")
    assert a1.id != b1.id
    assert a1.tmdb_id == 901
    assert b1.tmdb_id == 902


async def test_ensure_resolves_to_the_winner_when_its_own_insert_collides(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a genuine TOCTOU race: another writer creates the row for the
    SAME (show, season) between ensure()'s existence check and its own insert
    attempt. ensure() must catch the resulting IntegrityError (the unconditional
    ``uq_season_requests_media_season`` unique index), roll back its own losing
    insert, and resolve to a re-read of the ACTUAL winner -- never raising, and
    never leaving a second row behind.
    """
    show = await _make_show(session)

    # The "other writer"'s row already exists...
    winner = SeasonRequest(
        media_request_id=show.id, season_number=1, status=RequestStatus.searching
    )
    session.add(winner)
    await session.flush()

    # ...but force ensure()'s own pre-check to miss it EXACTLY ONCE, so its
    # insert attempt actually runs and collides with the real unique index,
    # instead of short-circuiting via the normal "already exists" path. Patched
    # at the class level (a string attribute name, not a member-access
    # expression) exactly like ``test_request_service.py`` patches
    # ``SqlRequestRepository.find_active`` to simulate the analogous
    # active-dedup race.
    calls = {"n": 0}

    async def racing_find(
        self: SqlSeasonRequestRepository, media_request_id: int, season_number: int
    ) -> SeasonRequest | None:
        if calls["n"] == 0:
            calls["n"] = 1
            return None
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == media_request_id,
            SeasonRequest.season_number == season_number,
        )
        return (await session.execute(stmt)).scalars().first()

    monkeypatch.setattr(SqlSeasonRequestRepository, "_find", racing_find)

    repo = SqlSeasonRequestRepository(session)
    resolved = await repo.ensure(show.id, 1, status="pending")
    assert resolved.id == winner.id
    # The winner's status is untouched by the losing insert's "pending" arg.
    assert resolved.status == "searching"

    rows = await repo.list_for_request(show.id)
    assert len(rows) == 1


async def test_get_missing_returns_none(session: AsyncSession) -> None:
    repo = SqlSeasonRequestRepository(session)
    assert await repo.get(999) is None


async def test_list_by_status_filters_across_shows(session: AsyncSession) -> None:
    show_a = await _make_show(session, tmdb_id=910)
    show_b = await _make_show(session, tmdb_id=911)
    repo = SqlSeasonRequestRepository(session)

    await repo.ensure(show_a.id, 1, status="pending")
    await repo.ensure(show_a.id, 2, status="downloading")
    await repo.ensure(show_b.id, 1, status="pending")

    pending = await repo.list_by_status("pending")
    assert {(r.media_request_id, r.season_number) for r in pending} == {
        (show_a.id, 1),
        (show_b.id, 1),
    }
    assert len(await repo.list_by_status()) == 3


async def test_set_status_updates(session: AsyncSession) -> None:
    show = await _make_show(session)
    repo = SqlSeasonRequestRepository(session)
    created = await repo.ensure(show.id, 1, status="pending")

    await repo.set_status(created.id, "searching")
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.status == "searching"


async def test_mark_completed_and_mark_available(session: AsyncSession) -> None:
    show = await _make_show(session)
    repo = SqlSeasonRequestRepository(session)
    created = await repo.ensure(show.id, 1, status="downloading")

    await repo.mark_completed(created.id)
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.status == "completed"

    await repo.mark_available(created.id)
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.status == "available"
