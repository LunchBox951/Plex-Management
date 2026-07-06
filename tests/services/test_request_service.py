"""request_service — active-request dedup recovers from the UNIQUE-index race.

A concurrent POST /requests for the same ``(tmdb_id, media_type)`` can both pass
the application-level ``find_active`` check, then both INSERT. The partial UNIQUE
index over active statuses rejects the loser; ``create_request`` catches the
``IntegrityError`` and resolves to the existing active request instead of crashing.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex import PlexLibrary
from plex_manager.adapters.plex.library import PlexLibraryError, reset_caches
from plex_manager.models import (
    MediaRequest,
    MediaType,
    RequestDedupLock,
    RequestStatus,
    SeasonRequest,
    User,
)
from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.ports.repositories import RequestRecord
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import request_service
from tests.web.fakes import FakeLibrary, FakeTmdb

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


async def test_mark_no_acceptable_release_does_not_overwrite_a_concurrent_downloading_request(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #72: the old read-then-write shape read the current status, checked
    it was non-TERMINAL, then wrote unconditionally -- a concurrent writer (a
    lower-ranked auto-grab candidate, a manual re-grab) moving the row to
    ``downloading`` in that gap would be silently regressed back to the
    ``no_acceptable_release`` dead-end even though a real download was now live.
    The genuine compare-and-swap closes the gap regardless of WHEN the
    concurrent write lands relative to this call -- seeding the row already at
    ``downloading`` exercises the exact postcondition of that race: the CAS's
    ``WHERE status IN (...)`` is evaluated by the database, sees ``downloading``
    is not in the parkable set, and updates zero rows."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=889,
            media_type=MediaType.movie,
            title="Arrival",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        parked = await request_service.mark_no_acceptable_release(session, request_id)
        await session.rollback()
    assert parked is False  # the CAS lost the race -- never silently claims a win

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.status is RequestStatus.downloading  # never regressed


async def test_mark_no_acceptable_release_parks_and_persists_on_a_won_cas(
    sessionmaker_: SessionMaker,
) -> None:
    """The success path of the CAS: a request in a parkable status (``searching``)
    is moved to ``no_acceptable_release`` and, since the function is FLUSH-ONLY,
    the caller's own commit is what makes it durable for a later session to see."""
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=890,
            media_type=MediaType.movie,
            title="Arrival",
            status=RequestStatus.searching,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    async with sessionmaker_() as session:
        parked = await request_service.mark_no_acceptable_release(session, request_id)
        await session.commit()
    assert parked is True

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.status is RequestStatus.no_acceptable_release


