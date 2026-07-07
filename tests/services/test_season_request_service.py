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
from plex_manager.models import (
    Download,
    DownloadHistory,
    DownloadHistoryEvent,
    MediaRequest,
    MediaType,
    RequestStatus,
    SeasonRequest,
)
from plex_manager.ports.repositories import SeasonRequestRecord
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
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


async def test_ensure_seasons_re_grabs_an_evicted_season_even_when_plex_reports_present(
    sessionmaker_: SessionMaker,
) -> None:
    """P1 (ADR-0012 #67): the eviction sweep commits a season 'evicted' BEFORE it
    unlinks the file and before the post-delete Plex refresh, so Plex's fresh
    'present' reading is STALE for that whole window. Re-requesting the evicted
    season must therefore re-grab it ('pending') rather than trust that stale
    reading and re-arm straight to 'available' over a file the sweep is about to
    (or just did) delete -- ``ensure_seasons`` subtracts just-evicted seasons
    (``evicted_seasons``) from the trusted present set. (This deliberately
    supersedes the old "straight to available when present" fast path, which
    trusted Plex presence during the exact window this closes.)"""
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

    # Re-grabbed, NOT minted 'available' off the stale in-Plex reading.
    assert {(r.season_number, r.status) for r in records} == {(1, "pending")}
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
    assert show is not None
    assert show.status is RequestStatus.pending


