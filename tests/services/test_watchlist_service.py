from __future__ import annotations

from plex_manager.models import User, WatchlistItem
from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.ports.watchlist import WatchlistEntry
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import watchlist_service
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
