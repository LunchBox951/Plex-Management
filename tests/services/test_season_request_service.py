"""season_request_service — per-season lifecycle + the parent rollup it recomputes.

``ensure_seasons`` / ``set_status`` / ``mark_completed`` / ``mark_available`` /
``mark_no_acceptable_release`` are exercised primarily through the PARENT
``MediaRequest.status`` they leave behind (the pure fold is unit-tested directly
in ``tests/domain/test_season_rollup.py``); these tests pin the wiring: every
season-status write recomputes and persists the rollup in the SAME call.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import PlexLibraryError
from plex_manager.models import MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.repositories.requests import SqlRequestRepository
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


async def test_ensure_seasons_presence_check_failure_logs_tmdb_id_via_extra(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Plex outage during the presence crawl logs ``tmdb_id`` via ``extra=``,
    never interpolated into the message text, and every season still falls
    through to ``pending`` (never blocked)."""
    show_id = await _make_show(sessionmaker_, tmdb_id=7099)
    library = FakeLibrary(raises=PlexLibraryError("plex is down"))

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.season_request_service"):
        async with sessionmaker_() as session:
            records = await season_request_service.ensure_seasons(
                session, library, media_request_id=show_id, tmdb_id=7099, seasons=[1, 2]
            )
            await session.commit()

    assert {r.status for r in records} == {"pending"}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a warning to be logged"
    assert "7099" not in warnings[0].getMessage()
    assert getattr(warnings[0], "tmdb_id", None) == 7099


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


async def test_ensure_seasons_re_arm_clears_library_path_and_backoff_on_evicted_reuse(
    sessionmaker_: SessionMaker,
) -> None:
    """#75 regression: re-requesting an EVICTED season must not just flip its
    status back to pending/available -- it must ALSO clear the library_path
    eviction breadcrumb and reset the search backoff ladder, exactly mirroring
    ``reset_for_research``'s report-issue re-arm (ADR-0013/ADR-0014). Before the
    fix, a re-requested evicted season inherited its stale library_path (a
    later eviction sweep would misread it as still reclaimable) and its stale
    search_attempts/next_search_at backoff (throttling the operator's brand-new
    request exactly like a fresh row never would)."""
    show_id = await _make_show(sessionmaker_, tmdb_id=730)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=730, seasons=[1]
        )
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=1,
            library_path="/media/tv/Some Show/Season 01",
        )
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=1, status="evicted"
        )
        await session.commit()

    # Simulate a backoff ladder the season accrued before it was evicted (e.g. a
    # stalled no_acceptable_release run) directly on the row.
    async with sessionmaker_() as session:
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == show_id, SeasonRequest.season_number == 1
        )
        season_row = (await session.execute(stmt)).scalars().one()
        season_row.search_attempts = 6
        season_row.next_search_at = datetime.now(UTC) + timedelta(hours=24)
        await session.commit()

    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=730, seasons=[1]
        )
        await session.commit()

    assert {(r.season_number, r.status) for r in records} == {(1, "pending")}
    async with sessionmaker_() as session:
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == show_id, SeasonRequest.season_number == 1
        )
        season_row = (await session.execute(stmt)).scalars().one()
    assert season_row.library_path is None
    assert season_row.search_attempts == 0
    assert season_row.next_search_at is None


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


async def test_mark_completed_stamps_parent_completed_at_on_first_season_only(
    sessionmaker_: SessionMaker,
) -> None:
    """A TV parent's ``completed_at`` (never touched by the movie-level
    ``mark_completed``/``mark_available``, which a computed rollup does not run) is
    stamped the FIRST time a tracked season is imported, and never re-stamped when a
    LATER season completes -- so it honestly records the show's first completion.
    Regression for the finding that every TV time-to-watch interval read
    'unknown'."""
    show_id = await _make_show(sessionmaker_, tmdb_id=720)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=720, seasons=[1, 2]
        )
        await session.commit()

    # Before any season completes, the parent has no completion timestamp.
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is None

    # Season 1 completes -> the parent's completed_at is stamped for the first time.
    async with sessionmaker_() as session:
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        first_stamp = show.completed_at
        assert first_stamp is not None
        # A partially-complete show is still honestly partial, but completion is now
        # recorded.
        assert show.status is RequestStatus.partially_available

    # Season 2 completes later -> the first stamp is preserved, never moved.
    async with sessionmaker_() as session:
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at == first_stamp  # NOT re-stamped by the later season