async def test_movie_availability_check_failure_logs_tmdb_id_via_extra(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A Plex outage during the in-library short-circuit is logged with
    ``tmdb_id`` carried via ``extra=`` (never interpolated into the message text)
    -- see CONTRIBUTING.md's logging convention / CodeQL py/log-injection."""
    tmdb = FakeTmdb(movies={999: MovieMetadata(tmdb_id=999, title="Arrival", year=2016)})
    library = FakeLibrary(raises=PlexLibraryError("plex is down"))

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.request_service"):
        async with sessionmaker_() as session:
            record = await request_service.create_request(
                session, tmdb, tmdb_id=999, media_type="movie", library=library
            )

    assert record.status == RequestStatus.pending.value  # never blocked by the outage
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a warning to be logged"
    assert "999" not in warnings[0].getMessage()  # the id is not interpolated into the text
    assert getattr(warnings[0], "tmdb_id", None) == 999  # ...but present as a structured field


async def test_create_request_collapses_racing_in_library_available_rows(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent in-library short-circuits each insert an ``available`` row (the
    active-dedup partial UNIQUE index excludes terminal ``available``, so no
    IntegrityError fires). The post-commit reconcile collapses the race loser: the
    second create returns the FIRST row and exactly one available row survives (F9)."""
    tmdb = FakeTmdb(movies={555: MovieMetadata(tmdb_id=555, title="Sicario", year=2015)})
    library = FakeLibrary(available={555})

    # First request: movie is in Plex -> recorded directly as available (the winner).
    async with sessionmaker_() as session:
        first = await request_service.create_request(
            session, tmdb, tmdb_id=555, media_type="movie", library=library
        )

    # Second (racing) request: force find_in_library to MISS, exactly as it would when
    # the winner's row is not yet visible to the racing transaction. This drives the
    # duplicate insert the active-dedup index cannot catch.
    async def racing_find_in_library(
        self: SqlRequestRepository,
        tmdb_id: int,
        media_type: str,
        *,
        prefer_user_id: int | None = None,
    ) -> RequestRecord | None:
        return None

    monkeypatch.setattr(SqlRequestRepository, "find_in_library", racing_find_in_library)

    async with sessionmaker_() as session:
        second = await request_service.create_request(
            session, tmdb, tmdb_id=555, media_type="movie", library=library
        )

    assert second.id == first.id  # the race loser collapsed onto the winner

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 555)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # the duplicate available row was deleted
    assert rows[0].status is RequestStatus.available


async def test_in_library_short_circuit_locks_before_terminal_dedup_lookup(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def acquire_media_lock(self: SqlRequestRepository, tmdb_id: int, media_type: str) -> None:
        assert tmdb_id == 556
        assert media_type == "movie"
        calls.append("lock")

    async def find_in_library(
        self: SqlRequestRepository,
        tmdb_id: int,
        media_type: str,
        *,
        prefer_user_id: int | None = None,
    ) -> RequestRecord | None:
        assert calls == ["lock"]
        calls.append("find")
        return None

    monkeypatch.setattr(SqlRequestRepository, "acquire_media_lock", acquire_media_lock)
    monkeypatch.setattr(SqlRequestRepository, "find_in_library", find_in_library)

    tmdb = FakeTmdb(movies={556: MovieMetadata(tmdb_id=556, title="Nope", year=2022)})
    library = FakeLibrary(available={556})
    async with sessionmaker_() as session:
        record = await request_service.create_request(
            session, tmdb, tmdb_id=556, media_type="movie", library=library
        )

    assert calls == ["lock", "find"]
    assert record.status == RequestStatus.available.value


async def test_removed_then_reacquired_yields_a_second_available_row(
    sessionmaker_: SessionMaker,
) -> None:
    """The legitimate re-acquire is preserved: a movie removed from Plex keeps its
    stale ``available`` row; re-requested while NOT in Plex it takes the normal
    pending -> mark_available path, producing a SECOND available row. The F9
    race-collapse must NOT delete it (that path never enters the short-circuit)."""
    tmdb = FakeTmdb(movies={42: MovieMetadata(tmdb_id=42, title="Heat", year=1995)})
    library = FakeLibrary(available={42})

    # 1. In Plex -> recorded as available (this becomes the stale row after removal).
    async with sessionmaker_() as session:
        stale = await request_service.create_request(
            session, tmdb, tmdb_id=42, media_type="movie", library=library
        )

    # 2. Removed from Plex.
    library.available_ids.discard(42)

    # 3. Re-requested while NOT in Plex -> a NEW pending request (no short-circuit, so
    #    the reconcile branch is never reached).
    async with sessionmaker_() as session:
        reacquired = await request_service.create_request(
            session, tmdb, tmdb_id=42, media_type="movie", library=library
        )
    assert reacquired.id != stale.id
    async with sessionmaker_() as session:
        pending_row = await session.get(MediaRequest, reacquired.id)
        assert pending_row is not None
        assert pending_row.status is RequestStatus.pending

    # 4. It downloads and Plex confirms it -> mark_available (the SECOND available row).
    async with sessionmaker_() as session:
        await request_service.mark_available(session, reacquired.id)

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 42)))
            .scalars()
            .all()
        )
    available_ids = sorted(r.id for r in rows if r.status is RequestStatus.available)
    assert available_ids == sorted([stale.id, reacquired.id])  # BOTH survive


async def test_create_request_redetects_removal_within_cache_ttl(
    sessionmaker_: SessionMaker,
) -> None:
    """G7: the request-dedup path must not trust a cached PRESENT answer from the real
    Plex adapter. A movie recorded as available fills the presence cache; after it is
    REMOVED from Plex and immediately re-requested (within the cache TTL), create_request
    must re-page Plex (use_cache=False), see it absent, and create a fresh pending
    request — not return the stale 'available' row."""
    reset_caches()
    present: set[int] = {99}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "Directory": [
                            {
                                "key": "1",
                                "title": "Movies",
                                "type": "movie",
                                "Location": [{"path": "/data/movies"}],
                            }
                        ]
                    }
                },
            )
        if path == "/library/sections/1/all":
            meta = [{"Guid": [{"id": f"tmdb://{i}"}]} for i in sorted(present)]
            return httpx.Response(200, json={"MediaContainer": {"Metadata": meta}})
        return httpx.Response(404, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    library = PlexLibrary(client, base_url="http://g7-plex:32400", token="tok")  # noqa: S106
    tmdb = FakeTmdb(movies={99: MovieMetadata(tmdb_id=99, title="Tenet", year=2020)})

    # 1. In Plex -> recorded as available; this pages Plex and fills the presence cache.
    async with sessionmaker_() as session:
        available = await request_service.create_request(
            session, tmdb, tmdb_id=99, media_type="movie", library=library
        )
    async with sessionmaker_() as session:
        avail_row = await session.get(MediaRequest, available.id)
        assert avail_row is not None
        assert avail_row.status is RequestStatus.available

    # 2. Removed from Plex — but the presence cache still holds tmdb 99 (within TTL).
    present.discard(99)

    # 3. Re-requested immediately. The dedup path must re-page, see it absent, and
    #    create a NEW pending request rather than returning the stale available row.
    async with sessionmaker_() as session:
        reacquired = await request_service.create_request(
            session, tmdb, tmdb_id=99, media_type="movie", library=library
        )
    assert reacquired.id != available.id
    async with sessionmaker_() as session:
        pending_row = await session.get(MediaRequest, reacquired.id)
        assert pending_row is not None
        assert pending_row.status is RequestStatus.pending


async def test_create_request_after_eviction_creates_a_fresh_request(
    sessionmaker_: SessionMaker,
) -> None:
    """ADR-0012: once the disk-pressure sweep flips a request to ``evicted``, the
    old (now off-disk) row must never shadow a re-request as if it were still
    active. This exercises the SERVICE-level path (``create_request`` ->
    ``find_active``), not just the DB partial-index backstop: without
    ``evicted`` in ``repositories.requests._SETTLED_REQUEST_STATUSES``,
    ``find_active`` would keep returning the evicted row and ``create_request``
    would resolve to the stale row instead of creating a fresh one that
    actually re-grabs the content."""
    async with sessionmaker_() as session:
        evicted = MediaRequest(
            tmdb_id=601,
            media_type=MediaType.movie,
            title="Evicted Movie",
            status=RequestStatus.evicted,
        )
        session.add(evicted)
        await session.commit()
        evicted_id = evicted.id

    tmdb = FakeTmdb(movies={601: MovieMetadata(tmdb_id=601, title="Evicted Movie", year=2019)})
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(session, tmdb, tmdb_id=601, media_type="movie")

    assert fresh.id != evicted_id
    assert fresh.status == RequestStatus.pending.value

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 601)))
            .scalars()
            .all()
        )
    statuses = sorted(r.status.value for r in rows)
    assert statuses == ["evicted", "pending"]  # both rows survive, independently


async def test_create_request_after_eviction_re_grabs_when_plex_still_reports_present(
    sessionmaker_: SessionMaker,
) -> None:
    """P1 (ADR-0012 #67): the eviction sweep commits a movie 'evicted' BEFORE it
    unlinks the file and BEFORE the post-delete Plex refresh, so for that whole
    window Plex still reports the (doomed) file present. A re-request in that
    window must NOT trust the stale in-library reading and mint a fresh 'available'
    row -- the sweep is about to delete the file, leaving it marked available with
    nothing to download. It must re-grab ('pending') instead."""
    async with sessionmaker_() as session:
        evicted = MediaRequest(
            tmdb_id=602,
            media_type=MediaType.movie,
            title="Evicted Movie",
            status=RequestStatus.evicted,
        )
        session.add(evicted)
        await session.commit()
        evicted_id = evicted.id

    tmdb = FakeTmdb(movies={602: MovieMetadata(tmdb_id=602, title="Evicted Movie", year=2019)})
    # Plex STILL reports the file present -- exactly the eviction delete window.
    library = FakeLibrary(available={602})
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(
            session, tmdb, tmdb_id=602, media_type="movie", library=library
        )

    assert fresh.id != evicted_id
    # Re-grabbed, NOT minted 'available' over the file the sweep is deleting.
    assert fresh.status == RequestStatus.pending.value

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 602)))
            .scalars()
            .all()
        )
    statuses = sorted(r.status.value for r in rows)
    assert statuses == ["evicted", "pending"]  # never a second 'available' row


async def test_create_request_after_whole_show_eviction_re_grabs_when_plex_stale(
    sessionmaker_: SessionMaker,
) -> None:
    """The TV analogue of the movie P1: a wholly-evicted show (rollup 'evicted', so
    ``find_active`` misses it) re-requested while Plex still lists its seasons must
    create a FRESH request whose seasons re-grab ('pending'), never seasons minted
    'available' over files the sweep is deleting -- ``ensure_seasons`` subtracts the
    just-evicted seasons from the trusted Plex-present set."""
    async with sessionmaker_() as session:
        show = MediaRequest(
            tmdb_id=615,
            media_type=MediaType.tv,
            title="Evicted Show",
            status=RequestStatus.evicted,
        )
        session.add(show)
        await session.flush()
        session.add_all(
            [
                SeasonRequest(
                    media_request_id=show.id, season_number=n, status=RequestStatus.evicted
                )
                for n in (1, 2)
            ]
        )
        await session.commit()
        old_id = show.id

    tmdb = FakeTmdb(
        shows={615: TvMetadata(tmdb_id=615, title="Evicted Show", year=2020, season_count=2)}
    )
    # Plex STILL lists both seasons -- the eviction delete window.
    library = FakeLibrary(available_tv_seasons={615: frozenset({1, 2})})
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(
            session, tmdb, tmdb_id=615, media_type="tv", seasons=[1, 2], library=library
        )

    assert fresh.id != old_id
    # The fresh show's seasons re-grab, so its rollup is 'pending' -- never a
    # dishonest 'available'/'partially_available' over the doomed files.
    assert fresh.status == RequestStatus.pending.value
    async with sessionmaker_() as session:
        rows = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == fresh.id)
                )
            )
            .scalars()
            .all()
        )
    assert {r.season_number: r.status.value for r in rows} == {1: "pending", 2: "pending"}


async def test_create_request_never_returns_a_stale_leftover_available_row_in_the_window(
    sessionmaker_: SessionMaker,
) -> None:
    """Round-3 finding 4 (guard ordering): a media can carry an OLDER stale
    'available' row alongside the just-evicted one (the removed-then-reacquired
    pattern keeps BOTH available rows; the sweep claims only the one it evicts).
    find_in_library would return that leftover BEFORE the evicted-guard ran,
    handing back an in-library answer for content the sweep is deleting. The
    guard must gate every path that answers 'available' off Plex presence: the
    re-request must land as a fresh 'pending' re-grab, not the stale row."""
    async with sessionmaker_() as session:
        stale = MediaRequest(
            tmdb_id=660,
            media_type=MediaType.movie,
            title="Leftover",
            status=RequestStatus.available,  # the old remove-then-reacquire leftover
        )
        session.add(stale)
        await session.flush()  # stale gets the lower id
        evicted = MediaRequest(
            tmdb_id=660,
            media_type=MediaType.movie,
            title="Leftover",
            status=RequestStatus.evicted,  # the just-claimed eviction (newest row)
        )
        session.add(evicted)
        await session.commit()
        stale_id = stale.id

    tmdb = FakeTmdb(movies={660: MovieMetadata(tmdb_id=660, title="Leftover", year=2018)})
    library = FakeLibrary(available={660})  # Plex STILL lists the doomed file
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(
            session, tmdb, tmdb_id=660, media_type="movie", library=library
        )

    assert fresh.id != stale_id  # the stale leftover row is NOT handed back
    assert fresh.status == RequestStatus.pending.value  # a real re-grab
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 660)))
            .scalars()
            .all()
        )
    assert sorted(r.status.value for r in rows) == ["available", "evicted", "pending"]


async def test_create_request_tv_skips_in_library_dedup_for_just_evicted_seasons(
    sessionmaker_: SessionMaker,
) -> None:
    """The TV analogue of the guard-ordering fix: an older stale 'available' show
    row exists while the newest row's season was just claimed 'evicted' (the
    delete window). The in-library dedup's Plex-present superset check must
    subtract just-evicted seasons FIRST -- otherwise it dedups onto the stale row
    and answers in-library for a season the sweep is deleting. The re-request
    must instead create a fresh tracked request whose season re-grabs."""
    async with sessionmaker_() as session:
        stale = MediaRequest(
            tmdb_id=661,
            media_type=MediaType.tv,
            title="Leftover Show",
            status=RequestStatus.available,  # the stale leftover
        )
        session.add(stale)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=stale.id, season_number=1, status=RequestStatus.available
            )
        )
        evicted = MediaRequest(
            tmdb_id=661,
            media_type=MediaType.tv,
            title="Leftover Show",
            status=RequestStatus.evicted,  # the just-claimed eviction (newest)
        )
        session.add(evicted)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=evicted.id, season_number=1, status=RequestStatus.evicted
            )
        )
        await session.commit()
        stale_id = stale.id

    tmdb = FakeTmdb(
        shows={661: TvMetadata(tmdb_id=661, title="Leftover Show", year=2019, season_count=1)}
    )
    library = FakeLibrary(available_tv_seasons={661: frozenset({1})})  # Plex still lists it
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(
            session, tmdb, tmdb_id=661, media_type="tv", seasons=[1], library=library
        )

    assert fresh.id != stale_id  # never deduped onto the stale leftover
    assert fresh.status == RequestStatus.pending.value
    async with sessionmaker_() as session:
        rows = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == fresh.id)
                )
            )
            .scalars()
            .all()
        )
    assert {r.season_number: r.status.value for r in rows} == {1: "pending"}  # re-grabs


async def test_create_request_tv_dedups_onto_concurrent_regrab_under_the_lock(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round-4 finding 3: the TV twin of the movie under-lock re-read. A second
    TV re-request commits an active 'pending' re-grab between this caller's
    top-of-function find_active and the in-library dedup; evicted_seasons (newest
    non-cancelled row per season) then sees that PENDING row, subtracts nothing,
    and -- without the under-lock re-read -- the superset check dedups onto an
    OLDER stale 'available' row for the season the sweep is deleting. The racing
    request must dedup onto the pending re-grab instead."""
    async with sessionmaker_() as session:
        stale = MediaRequest(
            tmdb_id=670,
            media_type=MediaType.tv,
            title="Raced Show",
            status=RequestStatus.available,  # the stale leftover
        )
        session.add(stale)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=stale.id, season_number=1, status=RequestStatus.available
            )
        )
        evicted = MediaRequest(
            tmdb_id=670,
            media_type=MediaType.tv,
            title="Raced Show",
            status=RequestStatus.evicted,  # the claim window
        )
        session.add(evicted)
        await session.flush()
        session.add(
            SeasonRequest(
                media_request_id=evicted.id, season_number=1, status=RequestStatus.evicted
            )
        )
        # The CONCURRENT re-request's already-committed pending re-grab (newest).
        regrab = MediaRequest(
            tmdb_id=670,
            media_type=MediaType.tv,
            title="Raced Show",
            status=RequestStatus.pending,
        )
        session.add(regrab)
        await session.flush()
        session.add(
            SeasonRequest(media_request_id=regrab.id, season_number=1, status=RequestStatus.pending)
        )
        await session.commit()
        stale_id, regrab_id = stale.id, regrab.id

    real_find_active = SqlRequestRepository.find_active
    calls = {"n": 0}

    async def racing_find_active(
        self: SqlRequestRepository, tmdb_id: int, media_type: str
    ) -> RequestRecord | None:
        # The top-of-function check misses the concurrent re-grab (not yet
        # visible to this transaction); the UNDER-LOCK re-read sees it.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_find_active(self, tmdb_id, media_type)

    monkeypatch.setattr(SqlRequestRepository, "find_active", racing_find_active)

    tmdb = FakeTmdb(
        shows={670: TvMetadata(tmdb_id=670, title="Raced Show", year=2020, season_count=1)}
    )
    library = FakeLibrary(available_tv_seasons={670: frozenset({1})})  # Plex still lists it
    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session, tmdb, tmdb_id=670, media_type="tv", seasons=[1], library=library
        )

    assert result.id == regrab_id  # deduped onto the concurrent re-grab...
    assert result.id != stale_id  # ...never the stale leftover 'available' row
    assert result.status == RequestStatus.pending.value
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 670)))
            .scalars()
            .all()
        )
    # No fourth row minted -- and no 'available' answer for the doomed season.
    assert sorted(r.status.value for r in rows) == ["available", "evicted", "pending"]


