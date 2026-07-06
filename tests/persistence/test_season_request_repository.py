"""``SqlSeasonRequestRepository`` ensure / get / list / set_status / mark_*.

``ensure()`` idempotency and its IntegrityError-catch-and-reread race handling are
the focus here (mirrors ``request_service.py:159-184``); the raw DB-level
uniqueness constraint itself is pinned separately in
``test_season_request_schema.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import MediaRequest, RequestStatus, SeasonRequest
from plex_manager.repositories import SqlSeasonRequestRepository

# The statuses the auto-grab worker scans (ADR-0013); the backoff gate applies
# ONLY to the parked ``no_acceptable_release``.
_DUE_STATUSES = frozenset({"pending", "no_acceptable_release", "searching"})
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


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


async def test_list_for_requests_batches_multiple_shows_in_one_call(
    session: AsyncSession,
) -> None:
    """The batch read the ``GET /requests`` list endpoint uses to avoid an N+1
    query per tv row: one call returns every named show's season rows, grouped."""
    show_a = await _make_show(session, tmdb_id=920)
    show_b = await _make_show(session, tmdb_id=921)
    show_c = await _make_show(session, tmdb_id=922)  # untracked -- no season rows
    repo = SqlSeasonRequestRepository(session)

    await repo.ensure(show_a.id, 1, status="pending")
    await repo.ensure(show_a.id, 2, status="downloading")
    await repo.ensure(show_b.id, 1, status="available")

    grouped = await repo.list_for_requests([show_a.id, show_b.id, show_c.id])

    assert {(s.season_number, s.status) for s in grouped[show_a.id]} == {
        (1, "pending"),
        (2, "downloading"),
    }
    assert [(s.season_number, s.status) for s in grouped[show_b.id]] == [(1, "available")]
    # tmdb_id is the PARENT show's, denormalized via the join -- not a lookup per row.
    assert all(s.tmdb_id == show_a.tmdb_id for s in grouped[show_a.id])
    # A show with no tracked seasons is simply absent, not mapped to [].
    assert show_c.id not in grouped


async def test_list_for_requests_empty_input_returns_empty_dict(session: AsyncSession) -> None:
    repo = SqlSeasonRequestRepository(session)
    assert await repo.list_for_requests([]) == {}


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


async def test_library_path_defaults_none_and_round_trips(session: AsyncSession) -> None:
    """Mirrors ``RequestRepository``'s breadcrumb (ADR-0012), one season at a time."""
    show = await _make_show(session)
    repo = SqlSeasonRequestRepository(session)
    created = await repo.ensure(show.id, 1, status="downloading")
    assert created.library_path is None

    await repo.set_library_path(created.id, "/data/library/tv/Show/Season 01")
    fetched = await repo.get(created.id)
    assert fetched is not None
    assert fetched.library_path == "/data/library/tv/Show/Season 01"


async def test_set_library_path_missing_row_raises(session: AsyncSession) -> None:
    repo = SqlSeasonRequestRepository(session)
    with pytest.raises(LookupError):
        await repo.set_library_path(999, "/data/library/tv/Ghost/Season 01")


# --------------------------------------------------------------------------- #
# list_due_for_search — the backoff gate applies ONLY to parked seasons (ADR-0013)
# --------------------------------------------------------------------------- #
async def test_list_due_returns_searching_season_with_stale_future_backoff(
    session: AsyncSession,
) -> None:
    """The season-level mirror of the movie rule: a season re-armed to ``searching``
    (a failed download) may still carry a stale ``next_search_at`` from a PRIOR
    ``no_acceptable_release`` backoff. ``searching`` is EAGER -- due IMMEDIATELY,
    never suppressed until that stale future timestamp expires."""
    show = await _make_show(session, tmdb_id=930)
    repo = SqlSeasonRequestRepository(session)
    season = await repo.ensure(show.id, 1, status="searching")
    await repo.schedule_search(
        season.id, search_attempts=3, next_search_at=_NOW + timedelta(hours=24)
    )

    due = await repo.list_due_for_search(_DUE_STATUSES, _NOW)
    assert [r.id for r in due] == [season.id]


async def test_list_due_suppresses_parked_season_until_backoff_elapses(
    session: AsyncSession,
) -> None:
    """A parked ``no_acceptable_release`` season earned its backoff: a FUTURE
    ``next_search_at`` means NOT due yet (existing behavior, pinned)."""
    show = await _make_show(session, tmdb_id=931)
    repo = SqlSeasonRequestRepository(session)
    season = await repo.ensure(show.id, 1, status="no_acceptable_release")
    await repo.schedule_search(
        season.id, search_attempts=1, next_search_at=_NOW + timedelta(hours=1)
    )

    due = await repo.list_due_for_search(_DUE_STATUSES, _NOW)
    assert due == []


async def test_evicted_seasons_reflects_the_newest_row_per_season(
    session: AsyncSession,
) -> None:
    """``evicted_seasons`` is the TV twin of ``latest_request_evicted`` (ADR-0012):
    the seasons whose NEWEST row (across every tv MediaRequest for this tmdb_id) is
    ``evicted``. ``ensure_seasons`` subtracts these from Plex's present set so a
    just-reclaimed season is never re-armed 'available' off a stale in-Plex reading.
    Keyed on the newest row per season so a season re-downloaded under a LATER
    MediaRequest (a fresh 'available' row) is not falsely suppressed."""
    repo = SqlSeasonRequestRepository(session)

    # Old request, wholly evicted (settled, so it does not occupy the active-dedup
    # slot when the newer request below is created): season 1 + season 2 evicted.
    old = MediaRequest(tmdb_id=940, media_type="tv", title="Show", status="evicted")
    session.add(old)
    await session.flush()
    await repo.ensure(old.id, 1, status="evicted")
    await repo.ensure(old.id, 2, status="evicted")
    assert await repo.evicted_seasons(940) == frozenset({1, 2})

    # A NEWER MediaRequest for the same show re-downloaded season 1 -> its newest
    # season-1 row is 'available', so season 1 drops out; season 2 (still only the
    # old evicted row) remains.
    new = MediaRequest(tmdb_id=940, media_type="tv", title="Show", status="partially_available")
    session.add(new)
    await session.flush()
    await repo.ensure(new.id, 1, status="available")
    assert await repo.evicted_seasons(940) == frozenset({2})

    # A show with no evicted history at all -> empty.
    other = await _make_show(session, tmdb_id=941)
    await repo.ensure(other.id, 1, status="available")
    assert await repo.evicted_seasons(941) == frozenset()