async def test_mixed_tv_request_stamps_completed_at_only_when_a_season_is_imported(
    sessionmaker_: SessionMaker,
) -> None:
    """R5 P2: a TV request that MIXES an already-in-Plex season (created ``available``
    with no ``library_path`` by ``ensure_seasons`` -- no import ever ran) with a
    missing season must NOT stamp the parent ``completed_at`` at REQUEST time. The
    stamp is confined to genuine import/availability transitions
    (``mark_completed``/``mark_available``), so ``ensure_seasons``'s already-present
    creation never fires it; otherwise the later-imported season's telemetry interval
    would start at request/Plex-verification time instead of its own import."""
    show_id = await _make_show(sessionmaker_, tmdb_id=723)
    library = FakeLibrary(available_tv_seasons={723: frozenset({1})})

    # Creation: season 1 already in Plex (-> available, no import), season 2 missing.
    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, library, media_request_id=show_id, tmdb_id=723, seasons=[1, 2]
        )
        await session.commit()
    assert {r.season_number: r.status for r in records} == {1: "available", 2: "pending"}

    # No import has happened yet -> completed_at must still be unset, even though one
    # season is already ``available`` off the Plex-presence short-circuit.
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is None

    # The missing season is imported (a real transition path) -> NOW the stamp fires.
    async with sessionmaker_() as session:
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None


async def test_completed_at_is_stamped_at_season_level_even_when_rollup_is_masked(
    sessionmaker_: SessionMaker,
) -> None:
    """The stamp is decided at the SEASON level, not off the rollup ``status``:
    rollup precedence (``downloading`` etc.) can mask a just-completed season while
    a sibling is still in flight, but the show's first completion has still happened
    and must be recorded. Season 1 completes while season 2 is still downloading ->
    parent status is (masked) ``downloading`` yet ``completed_at`` is stamped."""
    show_id = await _make_show(sessionmaker_, tmdb_id=721)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=721, seasons=[1, 2]
        )
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=2, status="downloading"
        )
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        # Precedence masks the completed season -> the rollup reads downloading...
        assert show.status is RequestStatus.downloading
        # ...but the show's first completion is still recorded off the season level.
        assert show.completed_at is not None


async def test_eviction_rollup_never_stamps_completed_at(
    sessionmaker_: SessionMaker,
) -> None:
    """An eviction (``tolerate_active_conflict=True``) recomputes the parent rollup
    but must NEVER stamp ``completed_at``: a file being reclaimed is not a
    completion. A pre-stamp show (``completed_at is None``) whose season is evicted
    while a sibling season is still available leaves ``completed_at`` None -- the
    stamp is confined to the strict, forward-transition branch."""
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=722,
            media_type=MediaType.tv,
            title="Pre-stamp Show",
            status=RequestStatus.available,
            completed_at=None,  # a row predating the completed_at stamp
        )
        session.add(show)
        await session.flush()
        season1 = SeasonRequest(
            media_request_id=show.id, season_number=1, status=RequestStatus.available
        )
        season2 = SeasonRequest(
            media_request_id=show.id, season_number=2, status=RequestStatus.available
        )
        session.add_all([season1, season2])
        await session.commit()
        show_id = show.id
        season1_id = season1.id

    async with sessionmaker_() as session:
        changed = await season_request_service.set_status_if_in(
            session,
            media_request_id=show_id,
            season_request_id=season1_id,
            status=RequestStatus.evicted.value,
            allowed_from=frozenset({RequestStatus.available.value}),
            tolerate_active_conflict=True,
        )
        await session.commit()
    assert changed is True

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        # One season available + one evicted -> partially_available, but the
        # eviction path never stamps completed_at.
        assert show.status is RequestStatus.partially_available
        assert show.completed_at is None


async def test_reset_for_research_clears_completed_at_when_no_season_remains_done(
    sessionmaker_: SessionMaker,
) -> None:
    """#76 regression: a single-season show's report-issue reset must clear the
    PARENT's stale ``completed_at`` so a redone season re-stamps on the next
    genuine completion. Before the fix, ``reset_for_research`` only re-armed the
    SEASON row -- the parent's ``completed_at`` (stamped once, idempotently, via
    ``stamp_completed_at_if_unset``) was left standing, so ``mark_completed``'s
    ``IS NULL`` guard silently skipped re-stamping it forever."""
    show_id = await _make_show(sessionmaker_, tmdb_id=724)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=724, seasons=[1]
        )
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None  # the first (only) stamp

    # report-issue: no OTHER tracked season is complete/available, so the reset
    # must clear the now-stale parent stamp.
    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is None

    # Redoing the season completes it again -> the parent re-stamps, honestly
    # reflecting the NEW completion rather than staying permanently None.
    async with sessionmaker_() as session:
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None