async def test_create_request_after_cancelled_in_window_regrab_still_re_grabs(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex round-2 finding 2: evicted -> in-window re-grab ('pending', per the
    guard) -> user CANCELS the re-grab -> newest row is now 'cancelled'. The next
    re-request in the still-open delete window must STILL re-grab ('pending') --
    the guard keys on the newest NON-cancelled row (still the eviction), because
    a cancellation says nothing about on-disk truth. Before the fix, the
    cancelled row reset the guard and this request minted 'available' over the
    file the sweep was deleting."""
    async with sessionmaker_() as session:
        evicted = MediaRequest(
            tmdb_id=640,
            media_type=MediaType.movie,
            title="Doomed Twice",
            status=RequestStatus.evicted,
        )
        cancelled = MediaRequest(
            tmdb_id=640,
            media_type=MediaType.movie,
            title="Doomed Twice",
            status=RequestStatus.cancelled,
        )
        session.add(evicted)
        await session.flush()  # evicted gets the lower id
        session.add(cancelled)
        await session.commit()

    tmdb = FakeTmdb(movies={640: MovieMetadata(tmdb_id=640, title="Doomed Twice", year=2021)})
    library = FakeLibrary(available={640})  # Plex STILL lists the doomed file
    async with sessionmaker_() as session:
        fresh = await request_service.create_request(
            session, tmdb, tmdb_id=640, media_type="movie", library=library
        )

    assert fresh.status == RequestStatus.pending.value  # re-grabbed, never 'available'
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 640)))
            .scalars()
            .all()
        )
    assert sorted(r.status.value for r in rows) == ["cancelled", "evicted", "pending"]


async def test_create_request_dedups_onto_concurrent_regrab_in_eviction_window(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adversarial concurrency for the movie eviction re-request guard: two
    re-requests race in the delete window. The first mints a 'pending' re-grab; the
    second's top-level find_active (before it wins the media lock) misses it, so it
    reaches the in-library branch. It must dedup onto the concurrent re-grab via the
    UNDER-LOCK find_active re-read -- NOT mint 'available' over the doomed file just
    because the newest row is now that 'pending' one (which find_in_library, which
    matches only available/completed, would not catch)."""
    async with sessionmaker_() as session:
        evicted = MediaRequest(
            tmdb_id=630,
            media_type=MediaType.movie,
            title="Doomed",
            status=RequestStatus.evicted,
        )
        session.add(evicted)
        await session.flush()
        # A concurrent re-request's already-committed 'pending' re-grab (newest row).
        regrab = MediaRequest(
            tmdb_id=630,
            media_type=MediaType.movie,
            title="Doomed",
            status=RequestStatus.pending,
        )
        session.add(regrab)
        await session.commit()
        regrab_id = regrab.id

    real_find_active = SqlRequestRepository.find_active
    calls = {"n": 0}

    async def racing_find_active(
        self: SqlRequestRepository, tmdb_id: int, media_type: str
    ) -> RequestRecord | None:
        # The top-level check (call 1) misses the concurrent 'pending' row, as it
        # would when that row is not yet visible to this transaction; the under-lock
        # re-read (call 2) sees the committed re-grab and dedups onto it.
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_find_active(self, tmdb_id, media_type)

    monkeypatch.setattr(SqlRequestRepository, "find_active", racing_find_active)

    tmdb = FakeTmdb(movies={630: MovieMetadata(tmdb_id=630, title="Doomed", year=2020)})
    library = FakeLibrary(available={630})  # Plex STILL lists the doomed file
    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session, tmdb, tmdb_id=630, media_type="movie", library=library
        )

    assert result.id == regrab_id  # deduped onto the concurrent re-grab
    assert result.status == RequestStatus.pending.value
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 630)))
            .scalars()
            .all()
        )
    # No third row -- and crucially no 'available' row minted over the doomed file.
    assert sorted(r.status.value for r in rows) == ["evicted", "pending"]


async def _season_numbers(sm: SessionMaker, media_request_id: int) -> set[int]:
    async with sm() as session:
        rows = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == media_request_id)
                )
            )
            .scalars()
            .all()
        )
    return {row.season_number for row in rows}


async def test_create_request_tv_ensures_every_aired_season_when_seasons_omitted(
    sessionmaker_: SessionMaker,
) -> None:
    tmdb = FakeTmdb(
        shows={
            5001: TvMetadata(tmdb_id=5001, title="Some Show", year=2020, season_count=3),
        }
    )
    async with sessionmaker_() as session:
        record = await request_service.create_request(session, tmdb, tmdb_id=5001, media_type="tv")

    assert await _season_numbers(sessionmaker_, record.id) == {1, 2, 3}
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, record.id)
        assert row is not None
        assert row.status is RequestStatus.pending  # nothing in Plex, no library check


async def test_create_request_tv_seasons_param_creates_only_named_seasons(
    sessionmaker_: SessionMaker,
) -> None:
    tmdb = FakeTmdb(
        shows={5002: TvMetadata(tmdb_id=5002, title="Some Show", year=2020, season_count=10)}
    )
    async with sessionmaker_() as session:
        record = await request_service.create_request(
            session, tmdb, tmdb_id=5002, media_type="tv", seasons=[2]
        )

    # Only the NAMED season is tracked, even though the show has 10 aired seasons.
    assert await _season_numbers(sessionmaker_, record.id) == {2}


