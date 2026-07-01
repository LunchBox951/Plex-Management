"""season_request_service — per-season lifecycle + the parent rollup it recomputes.

``ensure_seasons`` / ``set_status`` / ``mark_completed`` / ``mark_available`` /
``mark_no_acceptable_release`` are exercised primarily through the PARENT
``MediaRequest.status`` they leave behind (the pure fold is unit-tested directly
in ``tests/domain/test_season_rollup.py``); these tests pin the wiring: every
season-status write recomputes and persists the rollup in the SAME call.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.services import season_request_service
from tests.web.fakes import FakeLibrary

SessionMaker = async_sessionmaker[AsyncSession]


async def _make_show(sm: SessionMaker, tmdb_id: int = 700) -> int:
    async with sm() as session:
        show = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title="Some Show",
            status=RequestStatus.pending,
        )
        session.add(show)
        await session.commit()
        return show.id


async def test_ensure_seasons_creates_pending_rows_and_rolls_up_pending(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_)

    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=700, seasons=[1, 2]
        )
        await session.commit()

    assert {(r.season_number, r.status) for r in records} == {(1, "pending"), (2, "pending")}
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.status is RequestStatus.pending


async def test_ensure_seasons_marks_already_in_plex_seasons_available_and_rolls_up_partial(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=701)
    library = FakeLibrary(available_tv_seasons={701: frozenset({1})})

    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, library, media_request_id=show_id, tmdb_id=701, seasons=[1, 2]
        )
        await session.commit()

    by_season = {r.season_number: r.status for r in records}
    assert by_season == {1: "available", 2: "pending"}
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        # A mix of available + pending seasons rolls up to partially_available, not
        # a dishonest fully-available or plain pending.
        assert show.status is RequestStatus.partially_available


async def test_ensure_seasons_is_idempotent_and_grows_the_tracked_set(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=702)

    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=702, seasons=[1]
        )
        await session.commit()
    # Advance season 1 so a second ensure_seasons call must NOT regress it.
    async with sessionmaker_() as session:
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == show_id, SeasonRequest.season_number == 1
        )
        season_row = (await session.execute(stmt)).scalars().one()
        season_row.status = RequestStatus.downloading
        await session.commit()

    # A second POST names season 2 too (a repeat "whole series" request growing
    # the tracked set) -- season 1 must stay 'downloading', not regress to pending.
    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=702, seasons=[1, 2]
        )
        await session.commit()

    by_season = {r.season_number: r.status for r in records}
    assert by_season == {1: "downloading", 2: "pending"}
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        # 'downloading' has precedence in the rollup over 'pending'.
        assert show.status is RequestStatus.downloading


async def test_ensure_seasons_re_arms_an_evicted_season_to_pending(
    sessionmaker_: SessionMaker,
) -> None:
    """C3 regression (ADR-0012): a re-request for a show with a mix of seasons
    (season 1 done, season 2 evicted) must RE-ARM the evicted season to
    'pending' so it becomes grabbable again. Before the fix, 'Request again' on
    an evicted season was a silent no-op forever -- ``ensure()``'s get-or-create
    returns an already-established row unchanged, and the disk-pressure sweep
    already deleted the file, so nothing would ever re-search/re-grab it."""
    show_id = await _make_show(sessionmaker_, tmdb_id=710)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=710, seasons=[1, 2]
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=2, status="evicted"
        )
        await session.commit()

    # A fresh re-request tracking the SAME two seasons -- exactly what
    # create_request's dedup path calls on every POST /requests, including the
    # "Request again" flow the UI drives for a partially_available show.
    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=710, seasons=[1, 2]
        )
        await session.commit()

    by_season = {r.season_number: r.status for r in records}
    assert by_season == {1: "available", 2: "pending"}  # season 2 re-armed, not stuck
    async with sessionmaker_() as session:
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == show_id, SeasonRequest.season_number == 2
        )
        season_row = (await session.execute(stmt)).scalars().one()
        show = await session.get(MediaRequest, show_id)
    assert season_row.status.value == "pending"
    assert show is not None
    assert show.status is RequestStatus.partially_available


async def test_ensure_seasons_re_arms_an_evicted_season_straight_to_available_when_present(
    sessionmaker_: SessionMaker,
) -> None:
    """The re-arm mirrors a FRESH row's already-in-library short-circuit: if Plex
    already has the evicted season again, re-requesting goes straight to
    'available', not 'pending'."""
    show_id = await _make_show(sessionmaker_, tmdb_id=711)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=711, seasons=[1]
        )
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=1, status="evicted"
        )
        await session.commit()

    library = FakeLibrary(available_tv_seasons={711: frozenset({1})})
    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, library, media_request_id=show_id, tmdb_id=711, seasons=[1]
        )
        await session.commit()

    assert {(r.season_number, r.status) for r in records} == {(1, "available")}
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
    assert show is not None
    assert show.status is RequestStatus.available


async def test_ensure_seasons_never_regresses_a_non_evicted_terminal_season(
    sessionmaker_: SessionMaker,
) -> None:
    """The C3 re-arm is scoped EXCLUSIVELY to 'evicted': a season that is
    'failed' (or any other terminal/in-flight status) must be left completely
    untouched by a re-request -- never regressed to 'pending'."""
    show_id = await _make_show(sessionmaker_, tmdb_id=712)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=712, seasons=[1]
        )
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=1, status="failed"
        )
        await session.commit()

    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=712, seasons=[1]
        )
        await session.commit()

    assert {(r.season_number, r.status) for r in records} == {(1, "failed")}


async def test_set_status_updates_one_season_and_recomputes_precedence_rollup(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=703)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=703, seasons=[1, 2]
        )
        await session.commit()

    async with sessionmaker_() as session:
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=1, status="downloading"
        )
        await session.commit()

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        # Season 1 'downloading' + season 2 'pending' -> 'downloading' wins outright.
        assert show.status is RequestStatus.downloading


async def test_mark_completed_then_mark_available_promote_the_rollup(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=704)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=704, seasons=[1]
        )
        await session.commit()

    async with sessionmaker_() as session:
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.status is RequestStatus.completed  # "Finalizing", not yet available

    async with sessionmaker_() as session:
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        # The only tracked season is available -> the whole show rolls up available.
        assert show.status is RequestStatus.available


async def test_mark_no_acceptable_release_updates_a_pending_season(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=705)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=705, seasons=[1]
        )
        await session.commit()

    async with sessionmaker_() as session:
        await season_request_service.mark_no_acceptable_release(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.status is RequestStatus.no_acceptable_release


async def test_set_status_skip_if_terminal_leaves_a_finished_season_untouched(
    sessionmaker_: SessionMaker,
) -> None:
    """``skip_if_terminal=True`` mirrors ``mark_no_acceptable_release``'s
    never-un-terminate guard: a season a PRIOR download already finished must
    never be dragged back to 'searching' by a LATER, unrelated failure for that
    season (e.g. a supplementary per-episode re-grab) -- used by
    ``queue_service``'s failure re-arm call sites."""
    show_id = await _make_show(sessionmaker_, tmdb_id=707)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=707, seasons=[1]
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        await season_request_service.set_status(
            session,
            media_request_id=show_id,
            season_number=1,
            status="searching",
            skip_if_terminal=True,
        )
        await session.commit()

    async with sessionmaker_() as session:
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == show_id, SeasonRequest.season_number == 1
        )
        season_row = (await session.execute(stmt)).scalars().one()
        show = await session.get(MediaRequest, show_id)
    # The finished season is untouched -- never resurrected as a ghost.
    assert season_row.status.value == "available"
    assert show is not None
    assert show.status is RequestStatus.available


async def test_set_status_defaults_to_overwriting_a_finished_season(
    sessionmaker_: SessionMaker,
) -> None:
    """The default (``skip_if_terminal=False``) is unchanged: ``grab_service``
    relies on it to reopen an already-``available``/``completed`` season while it
    chases one more missing episode."""
    show_id = await _make_show(sessionmaker_, tmdb_id=708)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=708, seasons=[1]
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=1, status="downloading"
        )
        await session.commit()

    async with sessionmaker_() as session:
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == show_id, SeasonRequest.season_number == 1
        )
        season_row = (await session.execute(stmt)).scalars().one()
    assert season_row.status.value == "downloading"


async def test_mark_no_acceptable_release_never_unterminates_a_finished_season(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=706)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=706, seasons=[1]
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        await season_request_service.mark_no_acceptable_release(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        # The finished season is untouched -- never resurrected as a ghost.
        assert show.status is RequestStatus.available