async def test_reset_for_research_leaves_completed_at_when_a_sibling_is_still_done(
    sessionmaker_: SessionMaker,
) -> None:
    """The #76 fix RECOMPUTES rather than blindly clears: a multi-season show
    with one season STILL genuinely complete/available must keep the parent's
    ``completed_at`` intact when a DIFFERENT season is report-issue reset -- the
    show's first-completion fact does not become false just because a sibling
    season is being redone (the documented approximation in
    ``retention_telemetry_service._candidate_context``).

    Season 1 is a GENUINE import (it has a ``library_path`` breadcrumb, exactly as
    ``import_service._import_tv_locked`` writes one in the same transaction as
    ``mark_completed``), so the guarded clear counts it as still backing the stamp
    -- distinct from a Plex-present-only ``available`` season (no ``library_path``),
    which must NOT (Codex P2 #1)."""
    show_id = await _make_show(sessionmaker_, tmdb_id=725)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=725, seasons=[1, 2]
        )
        # A genuine import stamps the eviction breadcrumb (library_path) in the SAME
        # transaction as mark_completed -- mirror that so season 1 reads as truly
        # imported, not merely Plex-present.
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=1,
            library_path="/media/tv/Some Show/Season 01",
        )
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=1
        )
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=2,
            library_path="/media/tv/Some Show/Season 02",
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        first_stamp = show.completed_at
        assert first_stamp is not None

    # report-issue on season 2 only -- season 1 is still 'completed', so the
    # parent's completed_at must be left standing, not cleared.
    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at == first_stamp


async def test_reset_clears_completed_at_when_only_a_plex_present_sibling_remains(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex P2 #1: a Plex-present-only ``available`` season must NOT count as
    backing the parent's ``completed_at``. ``ensure_seasons`` creates an
    already-in-Plex season straight to ``available`` with NO ``library_path`` (no
    import ran); only a genuine import writes one (``import_service.
    _import_tv_locked`` sets ``library_path`` in the SAME transaction as
    ``mark_completed``). So when the ONLY other still-``available`` sibling is
    Plex-present, report-issuing the genuinely-imported season must CLEAR the now
    stale stamp -- otherwise the re-import can never re-stamp
    (``stamp_completed_at_if_unset`` would see a non-null ``completed_at``). Before
    the fix the predicate counted S1's Plex-present ``available`` and wrongly
    preserved the stamp."""
    show_id = await _make_show(sessionmaker_, tmdb_id=730)
    library = FakeLibrary(available_tv_seasons={730: frozenset({1})})

    # S1 already in Plex -> available, NO library_path. S2 missing -> pending.
    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, library, media_request_id=show_id, tmdb_id=730, seasons=[1, 2]
        )
        # S2 genuinely imports: breadcrumb + mark_completed, mirroring import_service.
        # This is the only genuine completion, so it stamps the parent completed_at.
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=2,
            library_path="/media/tv/Some Show/Season 02",
        )
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()
    assert {r.season_number: r.status for r in records} == {1: "available", 2: "pending"}

    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None  # stamped by S2's genuine import

    # report-issue on S2: the only OTHER 'available' season (S1) is Plex-present
    # (library_path IS NULL), so it does NOT back the stamp -> the stamp must clear.
    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is None  # NOT preserved by the Plex-present sibling

    # S2 re-imports -> the parent re-stamps (the IS NULL guard no longer blocks it).
    async with sessionmaker_() as session:
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=2,
            library_path="/media/tv/Some Show/Season 02",
        )
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None


async def test_clear_completed_at_guard_reasserts_a_committed_done_sibling(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex P2 #2 (TOCTOU): ``clear_completed_at_if_no_imported_done_season`` is a
    single GUARDED atomic UPDATE -- it re-asserts 'no genuinely-imported done season
    remains' (a correlated ``NOT EXISTS`` over ``status IN (completed, available)
    AND library_path IS NOT NULL``) in its WHERE at UPDATE time, NOT off a prior
    in-memory snapshot. When a sibling's genuine (imported) completion has committed,
    the UPDATE matches zero rows and the fresh, valid stamp survives; the pre-fix
    unconditional clear -- deciding off a snapshot taken before that commit -- would
    have erased it. Mirrors the DB-authoritative CAS in ``set_status_if_in`` and
    holds on SQLite and the Postgres-ready posture the repo keeps."""
    show_id = await _make_show(sessionmaker_, tmdb_id=732)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=732, seasons=[1, 2]
        )
        # An earlier completion to protect (round-tripped through the DB below so the
        # later equality compares like-for-like on SQLite's naive datetimes).
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.completed_at = datetime.now(UTC)
        await session.commit()

    # A separate session commits S2's genuine import (library_path + completed) --
    # the sibling completion landing before the clear. Its own recompute cannot
    # re-stamp (completed_at already set), but it now BACKS the stamp.
    async with sessionmaker_() as session:
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=2,
            library_path="/media/tv/Some Show/Season 02",
        )
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        surviving_stamp = show.completed_at
        assert surviving_stamp is not None

    # The guarded clear re-asserts at UPDATE time -> S2 genuinely backs the stamp ->
    # zero rows matched, nothing cleared.
    async with sessionmaker_() as session:
        cleared = await SqlRequestRepository(session).clear_completed_at_if_no_imported_done_season(
            show_id
        )
        await session.commit()
    assert cleared is False
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at == surviving_stamp  # survived the clear