async def test_tv_season_presence_check_failure_logs_tmdb_id_via_extra(
    sessionmaker_: SessionMaker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The TV analogue of the movie availability-check-failure test: a Plex outage
    during the per-season presence crawl logs ``tmdb_id`` via ``extra=``, never
    interpolated into the message text."""
    tmdb = FakeTmdb(
        shows={5099: TvMetadata(tmdb_id=5099, title="Some Show", year=2020, season_count=3)}
    )
    library = FakeLibrary(raises=PlexLibraryError("plex is down"))

    with caplog.at_level(logging.WARNING, logger="plex_manager.services.request_service"):
        async with sessionmaker_() as session:
            record = await request_service.create_request(
                session, tmdb, tmdb_id=5099, media_type="tv", seasons=[1], library=library
            )

    assert record.status == RequestStatus.pending.value  # never blocked by the outage
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a warning to be logged"
    assert "5099" not in warnings[0].getMessage()
    assert getattr(warnings[0], "tmdb_id", None) == 5099


async def test_create_request_tv_second_post_with_new_seasons_grows_the_tracked_set(
    sessionmaker_: SessionMaker,
) -> None:
    tmdb = FakeTmdb(
        shows={5003: TvMetadata(tmdb_id=5003, title="Some Show", year=2020, season_count=10)}
    )
    async with sessionmaker_() as session:
        first = await request_service.create_request(
            session, tmdb, tmdb_id=5003, media_type="tv", seasons=[1]
        )
    assert await _season_numbers(sessionmaker_, first.id) == {1}

    # A second POST for the SAME show, naming a DIFFERENT season: the dedup path
    # returns the SAME request, but the tracked season set must GROW, not be
    # silently dropped by the request-level dedup.
    async with sessionmaker_() as session:
        second = await request_service.create_request(
            session, tmdb, tmdb_id=5003, media_type="tv", seasons=[2]
        )
    assert second.id == first.id
    assert await _season_numbers(sessionmaker_, first.id) == {1, 2}


async def test_create_request_tv_all_seasons_in_library_dedups_to_existing(
    sessionmaker_: SessionMaker,
) -> None:
    """A repeat request for a TV show whose requested seasons are ALL already in
    Plex returns the existing in-library record instead of inserting a duplicate
    terminal 'available' MediaRequest -- the active-dedup index excludes 'available',
    and the movie in-library collapse is movie-only, so nothing else would catch it."""
    tmdb = FakeTmdb(
        shows={5010: TvMetadata(tmdb_id=5010, title="Done Show", year=2020, season_count=3)}
    )
    library = FakeLibrary(available_tv_seasons={5010: frozenset({1, 2})})
    async with sessionmaker_() as session:
        first = await request_service.create_request(
            session, tmdb, tmdb_id=5010, media_type="tv", seasons=[1, 2], library=library
        )
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, first.id)
        assert row is not None and row.status is RequestStatus.available

    async with sessionmaker_() as session:
        second = await request_service.create_request(
            session, tmdb, tmdb_id=5010, media_type="tv", seasons=[1, 2], library=library
        )
    assert second.id == first.id  # deduped to the existing available record
    assert second.status == RequestStatus.available.value  # the DTO status is a plain string

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 5010)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # no duplicate available MediaRequest


async def test_create_request_tv_collapses_racing_in_library_available_rows(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TV analogue of the movie F9 collapse: two racing creates whose seasons are
    ALL already in Plex each insert a 'pending' row that ensure_seasons rolls to
    terminal 'available' (outside the active-dedup index). The post-commit collapse
    resolves the loser onto the winner (one available row survives) AND merges the
    loser's requested seasons into the winner, so the DIFFERENT season the second
    caller asked for is still tracked (not lost with the deleted loser row)."""
    tmdb = FakeTmdb(shows={557: TvMetadata(tmdb_id=557, title="Done", year=2020, season_count=3)})
    library = FakeLibrary(available_tv_seasons={557: frozenset({1, 2})})
    async with sessionmaker_() as session:
        first = await request_service.create_request(
            session, tmdb, tmdb_id=557, media_type="tv", seasons=[1], library=library
        )

    # Force the in-library dedup + the racing transaction to MISS the winner row, so
    # the second create inserts a duplicate the active-dedup index cannot catch. The
    # racer requests a DIFFERENT season (2) than the winner tracked (1).
    async def racing_find_in_library(
        self: SqlRequestRepository,
        tmdb_id: int,
        media_type: str,
        *,
        prefer_user_id: int | None = None,
    ) -> RequestRecord | None:
        return None

    monkeypatch.setattr(SqlRequestRepository, "find_in_library", racing_find_in_library)
    async with sessionmaker_() as session:
        second = await request_service.create_request(
            session, tmdb, tmdb_id=557, media_type="tv", seasons=[2], library=library
        )

    assert second.id == first.id  # the race loser collapsed onto the winner
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 557)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # exactly one available row survives
    assert rows[0].status is RequestStatus.available
    # The winner tracks BOTH seasons: its own (1) AND the racer's merged season (2).
    assert await _season_numbers(sessionmaker_, first.id) == {1, 2}


async def test_create_request_tv_whole_series_with_zero_aired_seasons_raises(
    sessionmaker_: SessionMaker,
) -> None:
    """A whole-series tv request (no explicit ``seasons``) whose TMDB
    ``season_count`` resolves to 0 (a TMDB gap, or a specials-only show) must
    never persist a 'pending' request with ZERO tracked seasons -- nothing would
    ever drive search/grab for it, and it would show 'pending' forever (a silent
    dead request). Surfaced honestly as NoAiredSeasonsError instead."""
    tmdb = FakeTmdb(
        shows={5005: TvMetadata(tmdb_id=5005, title="Gap Show", year=2020, season_count=0)}
    )

    async with sessionmaker_() as session:
        with pytest.raises(request_service.NoAiredSeasonsError):
            await request_service.create_request(session, tmdb, tmdb_id=5005, media_type="tv")

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 5005)))
            .scalars()
            .all()
        )
    assert rows == []  # nothing was persisted -- no dead-end ghost request


async def test_create_request_tv_season_already_in_plex_rolls_up_partially_available(
    sessionmaker_: SessionMaker,
) -> None:
    tmdb = FakeTmdb(
        shows={5004: TvMetadata(tmdb_id=5004, title="Some Show", year=2020, season_count=2)}
    )
    library = FakeLibrary(available_tv_seasons={5004: frozenset({1})})

    async with sessionmaker_() as session:
        record = await request_service.create_request(
            session, tmdb, tmdb_id=5004, media_type="tv", library=library
        )

    async with sessionmaker_() as session:
        rows = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == record.id)
                )
            )
            .scalars()
            .all()
        )
        by_season = {row.season_number: row.status.value for row in rows}
        show = await session.get(MediaRequest, record.id)
    assert by_season == {1: "available", 2: "pending"}
    assert show is not None
    assert show.status is RequestStatus.partially_available


# --------------------------------------------------------------------------- #
# set_keep_forever (ADR-0012) — a per-TITLE pin, not a per-row one (R6-D)
# --------------------------------------------------------------------------- #


async def test_set_keep_forever_pins_every_row_sharing_the_title(
    sessionmaker_: SessionMaker,
) -> None:
    """R6-D regression: ``uq_media_requests_active`` only constrains ACTIVE
    rows, so a single show commonly has SEVERAL ``MediaRequest`` rows over its
    lifetime -- an older SETTLED ``available`` request (seasons 1-2) and a
    newer ACTIVE request (season 3). The UI resolves the title to the visible
    active row and toggles keep-forever THERE, but ``eviction_service.
    _season_candidates`` reads ``keep_forever`` off EACH season's OWN parent
    -- so pinning only the active row would leave the settled row's seasons
    unpinned and still evictable. Pinning must apply to every row sharing
    ``(tmdb_id, media_type)``."""
    async with sessionmaker_() as session:
        settled = MediaRequest(
            tmdb_id=9001,
            media_type=MediaType.tv,
            title="Old Show",
            status=RequestStatus.available,
        )
        active = MediaRequest(
            tmdb_id=9001,
            media_type=MediaType.tv,
            title="Old Show",
            status=RequestStatus.pending,
        )
        session.add_all([settled, active])
        await session.commit()
        settled_id, active_id = settled.id, active.id

    async with sessionmaker_() as session:
        updated = await request_service.set_keep_forever(
            session=session, request_id=active_id, keep_forever=True
        )
    assert updated is not None
    assert updated.id == active_id
    assert updated.keep_forever is True

    async with sessionmaker_() as session:
        settled_row = await session.get(MediaRequest, settled_id)
        active_row = await session.get(MediaRequest, active_id)
    assert settled_row is not None
    assert active_row is not None
    assert settled_row.keep_forever is True  # the OLD settled row is pinned too
    assert active_row.keep_forever is True


async def test_unset_keep_forever_clears_every_row_sharing_the_title(
    sessionmaker_: SessionMaker,
) -> None:
    """Symmetric to the pin above: unpinning the active row must clear the
    pin on every sibling row too, not leave a settled row permanently pinned."""
    async with sessionmaker_() as session:
        settled = MediaRequest(
            tmdb_id=9002,
            media_type=MediaType.tv,
            title="Another Show",
            status=RequestStatus.available,
            keep_forever=True,
        )
        active = MediaRequest(
            tmdb_id=9002,
            media_type=MediaType.tv,
            title="Another Show",
            status=RequestStatus.pending,
            keep_forever=True,
        )
        session.add_all([settled, active])
        await session.commit()
        settled_id, active_id = settled.id, active.id

    async with sessionmaker_() as session:
        updated = await request_service.set_keep_forever(
            session=session, request_id=active_id, keep_forever=False
        )
    assert updated is not None
    assert updated.keep_forever is False

    async with sessionmaker_() as session:
        settled_row = await session.get(MediaRequest, settled_id)
        active_row = await session.get(MediaRequest, active_id)
    assert settled_row is not None
    assert active_row is not None
    assert settled_row.keep_forever is False
    assert active_row.keep_forever is False


async def test_set_keep_forever_missing_request_returns_none(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        result = await request_service.set_keep_forever(
            session=session, request_id=999999, keep_forever=True
        )
    assert result is None


async def test_create_request_tv_mixed_fall_through_releases_the_media_lock(
    sessionmaker_: SessionMaker,
) -> None:
    """Round-7 finding 1: the mixed present/missing-season fall-through (the
    superset check fails) must release the media lock BEFORE proceeding to the
    create path, whose ensure_seasons performs its own Plex crawl -- the same
    discipline as the two dedup branches. Observable through the lock row
    itself: the release rolls back the RequestDedupLock insert, so the create's
    later commit persists NO lock row; without the release, the lock
    acquisition would ride along into that commit (after having held the write
    transaction across the crawl)."""
    tmdb = FakeTmdb(
        shows={690: TvMetadata(tmdb_id=690, title="Mixed Show", year=2021, season_count=2)}
    )
    # Season 1 present in Plex, season 2 missing -> present={1} acquires the
    # lock, superset([1, 2]) fails -> the fall-through creates a fresh request.
    library = FakeLibrary(available_tv_seasons={690: frozenset({1})})
    async with sessionmaker_() as session:
        record = await request_service.create_request(
            session, tmdb, tmdb_id=690, media_type="tv", seasons=[1, 2], library=library
        )

    # The fall-through create still works exactly as before...
    assert record.status == RequestStatus.partially_available.value
    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == record.id)
                )
            )
            .scalars()
            .all()
        )
        locks = (
            (await session.execute(select(RequestDedupLock).where(RequestDedupLock.tmdb_id == 690)))
            .scalars()
            .all()
        )
    assert {s.season_number: s.status.value for s in seasons} == {1: "available", 2: "pending"}
    # ...and the lock acquisition was rolled back before the create, so it never
    # rode into the final commit (the write transaction did not span the crawl).
    assert locks == []


