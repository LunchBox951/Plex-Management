"""``season_episode_service`` -- refresh_target / compute_missing / apply_import /
reconcile_airing (ADR-0020, issue #178).

Mirrors ``test_season_request_service.py``'s ``sessionmaker_`` fixture pattern.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

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
_NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)


async def _make_show(sm: SessionMaker, tmdb_id: int = 800) -> int:
    async with sm() as session:
        show = MediaRequest(
            tmdb_id=tmdb_id, media_type=MediaType.tv, title="Show", status=RequestStatus.pending
        )
        session.add(show)
        await session.commit()
        return show.id


async def _make_show_with_intent(
    sm: SessionMaker,
    *,
    tmdb_id: int,
    tv_request_mode: str,
    requested_episodes_json: dict[str, list[int]] | None = None,
) -> int:
    async with sm() as session:
        show = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType.tv,
            title="Show",
            status=RequestStatus.pending,
            tv_request_mode=tv_request_mode,
            requested_episodes_json=requested_episodes_json,
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
            session, tmdb, now=_NOW, max_refresh=5
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
            session, tmdb, now=_NOW, max_refresh=5
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
            session, tmdb, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert rearmed == 0


async def test_reconcile_airing_skips_episode_scoped_request(
    sessionmaker_: SessionMaker,
) -> None:
    """P1 fix (issue #178 review): an explicit episode-scoped request is TERMINAL
    for the episode it named. The airing refresh must NOT widen it to the whole
    aired season -- doing so would seed the whole season, see only the requested
    episode imported, and re-arm the completed request -> duplicate grabs. The
    season is left available and TMDB is never even consulted for it.
    """
    show_id = await _make_show_with_intent(
        sessionmaker_,
        tmdb_id=811,
        tv_request_mode="explicit_episodes",
        requested_episodes_json={"1": [5]},
    )
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.available)

    async with sessionmaker_() as session:
        download = Download(torrent_hash="ep-scoped-hash", status="imported")
        session.add(download)
        await session.commit()
        episode_repo = SqlSeasonEpisodeStateRepository(session)
        await episode_repo.upsert_target(season_request_id, {5: date(2026, 1, 29)})
        await episode_repo.mark_imported(season_request_id, [5], download_id=download.id)
        await session.commit()

    # TMDB would report the whole aired season -- if the refresh ran it would seed
    # {1..10} and re-arm. It must never be called for an episode-scoped season.
    tmdb = FakeTmdb(
        season_episodes={
            (811, 1): [
                EpisodeInfo(episode_number=n, air_date=date(2026, 1, 1)) for n in range(1, 11)
            ]
        }
    )

    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert rearmed == 0
    assert tmdb.season_episodes_calls == []  # never widened to the whole season
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.available  # stays terminal
        episode_repo = SqlSeasonEpisodeStateRepository(session)
        rows = await episode_repo.list_for_season(season_request_id)
    # Only the originally-requested episode's row -- the season was NOT seeded.
    assert {r.episode_number for r in rows} == {5}
    assert rows[0].status == "imported"


async def test_reconcile_airing_adopts_baseline_for_done_season_with_no_rows(
    sessionmaker_: SessionMaker,
) -> None:
    """P1 fix (issue #178 review): an already-watchable season with NO episode-
    state rows (Plex already owned it, or a whole-season-pack import the migration
    seeded nothing for) is fully OWNED. The refresh must NOT re-arm it (that would
    re-download owned content); it adopts the aired target as the imported
    baseline instead.
    """
    show_id = await _make_show_with_intent(sessionmaker_, tmdb_id=812, tv_request_mode="whole_show")
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.available)

    tmdb = FakeTmdb(
        season_episodes={
            (812, 1): [
                EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1)),
                EpisodeInfo(episode_number=2, air_date=date(2026, 1, 8)),
                EpisodeInfo(episode_number=3, air_date=date(2026, 1, 15)),
            ]
        }
    )

    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert rearmed == 0  # adopted as baseline, never re-armed
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.available  # stays watchable
        episode_repo = SqlSeasonEpisodeStateRepository(session)
        rows = await episode_repo.list_for_season(season_request_id)
    by_episode = {r.episode_number: r for r in rows}
    assert set(by_episode) == {1, 2, 3}
    assert all(r.status == "imported" for r in by_episode.values())
    assert all(r.grabbed_download_id is None for r in by_episode.values())


async def test_reconcile_airing_baseline_adoption_still_rearms_on_later_growth(
    sessionmaker_: SessionMaker,
) -> None:
    """Baseline adoption must not break FUTURE airing growth: once a no-baseline
    done season has adopted its aired target as imported, a genuinely newly-aired
    episode on a LATER cycle must still re-arm the season.
    """
    show_id = await _make_show_with_intent(sessionmaker_, tmdb_id=813, tv_request_mode="whole_show")
    season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.available)

    # Cycle 1: aired {1, 2} with no baseline -> adopted, not re-armed.
    tmdb = FakeTmdb(
        season_episodes={
            (813, 1): [
                EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1)),
                EpisodeInfo(episode_number=2, air_date=date(2026, 1, 8)),
            ]
        }
    )
    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, now=_NOW, max_refresh=5
        )
        await session.commit()
    assert rearmed == 0

    # Cycle 2: episode 3 newly aired -> not in the adopted baseline -> re-arm.
    tmdb_grown = FakeTmdb(
        season_episodes={
            (813, 1): [
                EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1)),
                EpisodeInfo(episode_number=2, air_date=date(2026, 1, 8)),
                EpisodeInfo(episode_number=3, air_date=date(2026, 1, 15)),
            ]
        }
    )
    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb_grown, now=_NOW, max_refresh=5
        )
        await session.commit()

    assert rearmed == 1
    async with sessionmaker_() as session:
        season = await session.get(SeasonRequest, season_request_id)
        assert season is not None
        assert season.status == RequestStatus.searching


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
            session, tmdb, now=_NOW, max_refresh=3
        )
        await session.commit()
    assert rearmed == 0
    first_cycle = set(tmdb.season_episodes_calls)
    assert len(first_cycle) == 3

    async with sessionmaker_() as session:
        rearmed = await season_episode_service.reconcile_airing(
            session, tmdb, now=_NOW, max_refresh=3
        )
        await session.commit()
    assert rearmed == 0
    second_cycle = set(tmdb.season_episodes_calls) - first_cycle
    assert len(second_cycle) == 3

    # The SAME three shows must not have been picked again -- proves the window
    # rotated instead of collapsing back to the id-lowest slice.
    assert first_cycle.isdisjoint(second_cycle)


async def test_reconcile_airing_rotation_keeps_advancing_within_the_same_day(
    sessionmaker_: SessionMaker,
) -> None:
    """P2 fix (issue #178 review round 2): the rotation cursor is a TIMESTAMP,
    not a date. With a date-granular cursor, once every candidate carried today's
    stamp the ordering degraded to ``id`` and every remaining same-day cycle
    re-selected the SAME lowest-id slice, starving the higher-id seasons until
    midnight.

    Four shows, ``max_refresh=2``, four cycles at successive times on ONE day:
    cycle 1 -> {A, B}, cycle 2 -> {C, D}, cycle 3 -> {A, B} (oldest stamps),
    cycle 4 MUST -> {C, D}. A date cursor would give cycle 4 = {A, B} again
    (same-day tie broken by id).
    """
    tmdb_ids = list(range(920, 924))  # 4 shows: A, B, C, D

    for tmdb_id in tmdb_ids:
        show_id = await _make_show(sessionmaker_, tmdb_id=tmdb_id)
        season_request_id = await _make_season(sessionmaker_, show_id, 1, RequestStatus.available)
        async with sessionmaker_() as session:
            download = Download(torrent_hash=f"same-day-hash-{tmdb_id}", status="imported")
            session.add(download)
            await session.commit()
            episode_repo = SqlSeasonEpisodeStateRepository(session)
            await episode_repo.upsert_target(season_request_id, {1: date(2026, 1, 1)})
            await episode_repo.mark_imported(season_request_id, [1], download_id=download.id)
            await session.commit()

    episodes = {
        (tmdb_id, 1): [EpisodeInfo(episode_number=1, air_date=date(2026, 1, 1))]
        for tmdb_id in tmdb_ids
    }

    cycles: list[set[tuple[int, int]]] = []
    for minutes in (0, 5, 10, 15):  # four cycles, all on _NOW's calendar day
        tmdb = FakeTmdb(season_episodes=dict(episodes))
        async with sessionmaker_() as session:
            rearmed = await season_episode_service.reconcile_airing(
                session, tmdb, now=_NOW + timedelta(minutes=minutes), max_refresh=2
            )
            await session.commit()
        assert rearmed == 0
        cycles.append(set(tmdb.season_episodes_calls))

    assert all(len(cycle) == 2 for cycle in cycles)
    assert cycles[0].isdisjoint(cycles[1])  # {A,B} then {C,D}
    assert cycles[2] == cycles[0]  # oldest stamps come back around first
    # THE pin: the 4th same-day cycle keeps rotating to the OTHER half. A
    # date-granular cursor would have collapsed to id-order here and re-picked
    # cycles[0]'s pair a second consecutive time.
    assert cycles[3] == cycles[1]
