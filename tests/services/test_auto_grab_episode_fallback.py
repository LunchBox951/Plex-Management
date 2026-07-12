"""auto_grab_service — Pass-2 episode-level fallback wiring (ADR-0018, issue #178).

Covers the honest behaviours the fallback depends on: pack-first precedence (Pass 2
never runs while Pass 1 can still grab a pack), the TMDB target-unknown fall-through
(never a guessed target, never an aborted cycle), the no-redundant-grab exclusion of
already-imported episodes, one-active-per-season serialization, and the airing
pre-pass wiring into the SAME cycle's due-scope collection.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.parser.guessit_adapter import GuessitParser
from plex_manager.adapters.tmdb import TmdbApiError
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest
from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.metadata import EpisodeInfo
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.services import auto_grab_service
from tests.web.fakes import FakeQbittorrent, FakeTmdb, candidate

SessionMaker = async_sessionmaker[AsyncSession]

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


class _PerTmdbProwlarr:
    """An ``IndexerPort`` returning a DIFFERENT candidate set per tmdb id."""

    def __init__(self, by_tmdb: dict[int, list[CandidateRelease]]) -> None:
        self.by_tmdb = by_tmdb
        self.searched: list[IndexerSearchRequest] = []

    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        self.searched.append(request)
        return list(self.by_tmdb.get(request.tmdb_id or 0, []))


async def _seed_tv_season(
    sessionmaker_: SessionMaker,
    *,
    tmdb_id: int,
    season_number: int = 1,
    title: str = "Some Show",
    status: RequestStatus = RequestStatus.pending,
) -> tuple[int, int]:
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=tmdb_id, media_type=MediaType.tv, title=title, year=None, status=status
        )
        session.add(request)
        await session.flush()
        season = SeasonRequest(
            media_request_id=request.id, season_number=season_number, status=status
        )
        session.add(season)
        await session.commit()
        return request.id, season.id


async def _seed_imported_episode(
    sessionmaker_: SessionMaker, season_request_id: int, episode_number: int
) -> None:
    async with sessionmaker_() as session:
        download = Download(torrent_hash=f"imported-ep-{episode_number}", status="imported")
        session.add(download)
        await session.commit()
        repo = SqlSeasonEpisodeStateRepository(session)
        await repo.mark_imported(season_request_id, [episode_number], download_id=download.id)
        await session.commit()


async def _run(
    sessionmaker_: SessionMaker,
    prowlarr: object,
    qbt: FakeQbittorrent,
    *,
    metadata: FakeTmdb | None,
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
            metadata=metadata,  # type: ignore[arg-type]  # a fake MetadataPort
            max_searches=max_searches,
            now=now,
            clock=clock or (lambda: now),
        )


async def test_fallback_grabs_a_missing_episode_when_no_pack_is_acceptable(
    sessionmaker_: SessionMaker,
) -> None:
    request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=2001)
    qbt = FakeQbittorrent()
    # No season pack in the results -- Pass 1 rejects every candidate
    # NOT_SEASON_PACK, leaving Pass 2 to run.
    prowlarr = _PerTmdbProwlarr(
        {2001: [candidate("Some.Show.S01E01.1080p.WEB-DL.x264-GROUP", info_hash="a" * 40)]}
    )
    metadata = FakeTmdb(
        season_episodes={
            (2001, 1): [
                EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1)),
                EpisodeInfo(episode_number=2, air_date=date(2026, 1, 8)),
                EpisodeInfo(episode_number=3, air_date=date(2026, 1, 15)),
            ]
        }
    )

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=metadata)

    assert result.grabbed == 1
    assert result.season_episode_fallback_grabs == 1
    assert result.no_acceptable == 0
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.downloading
        parent = await session.get(MediaRequest, request_id)
        assert parent is not None
        assert parent.status == RequestStatus.downloading
        download = (
            (
                await session.execute(
                    Download.__table__.select().where(Download.media_request_id == request_id)
                )
            )
            .mappings()
            .one()
        )
        assert download["season"] == 1
        assert download["episodes_json"] == [1]


async def test_pack_first_wins_metadata_never_consulted(sessionmaker_: SessionMaker) -> None:
    request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=2002)
    qbt = FakeQbittorrent()
    prowlarr = _PerTmdbProwlarr(
        {2002: [candidate("Some.Show.S01.1080p.WEB-DL.x264-GROUP", info_hash="b" * 40)]}
    )
    metadata = FakeTmdb()

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=metadata)

    assert result.grabbed == 1
    assert result.season_episode_fallback_grabs == 0
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.downloading
        parent = await session.get(MediaRequest, request_id)
        assert parent is not None
        assert parent.status == RequestStatus.downloading
    # MetadataPort was never touched -- Pass 1 alone settled the scope.
    assert metadata.season_episodes_calls == []


async def test_tmdb_target_unknown_parks_honestly_and_other_scopes_still_process(
    sessionmaker_: SessionMaker,
) -> None:
    # Scope A: no pack available, TMDB raises when the fallback tries to refresh
    # the target -> must park honestly, never crash the cycle.
    request_a, season_a = await _seed_tv_season(sessionmaker_, tmdb_id=2003, title="Show A")
    # Scope B: an ordinary movie that grabs cleanly via Pass 1 -- proves the raise
    # in scope A's fallback did not abort the rest of the cycle.
    async with sessionmaker_() as session:
        movie = MediaRequest(
            tmdb_id=9001,
            media_type=MediaType.movie,
            title="Some Movie",
            year=2020,
            status=RequestStatus.pending,
        )
        session.add(movie)
        await session.commit()
        request_b = movie.id

    qbt = FakeQbittorrent()
    prowlarr = _PerTmdbProwlarr(
        {
            2003: [candidate("Show.A.S01E01.1080p.WEB-DL.x264-GROUP", info_hash="c" * 40)],
            9001: [candidate("Some.Movie.2020.1080p.WEB-DL.x264-GROUP", info_hash="d" * 40)],
        }
    )
    metadata = FakeTmdb(season_episodes_error=TmdbApiError("tmdb down"))

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=metadata)

    assert result.season_episode_fallback_grabs == 0
    assert result.no_acceptable == 1  # scope A parked
    assert result.grabbed == 1  # scope B still grabbed
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_a)
        assert season is not None
        assert season.status == RequestStatus.no_acceptable_release
        parent_a = await session.get(MediaRequest, request_a)
        assert parent_a is not None
        assert parent_a.status == RequestStatus.no_acceptable_release
        movie_row = await session.get(MediaRequest, request_b)
        assert movie_row is not None
        assert movie_row.status == RequestStatus.downloading


async def test_already_imported_episode_excluded_grabs_the_next_missing_one(
    sessionmaker_: SessionMaker,
) -> None:
    """The Last Man on Earth S4 shape: episode 7 already imported must never be
    redundantly re-grabbed; a release for the still-missing episode 8 is."""
    request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=2004, season_number=4)
    await _seed_imported_episode(sessionmaker_, season_id, 7)

    qbt = FakeQbittorrent()
    prowlarr = _PerTmdbProwlarr(
        {
            2004: [
                candidate("Some.Show.S04E07.1080p.WEB-DL.x264-GROUP", info_hash="e" * 40),
                candidate("Some.Show.S04E08.1080p.WEB-DL.x264-GROUP", info_hash="f" * 40),
            ]
        }
    )
    metadata = FakeTmdb(
        season_episodes={
            (2004, 4): [
                EpisodeInfo(episode_number=n, air_date=date(2026, 1, 1)) for n in range(1, 9)
            ]
        }
    )

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=metadata)

    assert result.grabbed == 1
    assert result.season_episode_fallback_grabs == 1
    async with sessionmaker_() as session:
        download = (
            (
                await session.execute(
                    Download.__table__.select().where(Download.media_request_id == request_id)
                )
            )
            .mappings()
            .one()
        )
        assert download["episodes_json"] == [8]


async def test_one_active_download_serializes_a_second_fallback_grab(
    sessionmaker_: SessionMaker,
) -> None:
    # The season's own status is a DUE one (``searching``) so ``_collect_due_scopes``
    # picks it up -- simulating a concurrent writer having just grabbed an active
    # download for it before this cycle's pre-search active check runs, exactly
    # the race the pre-search ``find_active_for_request`` guard exists to close.
    request_id, season_id = await _seed_tv_season(
        sessionmaker_, tmdb_id=2005, status=RequestStatus.searching
    )
    async with sessionmaker_() as session:
        active = Download(
            torrent_hash="already-active-hash",
            status="downloading",
            media_request_id=request_id,
            season=1,
            episodes_json=[1],
        )
        session.add(active)
        await session.commit()

    qbt = FakeQbittorrent()
    prowlarr = _PerTmdbProwlarr(
        {2005: [candidate("Some.Show.S01E02.1080p.WEB-DL.x264-GROUP", info_hash="12" * 20)]}
    )
    metadata = FakeTmdb(
        season_episodes={
            (2005, 1): [
                EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1)),
                EpisodeInfo(episode_number=2, air_date=date(2026, 1, 8)),
            ]
        }
    )

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=metadata)

    # The scope was skipped BEFORE it cost a search -- it already has an active
    # download for the season, so no fallback grab (or any grab) happens.
    assert result.searched == 0
    assert result.skipped_active == 1
    assert result.season_episode_fallback_grabs == 0
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        # Left untouched -- the scope was skipped, never parked or re-decided.
        assert season.status == RequestStatus.searching


async def test_airing_prepass_rearms_and_processes_within_the_same_cycle(
    sessionmaker_: SessionMaker,
) -> None:
    request_id, season_id = await _seed_tv_season(
        sessionmaker_, tmdb_id=2006, status=RequestStatus.available
    )
    await _seed_imported_episode(sessionmaker_, season_id, 1)
    await _seed_imported_episode(sessionmaker_, season_id, 2)

    qbt = FakeQbittorrent()
    prowlarr = _PerTmdbProwlarr({2006: []})  # nothing acceptable this cycle
    metadata = FakeTmdb(
        season_episodes={
            (2006, 1): [
                EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1)),
                EpisodeInfo(episode_number=2, air_date=date(2026, 1, 8)),
                EpisodeInfo(episode_number=3, air_date=date(2026, 1, 15)),  # newly aired
            ]
        }
    )

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=metadata)

    # The airing pre-pass re-armed the season to searching BEFORE due-scope
    # collection, so it was searched (and, finding nothing, parked) in THIS cycle
    # -- proving the pre-pass wiring, not just the underlying service function.
    assert result.searched == 1
    assert result.no_acceptable == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status != RequestStatus.available
        parent = await session.get(MediaRequest, request_id)
        assert parent is not None
        assert parent.status != RequestStatus.available


async def test_no_metadata_disables_fallback_cleanly(sessionmaker_: SessionMaker) -> None:
    """``metadata=None`` (unconfigured TMDB) must behave exactly like before this
    feature existed: Pass 1 only, honest park on nothing acceptable."""
    request_id, season_id = await _seed_tv_season(sessionmaker_, tmdb_id=2007)
    qbt = FakeQbittorrent()
    prowlarr = _PerTmdbProwlarr(
        {2007: [candidate("Some.Show.S01E01.1080p.WEB-DL.x264-GROUP", info_hash="99" * 20)]}
    )

    result = await _run(sessionmaker_, prowlarr, qbt, metadata=None)

    assert result.grabbed == 0
    assert result.season_episode_fallback_grabs == 0
    assert result.no_acceptable == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_id)
        assert season is not None
        assert season.status == RequestStatus.no_acceptable_release
        parent = await session.get(MediaRequest, request_id)
        assert parent is not None
        assert parent.status == RequestStatus.no_acceptable_release
