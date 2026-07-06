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
        self: SqlRequestRepository, tmdb_id: int, media_type: str
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
        self: SqlRequestRepository, tmdb_id: int, media_type: str
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
        self: SqlRequestRepository, tmdb_id: int, media_type: str
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
