"""``season_request_service.wake_waiting_for_air_date`` -- periodic air-date wake
for parked TV seasons (issue #210).

Mirrors ``test_season_episode_service.py``'s ``sessionmaker_`` fixture pattern and
``reconcile_airing``'s rotation-test shape, since both share the
``airing_refresh_checked_at`` cursor.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.tmdb import TmdbApiError
from plex_manager.models import MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.metadata import TvMetadata
from plex_manager.services import season_request_service
from tests.web.fakes import FakeLibrary, FakeTmdb

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)


async def _make_show(
    sm: SessionMaker,
    tmdb_id: int,
    *,
    tv_request_mode: str | None = None,
    requested_episodes: dict[str, list[int]] | None = None,
) -> int:
    async with sm() as session:
        show = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title="Show",
            status=RequestStatus.waiting_for_air_date,
            tv_request_mode=tv_request_mode,
            requested_episodes_json=requested_episodes,
        )
        session.add(show)
        await session.commit()
        return show.id


async def _make_waiting_season(sm: SessionMaker, media_request_id: int, season_number: int) -> int:
    async with sm() as session:
        row = SeasonRequest(
            media_request_id=media_request_id,
            season_number=season_number,
            status=RequestStatus.waiting_for_air_date,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _get_season(sm: SessionMaker, season_id: int) -> SeasonRequest:
    async with sm() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        return season


async def test_wakes_a_season_tmdb_now_reports(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1001)
    season_id = await _make_waiting_season(sessionmaker_, show_id, 2)
    tmdb = FakeTmdb(shows={1001: TvMetadata(tmdb_id=1001, title="Show", season_count=2)})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 1
    season = await _get_season(sessionmaker_, season_id)
    assert season.status == RequestStatus.pending
    assert season.next_search_at is None
    assert season.search_attempts == 0
    async with sessionmaker_() as session:
        parent = await session.get(MediaRequest, show_id)
        assert parent is not None
        assert parent.status != RequestStatus.waiting_for_air_date


async def test_still_future_season_stays_waiting(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1002)
    season_id = await _make_waiting_season(sessionmaker_, show_id, 2)
    tmdb = FakeTmdb(shows={1002: TvMetadata(tmdb_id=1002, title="Show", season_count=1)})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 0
    season = await _get_season(sessionmaker_, season_id)
    assert season.status == RequestStatus.waiting_for_air_date
    assert season.airing_refresh_checked_at is not None


async def test_tmdb_error_leaves_state_untouched(
    sessionmaker_: SessionMaker, caplog: pytest.LogCaptureFixture
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1003)
    season_id = await _make_waiting_season(sessionmaker_, show_id, 1)
    tmdb = FakeTmdb(get_tv_show_error=TmdbApiError("tmdb down"))

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.season_request_service"):
        async with sessionmaker_() as session:
            woken = await season_request_service.wake_waiting_for_air_date(
                session, tmdb, None, now=_NOW, max_refresh=5
            )
            await session.commit()

    assert woken == 0
    season = await _get_season(sessionmaker_, season_id)
    assert season.status == RequestStatus.waiting_for_air_date
    assert season.airing_refresh_checked_at is not None
    assert any("air-date wake" in record.message for record in caplog.records)
    # No secret / raw exception text leaks -- only the exception TYPE name.
    assert not any("tmdb down" in record.message for record in caplog.records)


async def test_get_tv_show_none_leaves_waiting(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1004)
    season_id = await _make_waiting_season(sessionmaker_, show_id, 1)
    tmdb = FakeTmdb()  # shows is empty -> get_tv_show returns None

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 0
    season = await _get_season(sessionmaker_, season_id)
    assert season.status == RequestStatus.waiting_for_air_date
    assert season.airing_refresh_checked_at is not None


async def test_zero_season_whole_show_placeholder_s1_woken(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1005, tv_request_mode="whole_show")
    season_id = await _make_waiting_season(sessionmaker_, show_id, 1)
    tmdb = FakeTmdb(shows={1005: TvMetadata(tmdb_id=1005, title="Show", season_count=1)})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 1
    season = await _get_season(sessionmaker_, season_id)
    assert season.status == RequestStatus.pending


async def test_plex_present_season_wakes_directly_to_available(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1006)
    season_id = await _make_waiting_season(sessionmaker_, show_id, 2)
    tmdb = FakeTmdb(shows={1006: TvMetadata(tmdb_id=1006, title="Show", season_count=2)})
    library = FakeLibrary(available_tv_seasons={1006: frozenset({2})})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, library, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 1
    season = await _get_season(sessionmaker_, season_id)
    assert season.status == RequestStatus.available


async def test_unrequested_later_seasons_not_added(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1007)
    await _make_waiting_season(sessionmaker_, show_id, 2)
    tmdb = FakeTmdb(shows={1007: TvMetadata(tmdb_id=1007, title="Show", season_count=5)})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 1
    async with sessionmaker_() as session:
        rows = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
                )
            )
            .scalars()
            .all()
        )
        assert {row.season_number for row in rows} == {2}


async def test_two_waiting_seasons_same_show_mixed(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1008)
    season2_id = await _make_waiting_season(sessionmaker_, show_id, 2)
    season3_id = await _make_waiting_season(sessionmaker_, show_id, 3)
    tmdb = FakeTmdb(shows={1008: TvMetadata(tmdb_id=1008, title="Show", season_count=2)})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 1
    season2 = await _get_season(sessionmaker_, season2_id)
    season3 = await _get_season(sessionmaker_, season3_id)
    assert season2.status == RequestStatus.pending
    assert season3.status == RequestStatus.waiting_for_air_date
    assert tmdb.get_tv_show_calls == [1008]


async def test_bounded_and_rotates_across_cycles(sessionmaker_: SessionMaker) -> None:
    show_ids = []
    season_ids = []
    for tmdb_id in (1101, 1102, 1103):
        show_id = await _make_show(sessionmaker_, tmdb_id=tmdb_id)
        season_id = await _make_waiting_season(sessionmaker_, show_id, 99)  # never aired
        show_ids.append(show_id)
        season_ids.append(season_id)

    # season_count == 0 for every show -> season 99 never wakes; isolates rotation.
    tmdb = FakeTmdb(
        shows={
            tmdb_id: TvMetadata(tmdb_id=tmdb_id, title="Show", season_count=0)
            for tmdb_id in (1101, 1102, 1103)
        }
    )

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=2
        )
        await session.commit()
    assert woken == 0

    stamped_after_cycle1 = []
    for season_id in season_ids:
        season = await _get_season(sessionmaker_, season_id)
        stamped_after_cycle1.append(season.airing_refresh_checked_at is not None)
    assert sum(stamped_after_cycle1) == 2

    later = _NOW + timedelta(minutes=5)
    tmdb2 = FakeTmdb(
        shows={
            tmdb_id: TvMetadata(tmdb_id=tmdb_id, title="Show", season_count=0)
            for tmdb_id in (1101, 1102, 1103)
        }
    )
    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb2, None, now=later, max_refresh=2
        )
        await session.commit()
    assert woken == 0

    stamped_after_cycle2 = []
    for season_id in season_ids:
        season = await _get_season(sessionmaker_, season_id)
        stamped_after_cycle2.append(season.airing_refresh_checked_at)
    # Every row is now stamped -- the previously-unstamped one was picked up.
    assert all(ts is not None for ts in stamped_after_cycle2)


async def test_still_future_season_not_rechecked_within_interval(
    sessionmaker_: SessionMaker,
) -> None:
    """P2: a still-future season must not re-cost a TMDB lookup every ~60s cycle.

    Within ``_AIR_DATE_WAKE_MIN_INTERVAL`` of its last check the row is skipped
    entirely (no ``get_tv_show``); once the interval elapses it is due again.
    """
    show_id = await _make_show(sessionmaker_, tmdb_id=1010)
    season_id = await _make_waiting_season(sessionmaker_, show_id, 2)

    # Pass 1: season 2 above season_count 1 -> stays waiting, stamped, TMDB queried.
    tmdb1 = FakeTmdb(shows={1010: TvMetadata(tmdb_id=1010, title="Show", season_count=1)})
    async with sessionmaker_() as session:
        await season_request_service.wake_waiting_for_air_date(
            session, tmdb1, None, now=_NOW, max_refresh=5
        )
        await session.commit()
    assert tmdb1.get_tv_show_calls == [1010]
    stamped = (await _get_season(sessionmaker_, season_id)).airing_refresh_checked_at

    # Pass 2: one minute later, still inside the interval -> row not even selected,
    # so NO TMDB call and the stamp is unchanged.
    tmdb2 = FakeTmdb(shows={1010: TvMetadata(tmdb_id=1010, title="Show", season_count=1)})
    async with sessionmaker_() as session:
        await season_request_service.wake_waiting_for_air_date(
            session, tmdb2, None, now=_NOW + timedelta(minutes=1), max_refresh=5
        )
        await session.commit()
    assert tmdb2.get_tv_show_calls == []
    assert (await _get_season(sessionmaker_, season_id)).airing_refresh_checked_at == stamped

    # Pass 3: past the interval -> due again, TMDB queried again.
    tmdb3 = FakeTmdb(shows={1010: TvMetadata(tmdb_id=1010, title="Show", season_count=1)})
    async with sessionmaker_() as session:
        await season_request_service.wake_waiting_for_air_date(
            session, tmdb3, None, now=_NOW + timedelta(hours=7), max_refresh=5
        )
        await session.commit()
    assert tmdb3.get_tv_show_calls == [1010]


async def test_whole_show_placeholder_expands_to_full_season_set(
    sessionmaker_: SessionMaker,
) -> None:
    """P2: a zero-season whole-show placeholder (parked S1) expands to
    1..season_count on wake, not just the placeholder S1."""
    show_id = await _make_show(sessionmaker_, tmdb_id=1011, tv_request_mode="whole_show")
    await _make_waiting_season(sessionmaker_, show_id, 1)
    tmdb = FakeTmdb(shows={1011: TvMetadata(tmdb_id=1011, title="Show", season_count=3)})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 1  # the placeholder S1 transitioned out of waiting
    async with sessionmaker_() as session:
        rows = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
                )
            )
            .scalars()
            .all()
        )
    assert {row.season_number: row.status for row in rows} == {
        1: RequestStatus.pending,
        2: RequestStatus.pending,
        3: RequestStatus.pending,
    }


async def test_episode_scoped_wake_forced_pending_not_available(
    sessionmaker_: SessionMaker,
) -> None:
    """P2: an episode-scoped parked season (e.g. S2E10) must wake to ``pending``
    and be searched, never be marked ``available`` off partial Plex season
    presence (S2E1 present)."""
    show_id = await _make_show(
        sessionmaker_,
        tmdb_id=1012,
        tv_request_mode="explicit_episodes",
        requested_episodes={"2": [10]},
    )
    season_id = await _make_waiting_season(sessionmaker_, show_id, 2)
    tmdb = FakeTmdb(shows={1012: TvMetadata(tmdb_id=1012, title="Show", season_count=2)})
    # Plex reports season 2 present (only S2E1 in reality) -> season-level presence
    # would falsely satisfy the request without the force_pending guard.
    library = FakeLibrary(available_tv_seasons={1012: frozenset({2})})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, library, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert woken == 1
    season = await _get_season(sessionmaker_, season_id)
    assert season.status == RequestStatus.pending
    assert season.next_search_at is None
    assert season.search_attempts == 0


async def test_second_pass_is_noop_after_wake(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=1009)
    season_id = await _make_waiting_season(sessionmaker_, show_id, 2)
    tmdb = FakeTmdb(shows={1009: TvMetadata(tmdb_id=1009, title="Show", season_count=2)})

    async with sessionmaker_() as session:
        woken = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW, max_refresh=5
        )
        await session.commit()
    assert woken == 1

    async with sessionmaker_() as session:
        woken_again = await season_request_service.wake_waiting_for_air_date(
            session, tmdb, None, now=_NOW + timedelta(minutes=1), max_refresh=5
        )
        await session.commit()
    assert woken_again == 0

    season = await _get_season(sessionmaker_, season_id)
    assert season.status == RequestStatus.pending
