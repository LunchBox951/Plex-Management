"""``season_episode_service`` -- refresh_target / compute_missing / apply_import /
reconcile_airing (ADR-0020, issue #178).

Mirrors ``test_season_request_service.py``'s ``sessionmaker_`` fixture pattern.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.tmdb import TmdbApiError
from plex_manager.models import Download, MediaRequest, MediaType, RequestStatus, SeasonRequest
from plex_manager.ports.metadata import EpisodeInfo
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.season_episode_states import SqlSeasonEpisodeStateRepository
from plex_manager.services import season_episode_service
from tests.web.fakes import FakeTmdb

SessionMaker = async_sessionmaker[AsyncSession]

_TODAY = date(2026, 7, 11)


async def _make_show(sm: SessionMaker, tmdb_id: int = 800) -> int:
    async with sm() as session:
        show = MediaRequest(
            tmdb_id=tmdb_id, media_type=MediaType.tv, title="Show", status=RequestStatus.pending
        )
        session.add(show)
        await session.commit()
        return show.id


async def _make_season(
    sm: SessionMaker, media_request_id: int, season_number: int, status: RequestStatus
) -> int:
    async with sm() as session:
        row = SeasonRequest(
            media_request_id=media_request_id, season_number=season_number, status=status
        )
        session.add(row)
        await session.commit()
        return row.id


async def test_refresh_target_seeds_pending_rows_and_returns_aired_set(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=801)
    tmdb = FakeTmdb(
        season_episodes={
            (801, 4): [
                EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1)),
                EpisodeInfo(episode_number=2, air_date=date(2026, 12, 25)),  # future
                EpisodeInfo(episode_number=3, air_date=None),  # unaired
            ]
        }
    )

    async with sessionmaker_() as session:
        aired = await season_episode_service.refresh_target(
            session,
            tmdb,
            media_request_id=show_id,
            season_number=4,
            tmdb_id=801,
            today=_TODAY,
        )
        await session.commit()

    assert aired == frozenset({1})

    async with sessionmaker_() as session:
        season_row = (
            await session.execute(
                select(SeasonRequest).where(
                    SeasonRequest.media_request_id == show_id, SeasonRequest.season_number == 4
                )
            )
        ).scalar_one()
        repo = SqlSeasonEpisodeStateRepository(session)
        rows = await repo.list_for_season(season_row.id)
    assert {r.episode_number for r in rows} == {1}
    assert rows[0].status == "pending"


async def test_refresh_target_propagates_tmdb_error(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=802)
    tmdb = FakeTmdb(season_episodes_error=TmdbApiError("tmdb down"))

    async with sessionmaker_() as session:
        try:
            await season_episode_service.refresh_target(
                session,
                tmdb,
                media_request_id=show_id,
                season_number=1,
                tmdb_id=802,
                today=_TODAY,
            )
        except TmdbApiError:
            pass
        else:
            raise AssertionError("expected TmdbApiError to propagate")


async def test_compute_missing_excludes_imported_and_active_download_episodes(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=803)
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.downloading)

    async with sessionmaker_() as session:
        episode_repo = SqlSeasonEpisodeStateRepository(session)
        await episode_repo.upsert_target(
            season_request_id,
            {1: date(2026, 1, 1), 2: date(2026, 1, 8), 3: date(2026, 1, 15)},
        )
        download = Download(
            torrent_hash="active-download-hash",
            status="downloading",
            media_request_id=show_id,
            season=1,
            episodes_json=[2],
        )
        session.add(download)
        await session.commit()

    async with sessionmaker_() as session:
        download_repo = SqlDownloadRepository(session)
        missing = await season_episode_service.compute_missing(
            session,
            download_repo,
            media_request_id=show_id,
            season_number=1,
            season_request_id=season_request_id,
            target=frozenset({1, 2, 3}),
        )

    assert missing == frozenset({1, 3})


async def test_compute_missing_active_pack_download_excludes_whole_target(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=804)
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.downloading)

    async with sessionmaker_() as session:
        download = Download(
            torrent_hash="active-pack-hash",
            status="downloading",
            media_request_id=show_id,
            season=1,
            episodes_json=None,
        )
        session.add(download)
        await session.commit()

    async with sessionmaker_() as session:
        download_repo = SqlDownloadRepository(session)
        missing = await season_episode_service.compute_missing(
            session,
            download_repo,
            media_request_id=show_id,
            season_number=1,
            season_request_id=season_request_id,
            target=frozenset({1, 2, 3}),
        )

    assert missing == frozenset()


async def test_compute_missing_no_active_download_returns_full_gap(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=8035)
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.searching)

    async with sessionmaker_() as session:
        episode_repo = SqlSeasonEpisodeStateRepository(session)
        await episode_repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})
        await session.commit()

    async with sessionmaker_() as session:
        download_repo = SqlDownloadRepository(session)
        missing = await season_episode_service.compute_missing(
            session,
            download_repo,
            media_request_id=show_id,
            season_number=1,
            season_request_id=season_request_id,
            target=frozenset({1, 2}),
        )

    assert missing == frozenset({1, 2})


async def test_apply_import_unknown_target_completes_legacy(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=805)
    async with sessionmaker_() as session:
        download = Download(torrent_hash="legacy-pack-hash", status="imported")
        session.add(download)
        await session.commit()
        download_id = download.id

    async with sessionmaker_() as session:
        complete = await season_episode_service.apply_import(
            session,
            media_request_id=show_id,
            season_number=1,
            imported_episodes=[1, 2, 3],
            download_id=download_id,
            target=frozenset(),
        )
        await session.commit()

    assert complete is True


async def test_apply_import_partial_target_not_complete(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=806)
    async with sessionmaker_() as session:
        download = Download(torrent_hash="partial-hash", status="imported")
        session.add(download)
        await session.commit()
        download_id = download.id

    async with sessionmaker_() as session:
        complete = await season_episode_service.apply_import(
            session,
            media_request_id=show_id,
            season_number=1,
            imported_episodes=[4],
            download_id=download_id,
            target=frozenset({4, 5}),
        )
        await session.commit()

    assert complete is False


async def test_apply_import_final_episode_completes(sessionmaker_: SessionMaker) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=807)
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.downloading)

    async with sessionmaker_() as session:
        download = Download(torrent_hash="first-episode-hash", status="imported")
        session.add(download)
        await session.commit()
        first_download_id = download.id
        episode_repo = SqlSeasonEpisodeStateRepository(session)
        await episode_repo.mark_imported(season_request_id, [4], download_id=first_download_id)
        await session.commit()

    async with sessionmaker_() as session:
        download = Download(torrent_hash="second-episode-hash", status="imported")
        session.add(download)
        await session.commit()
        second_download_id = download.id

        complete = await season_episode_service.apply_import(
            session,
            media_request_id=show_id,
            season_number=1,
            imported_episodes=[5],
            download_id=second_download_id,
            target=frozenset({4, 5}),
        )
        await session.commit()

    assert complete is True


async def test_reconcile_airing_rearms_a_season_whose_target_grew(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=808)
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.available)

    async with sessionmaker_() as session:
        download = Download(torrent_hash="rearm-target-hash", status="imported")
        session.add(download)
        await session.commit()
        download_id = download.id
        episode_repo = SqlSeasonEpisodeStateRepository(session)
        await episode_repo.upsert_target(
            season_request_id, {1: date(2026, 1, 1), 2: date(2026, 1, 8)}
        )
        await episode_repo.mark_imported(season_request_id, [1, 2], download_id=download_id)
        await session.commit()

    tmdb = FakeTmdb(
        season_episodes={
            (808, 1): [
                EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1)),
                EpisodeInfo(episode_number=2, air_date=date(2026, 1, 8)),
                EpisodeInfo(episode_number=3, air_date=date(2026, 1, 15)),  # newly aired
            ]
        }
    )

    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, today=_TODAY, max_refresh=5
        )
        await session.commit()

    assert rearmed == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.searching
        show = await session.get(MediaRequest, show_id)
        assert show is not None
        assert show.status == RequestStatus.searching


async def test_reconcile_airing_leaves_season_alone_when_target_unchanged(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=809)
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.available)

    async with sessionmaker_() as session:
        download = Download(torrent_hash="unchanged-target-hash", status="imported")
        session.add(download)
        await session.commit()
        download_id = download.id
        episode_repo = SqlSeasonEpisodeStateRepository(session)
        await episode_repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})
        await episode_repo.mark_imported(season_request_id, [1], download_id=download_id)
        await session.commit()

    tmdb = FakeTmdb(
        season_episodes={(809, 1): [EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1))]}
    )

    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, today=_TODAY, max_refresh=5
        )
        await session.commit()

    assert rearmed == 0
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.available


async def test_reconcile_airing_skips_a_season_on_tmdb_error_without_aborting(
    sessionmaker_: SessionMaker,
) -> None:
    show_id = await _make_show(sessionmaker_, tmdb_id=810)
    await _make_season(sessionmaker_, show_id, 1, RequestStatus.available)

    tmdb = FakeTmdb(season_episodes_error=TmdbApiError("tmdb down"))

    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, today=_TODAY, max_refresh=5
        )
        await session.commit()

    assert rearmed == 0


async def test_reconcile_airing_rotates_the_refresh_window_across_cycles(
    sessionmaker_: SessionMaker,
) -> None:
    """P2 fix (issue #178 review): with MORE airing/completed seasons than
    ``max_refresh``, the candidate window must ROTATE across cycles, not always
    return the same id-lowest slice. Pre-fix, seasons past the first
    ``max_refresh`` (by id) would NEVER be re-checked, so a legitimately-aired
    new episode on one of them could never re-arm the season.

    Seven shows, each with a season whose TMDB target already equals what is
    imported (no rearm -- isolates the rotation itself from the rearm decision).
    ``max_refresh=3`` over two cycles must touch SIX DISTINCT shows, not the
    SAME three twice.
    """
    tmdb_ids = list(range(900, 907))  # 7 shows

    for tmdb_id in tmdb_ids:
        show_id = await _make_show(sessionmaker_, tmdb_id=tmdb_id)
        season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.available)
        async with sessionmaker_() as session:
            download = Download(torrent_hash=f"rotation-hash-{tmdb_id}", status="imported")
            session.add(download)
            await session.commit()
            episode_repo = SqlSeasonEpisodeStateRepository(session)
            await episode_repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})
            await episode_repo.mark_imported(season_request_id, [1], download_id=download.id)
            await session.commit()

    tmdb = FakeTmdb(
        season_episodes={
            (tmdb_id, 1): [EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1))]
            for tmdb_id in tmdb_ids
        }
    )

    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, today=_TODAY, max_refresh=3
        )
        await session.commit()
    assert rearmed == 0
    first_cycle = set(tmdb.season_episodes_calls)
    assert len(first_cycle) == 3

    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, today=_TODAY, max_refresh=3
        )
        await session.commit()
    assert rearmed == 0
    second_cycle = set(tmdb.season_episodes_calls) - first_cycle
    assert len(second_cycle) == 3

    # The SAME three shows must not have been picked again -- proves the window
    # rotated instead of collapsing back to the id-lowest slice.
    assert first_cycle.isdisjoint(second_cycle)