# --------------------------------------------------------------------------- #
# Ownership on the dedup path (issue #58) — a non-admin must not dedup onto     #
# ANOTHER user's active request (would mutate/return a row they cannot see).    #
# --------------------------------------------------------------------------- #


async def _make_user(sm: SessionMaker, *, username: str, permissions: int = 0) -> int:
    async with sm() as session:
        user = User(username=username, permissions=permissions)
        session.add(user)
        await session.commit()
        return user.id


async def test_non_admin_dedup_onto_foreign_owned_tv_request_rejects_and_leaves_it_unmutated(
    sessionmaker_: SessionMaker,
) -> None:
    """A non-admin user POSTing a title already actively requested by a DIFFERENT
    user gets an honest 409 (``RequestOwnedByAnotherUserError``) — and the other
    user's request is NOT mutated: its tracked TV season set is unchanged, never
    grown with the intruder's requested season."""
    owner_id = await _make_user(sessionmaker_, username="owner")
    intruder_id = await _make_user(sessionmaker_, username="intruder")
    tmdb = FakeTmdb(shows={7001: TvMetadata(tmdb_id=7001, title="Show", year=2020, season_count=5)})

    async with sessionmaker_() as session:
        owned = await request_service.create_request(
            session, tmdb, tmdb_id=7001, media_type="tv", seasons=[1], user_id=owner_id
        )
    assert await _season_numbers(sessionmaker_, owned.id) == {1}

    async with sessionmaker_() as session:
        with pytest.raises(request_service.RequestOwnedByAnotherUserError):
            await request_service.create_request(
                session,
                tmdb,
                tmdb_id=7001,
                media_type="tv",
                seasons=[2],
                user_id=intruder_id,
                actor_is_admin=False,
            )

    assert await _season_numbers(sessionmaker_, owned.id) == {1}  # NOT grown to {1, 2}
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, owned.id)
        assert row is not None and row.user_id == owner_id  # still the owner's row


async def test_non_admin_dedup_onto_foreign_owned_movie_request_rejects(
    sessionmaker_: SessionMaker,
) -> None:
    """The movie analogue: a non-admin cannot dedup onto another user's active
    movie request (it would return a row the caller's list/get immediately hide)."""
    owner_id = await _make_user(sessionmaker_, username="owner-m")
    intruder_id = await _make_user(sessionmaker_, username="intruder-m")
    tmdb = FakeTmdb(movies={7005: MovieMetadata(tmdb_id=7005, title="Movie", year=2020)})

    async with sessionmaker_() as session:
        await request_service.create_request(
            session, tmdb, tmdb_id=7005, media_type="movie", user_id=owner_id
        )

    async with sessionmaker_() as session:
        with pytest.raises(request_service.RequestOwnedByAnotherUserError):
            await request_service.create_request(
                session,
                tmdb,
                tmdb_id=7005,
                media_type="movie",
                user_id=intruder_id,
                actor_is_admin=False,
            )


async def test_admin_dedup_onto_foreign_owned_tv_request_keeps_shared_behavior(
    sessionmaker_: SessionMaker,
) -> None:
    """Admins are exempt (they can see every request): an admin deduping onto
    another user's active TV request keeps the current shared behavior — the same
    row is returned and its tracked season set grows."""
    owner_id = await _make_user(sessionmaker_, username="owner-a")
    admin_id = await _make_user(sessionmaker_, username="admin", permissions=1)
    tmdb = FakeTmdb(shows={7002: TvMetadata(tmdb_id=7002, title="Show", year=2020, season_count=5)})

    async with sessionmaker_() as session:
        owned = await request_service.create_request(
            session, tmdb, tmdb_id=7002, media_type="tv", seasons=[1], user_id=owner_id
        )

    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7002,
            media_type="tv",
            seasons=[2],
            user_id=admin_id,
            actor_is_admin=True,
        )
    assert result.id == owned.id
    assert await _season_numbers(sessionmaker_, owned.id) == {1, 2}


async def test_key_auth_dedup_onto_owned_tv_request_keeps_shared_behavior(
    sessionmaker_: SessionMaker,
) -> None:
    """API-key automation carries no user identity (``user_id`` is None): it keeps
    the shared dedup behavior — deduping onto an owned request returns it and grows
    its season set, exactly as before this ownership guard."""
    owner_id = await _make_user(sessionmaker_, username="owner-k")
    tmdb = FakeTmdb(shows={7003: TvMetadata(tmdb_id=7003, title="Show", year=2020, season_count=5)})

    async with sessionmaker_() as session:
        owned = await request_service.create_request(
            session, tmdb, tmdb_id=7003, media_type="tv", seasons=[1], user_id=owner_id
        )

    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session, tmdb, tmdb_id=7003, media_type="tv", seasons=[2], user_id=None
        )
    assert result.id == owned.id
    assert await _season_numbers(sessionmaker_, owned.id) == {1, 2}


async def test_non_admin_claims_ownerless_request_on_dedup_unchanged(
    sessionmaker_: SessionMaker,
) -> None:
    """The ownerless-claim path is unaffected: a non-admin deduping onto an
    UNOWNED active request (e.g. one created by API-key automation) adopts it —
    no 409 — so the dedup result shows up in their own request list."""
    claimer_id = await _make_user(sessionmaker_, username="claimer")
    tmdb = FakeTmdb(movies={7004: MovieMetadata(tmdb_id=7004, title="Movie", year=2020)})

    async with sessionmaker_() as session:
        ownerless = await request_service.create_request(
            session, tmdb, tmdb_id=7004, media_type="movie", user_id=None
        )

    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7004,
            media_type="movie",
            user_id=claimer_id,
            actor_is_admin=False,
        )
    assert result.id == ownerless.id
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, ownerless.id)
        assert row is not None and row.user_id == claimer_id  # claimed, not rejected


async def test_owner_rededuping_onto_own_tv_request_grows_seasons(
    sessionmaker_: SessionMaker,
) -> None:
    """A user re-requesting their OWN active request is never rejected: the tracked
    season set grows, same as the pre-existing dedup behavior."""
    owner_id = await _make_user(sessionmaker_, username="self")
    tmdb = FakeTmdb(shows={7006: TvMetadata(tmdb_id=7006, title="Show", year=2020, season_count=5)})

    async with sessionmaker_() as session:
        owned = await request_service.create_request(
            session, tmdb, tmdb_id=7006, media_type="tv", seasons=[1], user_id=owner_id
        )
    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7006,
            media_type="tv",
            seasons=[2],
            user_id=owner_id,
            actor_is_admin=False,
        )
    assert result.id == owned.id
    assert await _season_numbers(sessionmaker_, owned.id) == {1, 2}


# --------------------------------------------------------------------------- #
# Ownership on the OTHER dedup paths (issue #58, wave 2) — the same decision    #
# guards the terminal find_in_library short-circuits, the IntegrityError race   #
# recovery, and the available-race collapse.                                    #
# --------------------------------------------------------------------------- #


async def test_non_admin_movie_in_library_dedup_onto_foreign_row_rejects(
    sessionmaker_: SessionMaker,
) -> None:
    """The TERMINAL-row bypass: the prior row is 'available' (outside find_active),
    so the movie in-library short-circuit resolves it via find_in_library — a
    non-admin must get the same honest 409 there, not another user's hidden row."""
    owner_id = await _make_user(sessionmaker_, username="lib-owner")
    intruder_id = await _make_user(sessionmaker_, username="lib-intruder")
    tmdb = FakeTmdb(movies={7101: MovieMetadata(tmdb_id=7101, title="Owned", year=2020)})
    library = FakeLibrary(available={7101})

    async with sessionmaker_() as session:
        owned = await request_service.create_request(
            session, tmdb, tmdb_id=7101, media_type="movie", user_id=owner_id, library=library
        )
    assert owned.status == RequestStatus.available.value

    async with sessionmaker_() as session:
        with pytest.raises(request_service.RequestOwnedByAnotherUserError):
            await request_service.create_request(
                session,
                tmdb,
                tmdb_id=7101,
                media_type="movie",
                user_id=intruder_id,
                actor_is_admin=False,
                library=library,
            )

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7101)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # nothing new persisted; the owner's row untouched
    assert rows[0].user_id == owner_id


async def test_admin_movie_in_library_dedup_onto_foreign_row_unchanged(
    sessionmaker_: SessionMaker,
) -> None:
    """Admin control for the terminal path: an admin deduping onto another user's
    in-library row keeps the shared behavior (the row is returned, never a 409)."""
    owner_id = await _make_user(sessionmaker_, username="lib-owner-a")
    admin_id = await _make_user(sessionmaker_, username="lib-admin", permissions=1)
    tmdb = FakeTmdb(movies={7102: MovieMetadata(tmdb_id=7102, title="Owned", year=2020)})
    library = FakeLibrary(available={7102})

    async with sessionmaker_() as session:
        owned = await request_service.create_request(
            session, tmdb, tmdb_id=7102, media_type="movie", user_id=owner_id, library=library
        )
    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7102,
            media_type="movie",
            user_id=admin_id,
            actor_is_admin=True,
            library=library,
        )
    assert result.id == owned.id


