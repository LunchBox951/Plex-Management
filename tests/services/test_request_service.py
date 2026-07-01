"""request_service — active-request dedup recovers from the UNIQUE-index race.

A concurrent POST /requests for the same ``(tmdb_id, media_type)`` can both pass
the application-level ``find_active`` check, then both INSERT. The partial UNIQUE
index over active statuses rejects the loser; ``create_request`` catches the
``IntegrityError`` and resolves to the existing active request instead of crashing.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex import PlexLibrary
from plex_manager.adapters.plex.library import reset_caches
from plex_manager.models import MediaRequest, MediaType, RequestStatus, SeasonRequest
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