async def test_ensure_seasons_rearm_loses_cleanly_to_a_concurrent_recovery_restore(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex round-6: the evicted-season re-arm is a CAS from exactly the status
    ``ensure()`` read, never an unconditional write. The eviction recovery can
    RESTORE the row to 'available' (its file is present -- the interrupted purge
    never actually deleted anything) between that read and the re-arm write;
    clobbering the restored row back to 'pending' would queue a duplicate
    download of on-disk content that recovery (already past) couldn't catch
    until a whole sweep later. Simulated by making the read stale: the DB row is
    already 'available' (the committed restore) while ``ensure()`` hands back
    the pre-restore 'evicted' snapshot. The lost CAS must honor the restore --
    the row stays 'available', no pending duplicate, and the re-request dedups
    onto the watchable season exactly like an already-in-Plex one."""
    show_id = await _make_show(sessionmaker_, tmdb_id=730)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=730, seasons=[1]
        )
        # The recovery restore's end state: 'available' over a live file.
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        stmt = select(SeasonRequest).where(SeasonRequest.media_request_id == show_id)
        season_id = (await session.execute(stmt)).scalars().one().id

    real_ensure = SqlSeasonRequestRepository.ensure

    async def stale_ensure(
        self: SqlSeasonRequestRepository,
        media_request_id: int,
        season_number: int,
        *,
        status: str,
        eviction_regrab: bool = False,
    ) -> SeasonRequestRecord:
        record = await real_ensure(
            self,
            media_request_id,
            season_number,
            status=status,
            eviction_regrab=eviction_regrab,
        )
        # The stale pre-restore snapshot: this caller read the row as 'evicted'
        # a moment before the recovery restore committed 'available'.
        return record.model_copy(update={"status": RequestStatus.evicted.value})

    monkeypatch.setattr(SqlSeasonRequestRepository, "ensure", stale_ensure)

    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=730, seasons=[1]
        )
        await session.commit()

    # The lost CAS honored the restore: returned AND persisted 'available',
    # never clobbered back to a duplicate-downloading 'pending'.
    assert {(r.season_number, r.status) for r in records} == {(1, "available")}
    async with sessionmaker_() as session:
        season_row = await session.get(SeasonRequest, season_id)
        show = await session.get(MediaRequest, show_id)
    assert season_row is not None
    assert season_row.status is RequestStatus.available
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


async def test_ensure_seasons_re_arm_keeps_library_path_but_resets_backoff(
    sessionmaker_: SessionMaker,
) -> None:
    """#117 regression: re-requesting an EVICTED season resets the search backoff
    ladder (so the operator's fresh request is not throttled by the evicted run's
    exhausted attempts) but DELIBERATELY KEEPS the ``library_path`` eviction
    breadcrumb. The re-arm can land while the eviction purge is still in flight
    (the claim commits 'evicted' + breadcrumb BEFORE the delete), so the file may
    still be on disk; the breadcrumb is owned end-to-end by the eviction lifecycle
    (the finalize clears it after a successful delete, a failed-delete restore/fold
    keeps it). Clearing it here -- as the earlier #75 re-arm did, mirroring
    report-issue's already-purged ``reset_for_research`` -- would strand a season
    folded back to 'available' over a still-present file with NO eviction/report
    handle, so disk pressure could never reclaim it (the leak #117 closes)."""
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
    # The breadcrumb is PRESERVED (owned by the eviction lifecycle, not cleared by
    # the re-arm) so a folded-back or finalized eviction still has a handle ...
    assert season_row.library_path == "/media/tv/Some Show/Season 01"
    # ... while the search backoff ladder IS reset for the operator's fresh request.
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


async def test_mark_available_clears_the_eviction_regrab_marker(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex round-2 finding 3 (TV twin): once a season regrab genuinely imports
    and Plex confirms it watchable, it is exactly as settled as any other
    available season -- the marker must retire, or a LATER, unrelated eviction's
    restore could read a settled row as still "in flight" off nothing but stale
    history."""
    show_id = await _make_show(sessionmaker_, tmdb_id=740)
    async with sessionmaker_() as session:
        season = SeasonRequest(
            media_request_id=show_id,
            season_number=1,
            status=RequestStatus.completed,
            eviction_regrab=True,  # this season WAS an eviction's own regrab
        )
        session.add(season)
        await session.commit()
        season_id = season.id

    async with sessionmaker_() as session:
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
    assert season is not None
    assert season.status is RequestStatus.available
    assert season.eviction_regrab is False  # cleared by the fix


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


async def test_mark_no_acceptable_release_does_not_overwrite_a_concurrent_downloading_season(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #72: the old read-then-write shape read the season's current status,
    checked it was non-TERMINAL, then wrote unconditionally -- a concurrent grab (a
    lower-ranked auto-grab candidate, a manual re-grab) moving the season to
    ``downloading`` in that gap would be silently regressed back to the
    ``no_acceptable_release`` dead-end even though a real download was now live.
    The genuine compare-and-swap closes the gap regardless of WHEN the concurrent
    write lands relative to this call -- seeding the season already at
    ``downloading`` exercises the exact postcondition of that race. A lost CAS
    must also never recompute (and persist) the parent rollup off a row it did
    not actually get to move."""
    show_id = await _make_show(sessionmaker_, tmdb_id=708)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=708, seasons=[1]
        )
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=1, status="downloading"
        )
        await session.commit()

    async with sessionmaker_() as session:
        parked = await season_request_service.mark_no_acceptable_release(
            session, media_request_id=show_id, season_number=1
        )
        await session.rollback()
    assert parked is False  # the CAS lost the race -- never silently claims a win

    async with sessionmaker_() as session:
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == show_id, SeasonRequest.season_number == 1
        )
        season_row = (await session.execute(stmt)).scalars().one()
        show = await session.get(MediaRequest, show_id)
    assert season_row.status is RequestStatus.downloading  # never regressed
    assert show is not None
    assert show.status is RequestStatus.downloading  # parent rollup left untouched too


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