async def test_non_admin_tv_all_present_dedup_onto_foreign_row_rejects_unmutated(
    sessionmaker_: SessionMaker,
) -> None:
    """The TV terminal-row bypass: every requested season is already in Plex and the
    existing available request belongs to ANOTHER user — rejected BEFORE
    ensure_seasons, so the owner's tracked season set is never grown."""
    owner_id = await _make_user(sessionmaker_, username="tv-owner")
    intruder_id = await _make_user(sessionmaker_, username="tv-intruder")
    tmdb = FakeTmdb(
        shows={7103: TvMetadata(tmdb_id=7103, title="Done Show", year=2020, season_count=3)}
    )
    library = FakeLibrary(available_tv_seasons={7103: frozenset({1, 2})})

    async with sessionmaker_() as session:
        owned = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7103,
            media_type="tv",
            seasons=[1],
            user_id=owner_id,
            library=library,
        )
    assert await _season_numbers(sessionmaker_, owned.id) == {1}

    async with sessionmaker_() as session:
        with pytest.raises(request_service.RequestOwnedByAnotherUserError):
            await request_service.create_request(
                session,
                tmdb,
                tmdb_id=7103,
                media_type="tv",
                seasons=[2],
                user_id=intruder_id,
                actor_is_admin=False,
                library=library,
            )

    assert await _season_numbers(sessionmaker_, owned.id) == {1}  # NOT grown with season 2
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7103)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].user_id == owner_id


async def test_non_admin_integrity_race_recovery_onto_foreign_row_rejects_unmutated(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The IntegrityError-recovery bypass: a non-admin who LOSES the insert race to
    another user's active request gets the honest 409 — never the other user's
    winning row, and never adds their season to it."""
    owner_id = await _make_user(sessionmaker_, username="race-owner")
    intruder_id = await _make_user(sessionmaker_, username="race-intruder")
    tmdb = FakeTmdb(
        shows={7104: TvMetadata(tmdb_id=7104, title="Race Show", year=2020, season_count=5)}
    )

    async with sessionmaker_() as session:
        owned = await request_service.create_request(
            session, tmdb, tmdb_id=7104, media_type="tv", seasons=[1], user_id=owner_id
        )
    assert await _season_numbers(sessionmaker_, owned.id) == {1}

    # Simulate the race exactly like the movie recovery test above: the intruder's
    # first find_active misses (the owner's row invisible to its transaction), the
    # insert loses to the partial UNIQUE index, and the recovery lookup finds the
    # owner's committed winner.
    real_find_active = SqlRequestRepository.find_active
    calls = {"n": 0}

    async def racing_find_active(
        self: SqlRequestRepository, tmdb_id: int, media_type: str
    ) -> RequestRecord | None:
        if calls["n"] == 0:
            calls["n"] = 1
            return None
        return await real_find_active(self, tmdb_id, media_type)

    monkeypatch.setattr(SqlRequestRepository, "find_active", racing_find_active)

    async with sessionmaker_() as session:
        with pytest.raises(request_service.RequestOwnedByAnotherUserError):
            await request_service.create_request(
                session,
                tmdb,
                tmdb_id=7104,
                media_type="tv",
                seasons=[2],
                user_id=intruder_id,
                actor_is_admin=False,
            )

    assert await _season_numbers(sessionmaker_, owned.id) == {1}  # winner unmutated
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7104)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # the loser's insert was rolled back — no orphan row


async def test_non_admin_available_race_collapse_keeps_their_own_row(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The available-race collapse must NOT delete a non-admin's just-created row
    in favor of ANOTHER user's earlier available row: their request would vanish
    behind the per-user list filter. Both rows survive, each visible to its owner
    (mirrors the accepted remove-then-reacquire duplicate-available state)."""
    owner_id = await _make_user(sessionmaker_, username="col-owner")
    intruder_id = await _make_user(sessionmaker_, username="col-second")
    tmdb = FakeTmdb(movies={7105: MovieMetadata(tmdb_id=7105, title="Both Own", year=2020)})
    library = FakeLibrary(available={7105})

    async with sessionmaker_() as session:
        first = await request_service.create_request(
            session, tmdb, tmdb_id=7105, media_type="movie", user_id=owner_id, library=library
        )

    # Force find_in_library to MISS (the race window): the second, non-admin user
    # inserts their own available row instead of hitting the terminal-row guard.
    async def racing_find_in_library(
        self: SqlRequestRepository,
        tmdb_id: int,
        media_type: str,
        *,
        prefer_user_id: int | None = None,
    ) -> RequestRecord | None:
        return None

    monkeypatch.setattr(SqlRequestRepository, "find_in_library", racing_find_in_library)

    async with sessionmaker_() as session:
        second = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7105,
            media_type="movie",
            user_id=intruder_id,
            actor_is_admin=False,
            library=library,
        )

    assert second.id != first.id  # the collapse was SKIPPED — their own row returned
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7105)))
            .scalars()
            .all()
        )
    by_id = {row.id: row for row in rows}
    assert set(by_id) == {first.id, second.id}  # BOTH rows survive
    assert by_id[second.id].user_id == intruder_id
    assert all(row.status is RequestStatus.available for row in rows)


# --------------------------------------------------------------------------- #
# Ownerless-claim on the TERMINAL find_in_library short-circuits (issue #58):   #
# an X-Api-Key-automation ('user_id' None) in-library row must be ADOPTED for a #
# non-admin requester, exactly like the active find_active dedup — not merely   #
# returned, which would succeed yet vanish behind their per-user list filter.   #
# --------------------------------------------------------------------------- #
async def test_non_admin_claims_ownerless_movie_in_library_row_on_dedup(
    sessionmaker_: SessionMaker,
) -> None:
    """Terminal-row analogue of ``test_non_admin_claims_ownerless_request_on_dedup``:
    an OWNERLESS 'available' in-library movie row is adopted for the non-admin
    requester (no 409), so the dedup shows up in THEIR list instead of a success
    that instantly vanishes behind the per-user filter."""
    claimer_id = await _make_user(sessionmaker_, username="lib-claimer")
    tmdb = FakeTmdb(movies={7106: MovieMetadata(tmdb_id=7106, title="Owned", year=2020)})
    library = FakeLibrary(available={7106})

    # An automation (user_id None) records the movie as in-library first.
    async with sessionmaker_() as session:
        ownerless = await request_service.create_request(
            session, tmdb, tmdb_id=7106, media_type="movie", user_id=None, library=library
        )
    assert ownerless.status == RequestStatus.available.value
    async with sessionmaker_() as session:
        seeded = await session.get(MediaRequest, ownerless.id)
        assert seeded is not None and seeded.user_id is None  # ownerless to start

    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7106,
            media_type="movie",
            user_id=claimer_id,
            actor_is_admin=False,
            library=library,
        )
    assert result.id == ownerless.id  # deduped onto the same terminal row
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7106)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # no duplicate row minted
    assert rows[0].user_id == claimer_id  # claimed, not left ownerless-and-hidden


async def test_non_admin_claims_ownerless_tv_all_present_row_on_dedup(
    sessionmaker_: SessionMaker,
) -> None:
    """The TV terminal short-circuit adopts an OWNERLESS all-seasons-present row too:
    the non-admin requester claims it (and its seasons still grow), rather than
    receiving a row their own per-user list would hide (issue #58)."""
    claimer_id = await _make_user(sessionmaker_, username="tv-claimer")
    tmdb = FakeTmdb(
        shows={7107: TvMetadata(tmdb_id=7107, title="Done Show", year=2020, season_count=3)}
    )
    library = FakeLibrary(available_tv_seasons={7107: frozenset({1, 2})})

    async with sessionmaker_() as session:
        ownerless = await request_service.create_request(
            session, tmdb, tmdb_id=7107, media_type="tv", seasons=[1], user_id=None, library=library
        )
    async with sessionmaker_() as session:
        seeded = await session.get(MediaRequest, ownerless.id)
        assert seeded is not None and seeded.user_id is None  # ownerless to start

    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7107,
            media_type="tv",
            seasons=[2],
            user_id=claimer_id,
            actor_is_admin=False,
            library=library,
        )
    assert result.id == ownerless.id  # deduped onto the same terminal row
    assert await _season_numbers(sessionmaker_, ownerless.id) == {1, 2}  # season still grown
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7107)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].user_id == claimer_id  # claimed for the requester


# --------------------------------------------------------------------------- #
# In-library dedup winner PREFERENCE (issue #58): with SEVERAL terminal rows    #
# for one media, a non-admin's own row — then an ownerless claimable one —      #
# must win over a foreign row. Newest-global-row-wins alone made user B's       #
# newer row shadow user A's older visible row, turning A's re-request into a    #
# spurious requested_by_another_user 409.                                       #
# --------------------------------------------------------------------------- #


