"""auto_grab_service — air-date wake pre-pass wiring into ``run_grab_cycle``
(issue #210).

Covers the SAME-cycle collection guarantee (a season TMDB now reports is woken
AND searched/grabbed within one cycle), the honest park when nothing is
acceptable after a wake, the ``metadata=None`` no-op, and that a wake-pass TMDB
error never aborts the rest of the cycle. Mirrors ``test_auto_grab_episode_
fallback.py``'s ``_run`` helper, extended with a ``library`` passthrough.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.adapters.tmdb import TmdbApiError
from plex_manager.domain.quality_profile import default_profile
from plex_manager.models import MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.library import LibraryPort
from plex_manager.ports.metadata import MetadataPort, TvMetadata
from plex_manager.services import auto_grab_service
from tests.web.fakes import (
    FakeLibrary,
    FakeProwlarr,
    FakeQbittorrent,
    FakeTmdb,
    candidate,
    prerelease_only_candidates,
)

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)


async def _seed_waiting_tv_season(
    sessionmaker_: SessionMaker, *, tmdb_id: int, season_number: int = 2, title: str = "Some Show"
) -> tuple[int, int]:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title=title,
            year=None,
            status=RequestStatus.waiting_for_air_date,
        )
        session.add(request)
        await session.flush()
        season = SeasonRequest(
            media_request_id=request.id,
            season_number=season_number,
            status=RequestStatus.waiting_for_air_date,
        )
        session.add(season)
        await session.commit()
        return request.id, season.id


async def _seed_pending_movie(sessionmaker_: SessionMaker, *, tmdb_id: int) -> int:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.pending,
        )
        session.add(request)
        await session.commit()
        return request.id


async def _run(
    sessionmaker_: SessionMaker,
    prowlarr: object,
    qbt: FakeQbittorrent,
    *,
    metadata: MetadataPort | None,
    library: LibraryPort | None = None,
    max_searches: int = auto_grab_service.AUTO_GRAB_MAX_SEARCHES_PER_CYCLE,
    now: datetime = _NOW,
    clock: Callable[[], datetime] | None = None,
) -> auto_grab_service.AutograbCycleResult:
    async with sessionmaker_() as session:
        return await auto_grab_service.run_grab_cycle(
            session,
            prowlarr=prowlarr,  # type: ignore[arg-type]  # a fake IndexerPort
            parser=GuessitParser(),
            profile=default_profile(),
            qbt=qbt,
            metadata=metadata,
            library=library,
            max_searches=max_searches,
            now=now,
            clock=clock or (lambda: now),
        )


async def test_run_grab_cycle_wakes_waiting_season_and_searches_it_same_cycle(
    sessionmaker_: SessionMaker,
) -> None:
    request_id, season_id = await _seed_waiting_tv_season(sessionmaker_, tmdb_id=2101)
    tmdb = FakeTmdb(shows={2101: TvMetadata(tmdb_id=2101, title="Some Show", season_count=2)})
    prowlarr = FakeProwlarr(
        [candidate("Some.Show.S02.1080p.WEB-DL.x264-GROUP", info_hash="a" * 40)]
    )
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=tmdb)

    assert result.air_date_woken == 1
    assert result.grabbed == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.downloading
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.downloading


async def test_run_grab_cycle_wake_parks_when_nothing_acceptable(
    sessionmaker_: SessionMaker,
) -> None:
    request_id, season_id = await _seed_waiting_tv_season(sessionmaker_, tmdb_id=2102)
    tmdb = FakeTmdb(shows={2102: TvMetadata(tmdb_id=2102, title="Some Show", season_count=2)})
    prowlarr = FakeProwlarr(prerelease_only_candidates())
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=tmdb)

    assert result.air_date_woken == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.no_acceptable_release
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status == RequestStatus.no_acceptable_release


async def test_run_grab_cycle_no_wake_when_metadata_none(sessionmaker_: SessionMaker) -> None:
    _request_id, season_id = await _seed_waiting_tv_season(sessionmaker_, tmdb_id=2103)
    prowlarr = FakeProwlarr([])
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=None)

    assert result.air_date_woken == 0
    assert prowlarr.searched == []
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.waiting_for_air_date


async def test_run_grab_cycle_wake_tmdb_error_does_not_abort_cycle(
    sessionmaker_: SessionMaker,
) -> None:
    _request_id, season_id = await _seed_waiting_tv_season(sessionmaker_, tmdb_id=2104)
    movie_id = await _seed_pending_movie(sessionmaker_, tmdb_id=2105)
    tmdb = FakeTmdb(get_tv_show_error=TmdbApiError("tmdb down"))
    prowlarr = FakeProwlarr(
        [candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", info_hash="b" * 40)]
    )
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=tmdb)

    assert result.air_date_woken == 0
    assert result.grabbed == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.waiting_for_air_date
        movie = await session.get(MediaRequest, movie_id)
        assert movie is not None
        assert movie.status == RequestStatus.downloading


async def test_run_grab_cycle_wake_uses_library_for_present_season(
    sessionmaker_: SessionMaker,
) -> None:
    """A Plex-present woken season transitions straight to ``available`` (never
    searched -- it grabs nothing this cycle, but the wake itself still counts)."""
    request_id, season_id = await _seed_waiting_tv_season(sessionmaker_, tmdb_id=2106)
    tmdb = FakeTmdb(shows={2106: TvMetadata(tmdb_id=2106, title="Some Show", season_count=2)})
    library = FakeLibrary(available_tv_seasons={2106: frozenset({2})})
    prowlarr = FakeProwlarr([])
    qbt = FakeQbittorrent()

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=tmdb, library=library)

    assert result.air_date_woken == 1
    assert result.grabbed == 0
    assert prowlarr.searched == []
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.available
        request = await session.get(MediaRequest, request_id)
        assert request is not None
        assert request.status != RequestStatus.waiting_for_air_date