async def test_reset_for_research_clears_the_eviction_regrab_marker(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex round-2 finding 3 (TV twin): report-issue re-arming a season for a
    brand-new search is the row leaving "some eviction's own in-flight regrab"
    behind -- the marker must clear, or a LATER, unrelated eviction's restore
    could cancel the operator's live re-search purely because of the row's stale
    history (see ``test_eviction_service``'s composed regression for the full
    downstream effect)."""
    show_id = await _make_show(sessionmaker_, tmdb_id=738)
    async with sessionmaker_() as session:
        season = SeasonRequest(
            media_request_id=show_id,
            season_number=1,
            status=RequestStatus.available,
            eviction_regrab=True,  # this season WAS an eviction's own regrab
        )
        session.add(season)
        await session.commit()
        season_id = season.id

    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()

    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
    assert season is not None
    assert season.status is RequestStatus.searching
    assert season.eviction_regrab is False  # cleared by the fix


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


async def test_heal_completed_at_keeps_and_repairs_around_a_masked_sibling_import(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex round-2 #2 (masked-sibling TOCTOU): ``heal_completed_at``'s two
    conditional UPDATEs carry the qualifying-done-season predicate in their OWN
    WHERE, evaluated at UPDATE time -- never off a prior Python snapshot -- and the
    second (re-stamp) statement repairs the aftermath a stale-snapshot clear leaves
    behind. The masked sibling: S2's import finalizes while S3 (``downloading``,
    higher rollup precedence) masks the parent status, so S2's transaction never
    touches the parent row -- its ``stamp_completed_at_if_unset`` no-ops against
    the still-non-null stale stamp of the season about to be reported. Phase A: a
    reset AFTER S2's commit sees it in the clear's WHERE and the stamp survives.
    Phase B: simulating the MVCC interleave (the clear committed off a snapshot
    that predated S2's commit -- stamp NULL, S2 committed-done), any heal
    invocation re-stamps off the committed done season, so the show never ends
    permanently stampless while a season genuinely backs it."""
    show_id = await _make_show(sessionmaker_, tmdb_id=732)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=732, seasons=[1, 2, 3]
        )
        # S1: the future culprit -- genuinely imported (breadcrumb) and stamps first.
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=1,
            library_path="/media/tv/Some Show/Season 01",
        )
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=1
        )
        # S3: still downloading -- the higher-precedence mask over any later rollup.
        await season_request_service.set_status(
            session, media_request_id=show_id, season_number=3, status="downloading"
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        stale_stamp = show.completed_at
        assert stale_stamp is not None

    # A SEPARATE session commits the MASKED sibling S2's genuine import: its own
    # stamp no-ops (completed_at already non-null) and the rollup stays masked at
    # 'downloading', so S2's transaction leaves the parent row untouched.
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
        assert show.status is RequestStatus.downloading  # masked -- parent untouched
        assert show.completed_at == stale_stamp

    # Phase A: report-issue the culprit S1 AFTER S2's commit. The heal's clear
    # re-asserts its WHERE at UPDATE time -> committed S2 backs the stamp -> the
    # first stamp survives untouched (never moves once genuinely backed).
    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at == stale_stamp

    # Phase B: simulate the MVCC aftermath the pre-fix clear leaves behind on the
    # Postgres posture -- the clear committed off a statement snapshot predating
    # S2's commit (stamp NULL despite S2 committed-done, S2's own stamp no-op'd).
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        show.completed_at = None
        await session.commit()
    # ANY heal invocation is self-correcting: statement 2 (fresh snapshot) sees the
    # committed done season and re-stamps, ending non-null.
    async with sessionmaker_() as session:
        await SqlRequestRepository(session).heal_completed_at(show_id)
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None  # reflects the committed done S2


async def test_reset_keeps_stamp_backed_by_legacy_imported_season_without_breadcrumb(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex round-2 #1 (legacy imported seasons): on upgraded installs a season
    imported BEFORE ``SeasonRequest.library_path`` existed has a NULL breadcrumb
    (``models.SeasonRequest.library_path``: "None for seasons imported before this
    breadcrumb existed") -- but it still has the OTHER committed import marker: the
    placing ``Download`` row finalized to ``imported`` for its ``(media_request_id,
    season)`` (the same linkage report-issue uses to resolve its culprit,
    ``find_latest_imported_for_request``). Such a done season genuinely backs the
    parent's ``completed_at``, so a sibling's report-issue reset must PRESERVE the
    stamp -- under the breadcrumb-only round-1 discriminator it was wrongly
    cleared. A Plex-present-only season stays excluded (no grab ever ran for it, so
    no imported download row exists for the pair -- pinned by
    ``test_reset_clears_completed_at_when_only_a_plex_present_sibling_remains``)."""
    show_id = await _make_show(sessionmaker_, tmdb_id=733)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=733, seasons=[1, 2]
        )
        # S1: LEGACY import -- no breadcrumb, but the imported Download row a real
        # pre-breadcrumb import left behind, then Plex-confirmed available.
        session.add(
            Download(
                torrent_hash="legacy-s1-import",
                status="imported",
                media_request_id=show_id,
                media_type=MediaType.tv,
                tmdb_id=733,
                season=1,
            )
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        # S2: a modern import with the breadcrumb.
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
        first_stamp = show.completed_at
        assert first_stamp is not None

    # report-issue on S2: the legacy S1 (done + imported-download evidence, NULL
    # breadcrumb) still backs the stamp -> preserved.
    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at == first_stamp

    # report-issue on S1 too: its status leaves (completed, available), so the old
    # imported download alone no longer qualifies -> nothing backs the stamp -> clear.
    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is None


async def test_ensure_seasons_evicted_re_arm_heals_stale_completed_at(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex round-2 #3 (evicted re-arm): eviction deliberately never clears the
    parent's ``completed_at`` (a reclaimed file is not an un-completion), but a
    RE-REQUEST of the evicted season turns the stale stamp into a trap --
    ``stamp_completed_at_if_unset``'s IS NULL guard would block the re-import from
    ever re-stamping. The re-arm now runs the same guarded heal as report-issue:
    with NO genuinely-imported done sibling left (S1 evicted -> pending, S2 still
    pending), the stamp is cleared, and S1's re-import re-stamps."""
    show_id = await _make_show(sessionmaker_, tmdb_id=734)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=734, seasons=[1, 2]
        )
        # S1 genuinely imports (breadcrumb + the imported download a real run has)
        # and is Plex-confirmed -> the parent stamps.
        session.add(
            Download(
                torrent_hash="evicted-s1-import",
                status="imported",
                media_request_id=show_id,
                media_type=MediaType.tv,
                tmdb_id=734,
                season=1,
            )
        )
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=1,
            library_path="/media/tv/Some Show/Season 01",
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None
        season1 = (
            (
                await session.execute(
                    select(SeasonRequest).where(
                        SeasonRequest.media_request_id == show_id,
                        SeasonRequest.season_number == 1,
                    )
                )
            )
            .scalars()
            .one()
        )
        season1_id = season1.id

    # The disk-pressure sweep evicts S1 -- the stamp deliberately survives eviction.
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
        assert show.completed_at is not None  # eviction is not an un-completion

    # Re-requesting S1 re-arms it (evicted -> pending) AND heals the stamp: S1 is
    # no longer done and nothing else genuinely backs it (the old imported download
    # row no longer counts once the season left completed/available) -> cleared.
    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=734, seasons=[1]
        )
        await session.commit()
    assert [(r.season_number, r.status) for r in records] == [(1, "pending")]
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is None

    # S1 re-imports -> the IS NULL guard no longer blocks -> the parent re-stamps.
    async with sessionmaker_() as session:
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=1,
            library_path="/media/tv/Some Show/Season 01",
        )
        await season_request_service.mark_completed(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None


async def test_ensure_seasons_evicted_re_arm_preserves_stamp_backed_by_done_sibling(
    sessionmaker_: SessionMaker,
) -> None:
    """The evicted re-arm's heal is GUARDED, exactly like report-issue's: when a
    genuinely-imported sibling is STILL done (S2 completed with its breadcrumb),
    re-requesting the evicted S1 must leave the parent's first-completion stamp
    standing -- that historical fact is still backed."""
    show_id = await _make_show(sessionmaker_, tmdb_id=735)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=735, seasons=[1, 2]
        )
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=1,
            library_path="/media/tv/Some Show/Season 01",
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
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
        first_stamp = show.completed_at
        assert first_stamp is not None
        season1 = (
            (
                await session.execute(
                    select(SeasonRequest).where(
                        SeasonRequest.media_request_id == show_id,
                        SeasonRequest.season_number == 1,
                    )
                )
            )
            .scalars()
            .one()
        )
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

    # Re-request the evicted S1: S2 (genuinely imported, still 'completed') backs
    # the stamp, so the re-arm's heal preserves it.
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=735, seasons=[1]
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at == first_stamp