async def _seed_available_row(
    sm: SessionMaker,
    *,
    tmdb_id: int,
    user_id: int | None,
    media_type: str = "movie",
    title: str = "Seeded",
) -> int:
    """Insert a terminal ``available`` request row directly (a prior watchable)."""
    async with sm() as session:
        row = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType(media_type),
            title=title,
            status=RequestStatus.available,
            user_id=user_id,
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_non_admin_movie_in_library_dedup_prefers_their_own_older_row(
    sessionmaker_: SessionMaker,
) -> None:
    """User A owns an OLDER available row; user B's NEWER one exists for the same
    movie (a legitimate state — the per-user keep-own-row collapse produces it).
    A's re-request must return A's OWN row, not 409 because B's newer row would
    win a newest-global lookup."""
    a_id = await _make_user(sessionmaker_, username="pref-a")
    b_id = await _make_user(sessionmaker_, username="pref-b")
    older_a = await _seed_available_row(sessionmaker_, tmdb_id=7301, user_id=a_id)
    newer_b = await _seed_available_row(sessionmaker_, tmdb_id=7301, user_id=b_id)
    assert older_a < newer_b  # A's row is genuinely the shadowed older one

    tmdb = FakeTmdb(movies={7301: MovieMetadata(tmdb_id=7301, title="Seeded", year=2020)})
    library = FakeLibrary(available={7301})

    async with sessionmaker_() as session:
        record = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7301,
            media_type="movie",
            user_id=a_id,
            actor_is_admin=False,
            library=library,
        )

    assert record.id == older_a  # their own row, not a 409 and not B's row
    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7301)))
            .scalars()
            .all()
        )
    assert {row.id for row in rows} == {older_a, newer_b}  # nothing new, nothing deleted
    by_id = {row.id: row for row in rows}
    assert by_id[older_a].user_id == a_id
    assert by_id[newer_b].user_id == b_id  # B's row untouched


async def test_non_admin_movie_in_library_dedup_claims_ownerless_over_foreign(
    sessionmaker_: SessionMaker,
) -> None:
    """With a FOREIGN row and an OWNERLESS one both terminal for the same movie,
    the ownerless (claimable) row wins for a non-admin — adopted via the existing
    claim helper — instead of rejecting on the foreign row."""
    a_id = await _make_user(sessionmaker_, username="pref-claim-a")
    b_id = await _make_user(sessionmaker_, username="pref-claim-b")
    foreign_older = await _seed_available_row(sessionmaker_, tmdb_id=7302, user_id=b_id)
    ownerless_newer = await _seed_available_row(sessionmaker_, tmdb_id=7302, user_id=None)

    tmdb = FakeTmdb(movies={7302: MovieMetadata(tmdb_id=7302, title="Seeded", year=2020)})
    library = FakeLibrary(available={7302})

    async with sessionmaker_() as session:
        record = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7302,
            media_type="movie",
            user_id=a_id,
            actor_is_admin=False,
            library=library,
        )

    assert record.id == ownerless_newer
    async with sessionmaker_() as session:
        claimed = await session.get(MediaRequest, ownerless_newer)
        foreign = await session.get(MediaRequest, foreign_older)
    assert claimed is not None and claimed.user_id == a_id  # adopted for the requester
    assert foreign is not None and foreign.user_id == b_id  # the foreign row untouched


async def test_non_admin_movie_in_library_dedup_only_foreign_rows_still_409(
    sessionmaker_: SessionMaker,
) -> None:
    """When EVERY terminal row belongs to other users, the honest 409 is unchanged
    — the preference only reorders candidates, it never admits a foreign row."""
    a_id = await _make_user(sessionmaker_, username="pref-409-a")
    b_id = await _make_user(sessionmaker_, username="pref-409-b")
    c_id = await _make_user(sessionmaker_, username="pref-409-c")
    await _seed_available_row(sessionmaker_, tmdb_id=7303, user_id=b_id)
    await _seed_available_row(sessionmaker_, tmdb_id=7303, user_id=c_id)

    tmdb = FakeTmdb(movies={7303: MovieMetadata(tmdb_id=7303, title="Seeded", year=2020)})
    library = FakeLibrary(available={7303})

    async with sessionmaker_() as session:
        with pytest.raises(request_service.RequestOwnedByAnotherUserError):
            await request_service.create_request(
                session,
                tmdb,
                tmdb_id=7303,
                media_type="movie",
                user_id=a_id,
                actor_is_admin=False,
                library=library,
            )


async def test_non_admin_tv_all_present_dedup_prefers_their_own_older_row(
    sessionmaker_: SessionMaker,
) -> None:
    """The TV analogue: A's own older available show row wins over B's newer one,
    and ensure_seasons grows A's OWN row — never B's."""
    a_id = await _make_user(sessionmaker_, username="tv-pref-a")
    b_id = await _make_user(sessionmaker_, username="tv-pref-b")
    older_a = await _seed_available_row(
        sessionmaker_, tmdb_id=7304, user_id=a_id, media_type="tv", title="Seeded Show"
    )
    newer_b = await _seed_available_row(
        sessionmaker_, tmdb_id=7304, user_id=b_id, media_type="tv", title="Seeded Show"
    )

    tmdb = FakeTmdb(
        shows={7304: TvMetadata(tmdb_id=7304, title="Seeded Show", year=2020, season_count=3)}
    )
    library = FakeLibrary(available_tv_seasons={7304: frozenset({1, 2})})

    async with sessionmaker_() as session:
        record = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7304,
            media_type="tv",
            seasons=[1],
            user_id=a_id,
            actor_is_admin=False,
            library=library,
        )

    assert record.id == older_a  # their own row, not a 409 and not B's row
    assert await _season_numbers(sessionmaker_, older_a) == {1}  # A's row grew the season
    assert await _season_numbers(sessionmaker_, newer_b) == set()  # B's row untouched


# --------------------------------------------------------------------------- #
# Lost-claim race + ownerless recovery/collapse (issue #58, wave 3):           #
#  - the ownerless-adoption helper must NOT bless a LOST claim race as a silent #
#    success — the loser is routed through the same foreign-owner 409;          #
#  - the IntegrityError recovery AND the available-race collapse must ADOPT an  #
#    ownerless winner before returning/mutating it, never hand back a hidden    #
#    row the requester's own per-user list/get filter out.                      #
# --------------------------------------------------------------------------- #


async def test_lost_claim_race_loser_gets_foreign_owner_rejection(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-admin deduping onto an OWNERLESS active request whose adoption UPDATE
    LOSES a concurrent race (0 rows claimed — another user took it first) must get
    the SAME honest 409 as a row that was already foreign at read time, never a
    success on a row now owned by someone else that their own /requests list hides."""
    other_id = await _make_user(sessionmaker_, username="race-victor")
    claimer_id = await _make_user(sessionmaker_, username="race-loser")
    tmdb = FakeTmdb(movies={7108: MovieMetadata(tmdb_id=7108, title="Race", year=2020)})

    async with sessionmaker_() as session:
        ownerless = await request_service.create_request(
            session, tmdb, tmdb_id=7108, media_type="movie", user_id=None
        )
    async with sessionmaker_() as session:
        seeded = await session.get(MediaRequest, ownerless.id)
        assert seeded is not None and seeded.user_id is None  # ownerless to start

    async with sessionmaker_() as session:

        async def losing_claim(self: SqlRequestRepository, request_id: int, user_id: int) -> bool:
            # Simulate losing the adoption race: a concurrent writer takes ownership
            # between the ownerless read and our UPDATE (which then touches 0 rows).
            # Assign it in THIS session (create_request's own) so the helper's
            # get_fresh re-read observes the winning owner, then report the loss.
            row = await session.get(MediaRequest, request_id)
            assert row is not None
            row.user_id = other_id
            await session.flush()
            return False

        monkeypatch.setattr(SqlRequestRepository, "claim_if_unowned", losing_claim)
        with pytest.raises(request_service.RequestOwnedByAnotherUserError):
            await request_service.create_request(
                session,
                tmdb,
                tmdb_id=7108,
                media_type="movie",
                user_id=claimer_id,
                actor_is_admin=False,
            )

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7108)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # a dedup path — the loser never minted a duplicate row
    assert rows[0].user_id is None  # the lost-race UPDATE rolled back with the 409


async def test_integrity_race_recovery_claims_ownerless_movie_winner(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared user whose INSERT loses the active-unique race to an OWNERLESS
    automation row must ADOPT that recovery winner: without the claim, the
    ``winner.user_id is None`` guard passes and the ownerless row is returned
    behind the caller's own per-user filter — a success that vanishes (issue #58)."""
    claimer_id = await _make_user(sessionmaker_, username="race-claim-m")
    tmdb = FakeTmdb(movies={7109: MovieMetadata(tmdb_id=7109, title="Race", year=2020)})

    async with sessionmaker_() as session:
        ownerless = await request_service.create_request(
            session, tmdb, tmdb_id=7109, media_type="movie", user_id=None
        )

    real_find_active = SqlRequestRepository.find_active
    calls = {"n": 0}

    async def racing_find_active(
        self: SqlRequestRepository, tmdb_id: int, media_type: str
    ) -> RequestRecord | None:
        if calls["n"] == 0:
            calls["n"] = 1
            return None
        return await real_find_active(self, tmdb_id, media_type)

    monkeypatch.setattr(SqlRequestRepository, "find_active", racing_find_active)

    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7109,
            media_type="movie",
            user_id=claimer_id,
            actor_is_admin=False,
        )
    assert result.id == ownerless.id  # recovered onto the existing active winner

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7109)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # the loser's insert was rolled back — no orphan
    assert rows[0].user_id == claimer_id  # ownerless winner ADOPTED, not returned hidden


