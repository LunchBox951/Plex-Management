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
from plex_manager.models import MediaRequest, MediaType, RequestStatus, SeasonRequest, User
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
        self: SqlRequestRepository, tmdb_id: int, media_type: str
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