async def test_evicted_re_arm_to_pending_does_not_resurrect_stamp_via_stale_download(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex round-3: eviction never touches the old ``Download`` row, so its
    ``imported`` status survives the file's deletion. When the evicted season is
    re-requested while Plex STILL reports it present, ``ensure_seasons`` re-arms
    it evicted -> ``available`` and clears the breadcrumb -- and without the
    eviction-ordering clause the stale pre-eviction download would make the
    parent's old ``completed_at`` look backed. The heal's download arm now
    requires no ``evicted`` history event for the show newer than the download's
    latest ``imported`` event (both committed, append-only ``download_history``
    rows -- the eviction row is exactly what ``_evict_one`` writes:
    ``torrent_hash=None``, ``tmdb_id`` set), so the presence-derived ``available``
    cannot resurrect a stamp no current import supports."""
    show_id = await _make_show(sessionmaker_, tmdb_id=736)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=736, seasons=[1, 2]
        )
        # S1's full modern import: the placing download, its hash-tied 'imported'
        # history event (import_service._import_tv_locked), the breadcrumb, and
        # Plex confirmation -> the parent stamps.
        session.add(
            Download(
                torrent_hash="round3-s1-import",
                status="imported",
                media_request_id=show_id,
                media_type=MediaType.tv,
                tmdb_id=736,
                season=1,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=736,
                torrent_hash="round3-s1-import",
                event_type=DownloadHistoryEvent.imported,
                source_title=None,
                message="imported Some.Show.S01.mkv to Some Show/Season 01",
            )
        )
        await season_request_service.set_library_path(
            session,
            media_request_id=show_id,
            season_number=1,
            library_path="/media/tv/Some Show/Season 01",
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is not None
        season1_id = (
            (
                await session.execute(
                    select(SeasonRequest).where(
                        SeasonRequest.media_request_id == show_id,
                        SeasonRequest.season_number == 1,
                    )
                )
            )
            .scalars()
            .one()
            .id
        )

    # The sweep evicts S1 exactly as _evict_one does: the season CAS plus the
    # 'evicted' history row (torrent_hash=None -- not tied to any download).
    async with sessionmaker_() as session:
        changed = await season_request_service.set_status_if_in(
            session,
            media_request_id=show_id,
            season_request_id=season1_id,
            status=RequestStatus.evicted.value,
            allowed_from=frozenset({RequestStatus.available.value}),
            tolerate_active_conflict=True,
        )
        session.add(
            DownloadHistory(
                tmdb_id=736,
                torrent_hash=None,
                event_type=DownloadHistoryEvent.evicted,
                source_title="Some Show",
                message=(
                    "evicted season 1: watched, past grace period, "
                    "disk-pressure relief (/media/tv/Some Show/Season 01)"
                ),
            )
        )
        await session.commit()
    assert changed is True

    # Re-request S1 while Plex STILL reports it present: PR 117 deliberately
    # subtracts just-evicted seasons from trusted Plex presence, so this re-arms to
    # 'pending' (backoff reset; the eviction breadcrumb is deliberately PRESERVED
    # -- owned by the eviction lifecycle, #117) instead of trusting a stale
    # in-library reading. The stale pre-eviction download must NOT back the old
    # stamp, so the heal clears it.
    library = FakeLibrary(available_tv_seasons={736: frozenset({1})})
    async with sessionmaker_() as session:
        records = await season_request_service.ensure_seasons(
            session, library, media_request_id=show_id, tmdb_id=736, seasons=[1]
        )
        await session.commit()
    assert [(r.season_number, r.status) for r in records] == [(1, "pending")]
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is None  # nothing current supports the old stamp
        season = (
            (
                await session.execute(
                    select(SeasonRequest).where(
                        SeasonRequest.media_request_id == show_id,
                        SeasonRequest.season_number == 1,
                    )
                )
            )
            .scalars()
            .one()
        )
    # The breadcrumb is preserved through the re-arm (#117): this test's eviction
    # never ran the finalize clear, so the row is still 'evicted' + breadcrumb, and
    # the re-arm no longer strips it -- the eviction lifecycle owns that clear.
    assert season.library_path == "/media/tv/Some Show/Season 01"
    assert season.search_attempts == 0
    assert season.next_search_at is None


async def test_reimport_after_eviction_restores_download_evidence(
    sessionmaker_: SessionMaker,
) -> None:
    """The round-3 ordering is directional, not a blanket kill: a season
    RE-IMPORTED after its eviction appends a NEWER hash-tied 'imported' history
    event than the show's last 'evicted' event, so its download evidence validly
    counts again -- pinned through the RESET path (report-issue on a sibling),
    which shares the heal's predicate with the re-arm path. Modeled without the
    breadcrumb (like a legacy import) to isolate the download-evidence arm."""
    show_id = await _make_show(sessionmaker_, tmdb_id=737)
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=737, seasons=[1, 2]
        )
        # S1's first import (download + hash-tied imported event, no breadcrumb).
        session.add(
            Download(
                torrent_hash="round3-s1-cycle",
                status="imported",
                media_request_id=show_id,
                media_type=MediaType.tv,
                tmdb_id=737,
                season=1,
            )
        )
        session.add(
            DownloadHistory(
                tmdb_id=737,
                torrent_hash="round3-s1-cycle",
                event_type=DownloadHistoryEvent.imported,
                source_title=None,
                message="imported Some.Show.S01.mkv to Some Show/Season 01",
            )
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        season1_id = (
            (
                await session.execute(
                    select(SeasonRequest).where(
                        SeasonRequest.media_request_id == show_id,
                        SeasonRequest.season_number == 1,
                    )
                )
            )
            .scalars()
            .one()
            .id
        )

    # Evicted (CAS + history row), then re-requested -> pending re-arm clears the
    # stale stamp (nothing genuinely done remains).
    async with sessionmaker_() as session:
        changed = await season_request_service.set_status_if_in(
            session,
            media_request_id=show_id,
            season_request_id=season1_id,
            status=RequestStatus.evicted.value,
            allowed_from=frozenset({RequestStatus.available.value}),
            tolerate_active_conflict=True,
        )
        session.add(
            DownloadHistory(
                tmdb_id=737,
                torrent_hash=None,
                event_type=DownloadHistoryEvent.evicted,
                source_title="Some Show",
                message=(
                    "evicted season 1: watched, past grace period, "
                    "disk-pressure relief (/media/tv/Some Show/Season 01)"
                ),
            )
        )
        await session.commit()
    assert changed is True
    async with sessionmaker_() as session:
        await season_request_service.ensure_seasons(
            session, None, media_request_id=show_id, tmdb_id=737, seasons=[1]
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at is None

    # S1 re-imports through the SAME reused torrent row: a NEW hash-tied
    # 'imported' event lands AFTER the eviction event, and Plex re-confirms.
    async with sessionmaker_() as session:
        session.add(
            DownloadHistory(
                tmdb_id=737,
                torrent_hash="round3-s1-cycle",
                event_type=DownloadHistoryEvent.imported,
                source_title=None,
                message="imported Some.Show.S01.mkv to Some Show/Season 01",
            )
        )
        await season_request_service.mark_available(
            session, media_request_id=show_id, season_number=1
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        restamp = show.completed_at
        assert restamp is not None

    # RESET path: report-issue the sibling S2. S1's re-import is NEWER than the
    # show's last eviction, so its download evidence backs the stamp -> preserved.
    async with sessionmaker_() as session:
        await season_request_service.reset_for_research(
            session, media_request_id=show_id, season_number=2
        )
        await session.commit()
    async with sessionmaker_() as session:
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.completed_at == restamp