async def test_integrity_race_recovery_claims_ownerless_tv_winner_and_grows_seasons(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tv mutate case: the recovery winner is adopted BEFORE ensure_seasons, so
    the claimer's requested season grows an ownerless automation row they now own —
    never mutates + returns a row hidden behind their own per-user filter (issue #58)."""
    claimer_id = await _make_user(sessionmaker_, username="race-claim-tv")
    tmdb = FakeTmdb(
        shows={7110: TvMetadata(tmdb_id=7110, title="Race Show", year=2020, season_count=5)}
    )

    async with sessionmaker_() as session:
        ownerless = await request_service.create_request(
            session, tmdb, tmdb_id=7110, media_type="tv", seasons=[1], user_id=None
        )
    assert await _season_numbers(sessionmaker_, ownerless.id) == {1}

    real_find_active = SqlRequestRepository.find_active
    calls = {"n": 0}

    async def racing_find_active(
        self: SqlRequestRepository, tmdb_id: int, media_type: str
    ) -> RequestRecord | None:
        if calls["n"] == 0:
            calls["n"] = 1
            return None
        return await real_find_active(self, tmdb_id, media_type)

    monkeypatch.setattr(SqlRequestRepository, "find_active", racing_find_active)

    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7110,
            media_type="tv",
            seasons=[2],
            user_id=claimer_id,
            actor_is_admin=False,
        )
    assert result.id == ownerless.id
    assert await _season_numbers(sessionmaker_, ownerless.id) == {1, 2}  # grown on the winner

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7110)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].user_id == claimer_id  # ADOPTED before the season mutation


async def test_available_race_collapse_claims_ownerless_winner(
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The available-race collapse must ADOPT an ownerless EARLIER winner for this
    user before deleting their own duplicate row — else it deletes their row and
    returns one their per-user list/get hide. The user's row collapses onto the
    now-claimed winner: one available row survives, owned by the requester (#58)."""
    claimer_id = await _make_user(sessionmaker_, username="avail-claimer")
    tmdb = FakeTmdb(movies={7111: MovieMetadata(tmdb_id=7111, title="Both Avail", year=2020)})
    library = FakeLibrary(available={7111})

    # An OWNERLESS automation create records the movie in-library first (the winner).
    async with sessionmaker_() as session:
        ownerless = await request_service.create_request(
            session, tmdb, tmdb_id=7111, media_type="movie", user_id=None, library=library
        )
    assert ownerless.status == RequestStatus.available.value

    # Force find_in_library to MISS so the non-admin inserts their OWN available row;
    # the post-commit collapse must then adopt the ownerless winner, not just delete
    # the user's row and hand back a hidden one.
    async def racing_find_in_library(
        self: SqlRequestRepository,
        tmdb_id: int,
        media_type: str,
        *,
        prefer_user_id: int | None = None,
    ) -> RequestRecord | None:
        return None

    monkeypatch.setattr(SqlRequestRepository, "find_in_library", racing_find_in_library)

    async with sessionmaker_() as session:
        result = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=7111,
            media_type="movie",
            user_id=claimer_id,
            actor_is_admin=False,
            library=library,
        )
    assert result.id == ownerless.id  # collapsed onto the winner, now visible to them

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 7111)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # the user's duplicate was collapsed away
    assert rows[0].user_id == claimer_id  # winner ADOPTED, not left ownerless-and-hidden
    assert rows[0].status is RequestStatus.available


# --------------------------------------------------------------------------- #
# Re-acquire (issue #131): force=True bypasses the movie in-library short-       #
# circuit while every OTHER dedup/ownership guard runs unchanged.               #
# --------------------------------------------------------------------------- #


async def test_force_create_bypasses_in_library_short_circuit_creates_pending(
    sessionmaker_: SessionMaker,
) -> None:
    """The headline case: Plex still reports the movie present (its file was
    deleted out-of-band), but ``force=True`` skips the already-in-library
    short-circuit entirely -- a real 'pending' request is created, not a terminal
    'available' one. Contrast with the SAME inputs minus ``force`` (a different
    tmdb id, same library), which still takes the normal short-circuit."""
    tmdb = FakeTmdb(
        movies={
            999: MovieMetadata(tmdb_id=999, title="Ghost Movie", year=2019),
            998: MovieMetadata(tmdb_id=998, title="Normal Movie", year=2019),
        }
    )
    library = FakeLibrary(available={999, 998})

    async with sessionmaker_() as session:
        result = await request_service.create_request_result(
            session, tmdb, tmdb_id=999, media_type="movie", library=library, force=True
        )
    assert result.created is True
    assert result.record.status == RequestStatus.pending.value

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 999)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].status is RequestStatus.pending

    # Contrast: identical library presence, but no ``force`` -- the normal
    # already-in-library short-circuit still fires.
    async with sessionmaker_() as session:
        contrast = await request_service.create_request_result(
            session, tmdb, tmdb_id=998, media_type="movie", library=library, force=False
        )
    assert contrast.record.status == RequestStatus.available.value


async def test_force_create_alongside_stale_available_row_keeps_both(
    sessionmaker_: SessionMaker,
) -> None:
    """A force-create never re-arms or replaces a stale terminal 'available' row --
    it inserts a NEW 'pending' row alongside it (mirrors
    ``test_removed_then_reacquired_yields_a_second_available_row``). The old row
    survives untouched, and ``find_active`` now resolves to the new pending one."""
    tmdb = FakeTmdb(movies={997: MovieMetadata(tmdb_id=997, title="Phantom", year=2018)})
    library = FakeLibrary(available={997})

    async with sessionmaker_() as session:
        stale = await request_service.create_request(
            session, tmdb, tmdb_id=997, media_type="movie", library=library
        )
    assert stale.status == RequestStatus.available.value

    # Plex still hasn't rescanned -- library still reports it present -- but the
    # operator asserts it's gone and force-reacquires.
    async with sessionmaker_() as session:
        result = await request_service.create_request_result(
            session, tmdb, tmdb_id=997, media_type="movie", library=library, force=True
        )
    assert result.created is True
    assert result.record.id != stale.id
    assert result.record.status == RequestStatus.pending.value

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 997)))
            .scalars()
            .all()
        )
    statuses = sorted((r.id, r.status) for r in rows)
    assert statuses == sorted(
        [(stale.id, RequestStatus.available), (result.record.id, RequestStatus.pending)]
    )

    async with sessionmaker_() as session:
        active = await SqlRequestRepository(session).find_active(997, "movie")
    assert active is not None and active.id == result.record.id


async def test_force_create_rejects_foreign_owned_active_request(
    sessionmaker_: SessionMaker,
) -> None:
    """The ownerless-claim/ownership invariant (#58) holds on the force path too:
    ``force`` only skips the movie in-library short-circuit -- the UNCHANGED
    ``find_active`` dedup (and its ownership decision) still runs first, so a
    non-admin force-creating onto another user's active request still gets the
    honest 409, never the foreign row."""
    owner_id = await _make_user(sessionmaker_, username="force-owner")
    intruder_id = await _make_user(sessionmaker_, username="force-intruder")
    tmdb = FakeTmdb(movies={996: MovieMetadata(tmdb_id=996, title="Owned", year=2021)})
    library = FakeLibrary(available={996})

    async with sessionmaker_() as session:
        await request_service.create_request(
            session, tmdb, tmdb_id=996, media_type="movie", user_id=owner_id
        )

    async with sessionmaker_() as session:
        with pytest.raises(request_service.RequestOwnedByAnotherUserError):
            await request_service.create_request_result(
                session,
                tmdb,
                tmdb_id=996,
                media_type="movie",
                user_id=intruder_id,
                actor_is_admin=False,
                force=True,
                library=library,
            )


async def test_force_create_dedups_onto_own_active_request(
    sessionmaker_: SessionMaker,
) -> None:
    """Active-slot uniqueness holds on the force path: a second ``force=True`` call
    for the SAME user's already-active request returns the existing row
    (``created is False``), never a duplicate active row for the same media."""
    owner_id = await _make_user(sessionmaker_, username="force-self")
    tmdb = FakeTmdb(movies={995: MovieMetadata(tmdb_id=995, title="Mine", year=2021)})
    library = FakeLibrary(available={995})

    async with sessionmaker_() as session:
        first = await request_service.create_request(
            session, tmdb, tmdb_id=995, media_type="movie", user_id=owner_id
        )
    assert first.status == RequestStatus.pending.value

    async with sessionmaker_() as session:
        result = await request_service.create_request_result(
            session,
            tmdb,
            tmdb_id=995,
            media_type="movie",
            user_id=owner_id,
            actor_is_admin=False,
            force=True,
            library=library,
        )
    assert result.created is False
    assert result.record.id == first.id

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 995)))
            .scalars()
            .all()
        )
    assert len(rows) == 1  # no duplicate active row fabricated


async def test_force_create_claims_ownerless_active_request(
    sessionmaker_: SessionMaker,
) -> None:
    """The ownerless-claim invariant holds on the force path: a non-admin
    ``force=True`` create dedups onto (and ADOPTS) an existing OWNERLESS active
    request rather than 409-ing or fabricating a duplicate."""
    claimer_id = await _make_user(sessionmaker_, username="force-claimer")
    tmdb = FakeTmdb(movies={994: MovieMetadata(tmdb_id=994, title="Nobody's", year=2021)})
    library = FakeLibrary(available={994})

    async with sessionmaker_() as session:
        ownerless = await request_service.create_request(
            session, tmdb, tmdb_id=994, media_type="movie", user_id=None
        )
    assert ownerless.status == RequestStatus.pending.value

    async with sessionmaker_() as session:
        result = await request_service.create_request_result(
            session,
            tmdb,
            tmdb_id=994,
            media_type="movie",
            user_id=claimer_id,
            actor_is_admin=False,
            force=True,
            library=library,
        )
    assert result.created is False
    assert result.record.id == ownerless.id
    assert result.record.user_id == claimer_id  # claimed, not rejected

    async with sessionmaker_() as session:
        rows = (
            (await session.execute(select(MediaRequest).where(MediaRequest.tmdb_id == 994)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
