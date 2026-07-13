from __future__ import annotations

from sqlalchemy import select

from plex_manager.models import SeasonRequest, User, WatchlistItem
from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.ports.watchlist import WatchlistEntry
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import request_service, watchlist_service
from tests.services.conftest import SessionMaker
from tests.web.fakes import FakeTmdb


class FakeWatchlist:
    def __init__(self, entries: tuple[WatchlistEntry, ...]) -> None:
        self.entries = entries

    async def list_entries(self) -> tuple[WatchlistEntry, ...]:
        return self.entries


async def test_sync_replaces_snapshot_and_creates_requests(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        user = User(username="watcher", encrypted_plex_token="token")  # noqa: S106
        session.add(user)
        await session.commit()
        user_id = user.id

    tmdb = FakeTmdb(
        movies={603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999)},
        shows={1396: TvMetadata(tmdb_id=1396, title="Breaking Bad", year=2008, season_count=1)},
    )
    watchlist = FakeWatchlist(
        (
            WatchlistEntry(tmdb_id=603, media_type="movie"),
            WatchlistEntry(tmdb_id=1396, media_type="tv"),
        )
    )
    async with sessionmaker_() as session:
        result = await watchlist_service.sync_user(session, watchlist, tmdb, user_id=user_id)
    assert result.fetched == 2
    assert result.created == 2
    assert result.failed == 0

    async with sessionmaker_() as session:
        snapshots = list((await session.execute(WatchlistItem.__table__.select())).all())
        visible = await SqlRequestRepository(session).list_for_user(user_id)
    assert len(snapshots) == 2
    assert {(row.tmdb_id, row.media_type) for row in visible} == {(603, "movie"), (1396, "tv")}


async def test_successful_empty_sync_removes_only_snapshot(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        user = User(username="watcher", encrypted_plex_token="token")  # noqa: S106
        session.add(user)
        await session.flush()
        user_id = user.id
        session.add(WatchlistItem(user_id=user_id, tmdb_id=603, media_type="movie"))
        await session.commit()
    async with sessionmaker_() as session:
        result = await watchlist_service.sync_user(
            session, FakeWatchlist(()), FakeTmdb(), user_id=user_id
        )
    assert result.fetched == 0
    async with sessionmaker_() as session:
        assert list((await session.execute(WatchlistItem.__table__.select())).all()) == []


async def test_sync_continues_after_bad_entry(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        user = User(username="resilient-watcher", encrypted_plex_token="token")  # noqa: S106
        session.add(user)
        await session.commit()
        user_id = user.id

    watchlist = FakeWatchlist(
        (
            WatchlistEntry(tmdb_id=999, media_type="movie"),
            WatchlistEntry(tmdb_id=603, media_type="movie"),
        )
    )
    tmdb = FakeTmdb(movies={603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999)})
    async with sessionmaker_() as session:
        result = await watchlist_service.sync_user(session, watchlist, tmdb, user_id=user_id)
    assert result.fetched == 2
    assert result.created == 1
    assert result.existing == 0
    assert result.failed == 1

    async with sessionmaker_() as session:
        snapshots = list((await session.execute(WatchlistItem.__table__.select())).all())
        visible = await SqlRequestRepository(session).list_for_user(user_id)
    assert {(row.tmdb_id, row.media_type) for row in snapshots} == {
        (999, "movie"),
        (603, "movie"),
    }
    assert [(row.tmdb_id, row.media_type) for row in visible] == [(603, "movie")]


async def test_sync_expands_foreign_shared_tv_request_to_whole_show(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        owner = User(username="show-owner")
        watcher = User(username="show-watcher", encrypted_plex_token="token")  # noqa: S106
        session.add_all((owner, watcher))
        await session.commit()
        owner_id, watcher_id = owner.id, watcher.id

    tmdb = FakeTmdb(
        shows={1396: TvMetadata(tmdb_id=1396, title="Breaking Bad", year=2008, season_count=3)}
    )
    async with sessionmaker_() as session:
        existing = await request_service.create_request(
            session,
            tmdb,
            tmdb_id=1396,
            media_type="tv",
            seasons=[1],
            user_id=owner_id,
        )
    async with sessionmaker_() as session:
        result = await watchlist_service.sync_user(
            session,
            FakeWatchlist((WatchlistEntry(tmdb_id=1396, media_type="tv"),)),
            tmdb,
            user_id=watcher_id,
        )
    assert result.created == 0
    assert result.existing == 1

    async with sessionmaker_() as session:
        seasons = (
            (
                await session.execute(
                    select(SeasonRequest).where(SeasonRequest.media_request_id == existing.id)
                )
            )
            .scalars()
            .all()
        )
        assert await SqlRequestRepository(session).is_subscriber(existing.id, watcher_id)
    assert {season.season_number for season in seasons} == {1, 2, 3}


def test_worker_status_distinguishes_success_degraded_error_and_skips() -> None:
    status = watchlist_service.WatchlistWorkerStatus()
    assert status.state == "starting"

    status.mark_started()
    status.mark_completed(fetched=2, created=1, existing=1, failed_users=0, error=None)
    first_ok = status.last_ok_at
    assert first_ok is not None
    assert status.state == "ok"

    status.mark_started()
    status.mark_completed(
        fetched=2,
        created=1,
        existing=0,
        failed_users=1,
        error="WatchlistEntryError",
    )
    assert status.state == "degraded"
    assert status.last_ok_at == first_ok
    assert status.last_error_type == "WatchlistEntryError"
    assert status.last_error_at is not None

    status.mark_started()
    status.mark_error(RuntimeError("boom"))
    assert status.state == "error"
    assert status.last_ok_at == first_ok
    assert status.last_error_type == "RuntimeError"

    status.mark_started()
    status.mark_skipped("disabled")
    assert status.state == "disabled"
    assert status.last_ok_at == first_ok
    assert status.last_error_type is None

    status.mark_started()
    status.mark_skipped("not_configured")
    assert status.state == "not_configured"
    assert status.last_ok_at == first_ok
