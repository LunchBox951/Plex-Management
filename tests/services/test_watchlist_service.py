from __future__ import annotations

from typing import cast

import httpx
from sqlalchemy import Table, select

from plex_manager.adapters.plex.oauth import PlexTvClient
from plex_manager.models import SeasonRequest, User, WatchlistItem
from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.ports.watchlist import WatchlistEntry
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import request_service, watchlist_service
from plex_manager.services.watchlist_service import SyncUserAuthorization
from tests.services.conftest import SessionMaker
from tests.web.fakes import FakeTmdb

_MACHINE_ID = "configured-server-machine-id"
_TOKEN = "user-plex-token"  # noqa: S105


def _resources_transport(
    resources: list[dict[str, object]] | int,
) -> httpx.MockTransport:
    """A plex.tv ``/api/v2/resources`` transport. Pass an int to answer that
    status code (e.g. 401 for a rejected token) instead of a resource array."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(resources, int):
            return httpx.Response(resources, json={})
        return httpx.Response(200, json=resources)

    return httpx.MockTransport(handler)


def _server_resource(machine_id: str, *, owned: bool = True) -> dict[str, object]:
    return {
        "name": "Living Room",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": owned,
        "connections": [],
    }


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
    status.mark_completed(
        fetched=2,
        created=1,
        existing=1,
        failed_users=0,
        failed_entries=0,
        error=None,
    )
    first_ok = status.last_ok_at
    assert first_ok is not None
    assert status.state == "ok"

    status.mark_started()
    status.mark_completed(
        fetched=2,
        created=1,
        existing=0,
        failed_users=1,
        failed_entries=1,
        error="WatchlistEntryError",
    )
    assert status.state == "degraded"
    assert status.last_ok_at == first_ok
    assert status.last_error_type == "WatchlistEntryError"
    assert status.last_error_at is not None

    # A tick that fetched nothing because every candidate was SKIPPED (token
    # unrevalidatable / no longer authorized) has not succeeded: it must read
    # "degraded" and must NOT advance last_ok_at, exactly like a failed tick --
    # otherwise /health would claim success though nothing synced (#296, north
    # star #3). skipped_users drives this even with no error string.
    status.mark_started()
    status.mark_completed(
        fetched=0,
        created=0,
        existing=0,
        failed_users=0,
        failed_entries=0,
        error=None,
        skipped_users=1,
    )
    assert status.state == "degraded"
    assert status.last_ok_at == first_ok
    assert status.skipped_users == 1

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


async def test_revalidate_authorized_when_account_reaches_configured_server() -> None:
    transport = _resources_transport([_server_resource(_MACHINE_ID)])
    async with httpx.AsyncClient(transport=transport) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await watchlist_service.revalidate_sync_user(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert result is SyncUserAuthorization.AUTHORIZED


async def test_revalidate_stale_when_account_has_no_access_to_configured_server() -> None:
    # The account only reaches a DIFFERENT server (e.g. after a repoint): stale.
    transport = _resources_transport([_server_resource("some-other-server")])
    async with httpx.AsyncClient(transport=transport) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await watchlist_service.revalidate_sync_user(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert result is SyncUserAuthorization.STALE


async def test_revalidate_stale_when_token_rejected() -> None:
    async with httpx.AsyncClient(transport=_resources_transport(401)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await watchlist_service.revalidate_sync_user(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert result is SyncUserAuthorization.STALE


async def test_revalidate_unknown_when_plex_tv_unreachable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await watchlist_service.revalidate_sync_user(plex_tv, _MACHINE_ID, token=_TOKEN)
    # A transient plex.tv outage must not be read as a revoked account: retain.
    assert result is SyncUserAuthorization.UNKNOWN


async def test_revalidate_unknown_when_resources_malformed() -> None:
    # A 2xx /resources body that is NOT the expected JSON array (an error object,
    # a truncated/wrapped payload) must NOT be read as "zero resources" -> STALE
    # -> snapshot deleted. It is an undetermined result: UNKNOWN, retain (#296).
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "unexpected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await watchlist_service.revalidate_sync_user(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert result is SyncUserAuthorization.UNKNOWN


async def test_revalidate_stale_when_resources_genuinely_empty() -> None:
    # A genuine empty array IS a valid authorization signal: the account has zero
    # server resources, so it cannot reach the configured server -> STALE.
    async with httpx.AsyncClient(transport=_resources_transport([])) as client:
        plex_tv = PlexTvClient(client, client_identifier="pm-test")
        result = await watchlist_service.revalidate_sync_user(plex_tv, _MACHINE_ID, token=_TOKEN)
    assert result is SyncUserAuthorization.STALE


async def test_clear_user_snapshot_clears_when_token_unchanged(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        user = User(username="watcher", encrypted_plex_token=_TOKEN)
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()
        user_id = user.id

    async with sessionmaker_() as session:
        cleared = await watchlist_service.clear_user_snapshot(
            session, user_id=user_id, expected_token=_TOKEN
        )
        await session.commit()
    assert cleared == 1
    async with sessionmaker_() as session:
        assert list((await session.execute(WatchlistItem.__table__.select())).all()) == []


async def test_clear_user_snapshot_retains_when_token_changed(
    sessionmaker_: SessionMaker,
) -> None:
    # Re-sign-in race: the STALE decision was made from an OLD token, but the user
    # signed in again before the delete ran, replacing their stored token. Their
    # snapshot now backs a freshly-authorized account and must be RETAINED (#296).
    async with sessionmaker_() as session:
        user = User(username="watcher", encrypted_plex_token="new-token-after-resignin")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()
        user_id = user.id

    async with sessionmaker_() as session:
        cleared = await watchlist_service.clear_user_snapshot(
            session,
            user_id=user_id,
            expected_token="old-stale-token",  # noqa: S106 - test fixture, not a credential
        )
        await session.commit()
    assert cleared == 0
    async with sessionmaker_() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert len(remaining) == 1


async def test_clear_snapshots_removes_all_rows(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        alice = User(username="alice", encrypted_plex_token="a")  # noqa: S106
        bob = User(username="bob", encrypted_plex_token="b")  # noqa: S106
        session.add_all((alice, bob))
        await session.flush()
        session.add_all(
            (
                WatchlistItem(user_id=alice.id, tmdb_id=603, media_type="movie"),
                WatchlistItem(user_id=bob.id, tmdb_id=1396, media_type="tv"),
            )
        )
        await session.commit()

    async with sessionmaker_() as session:
        removed = await watchlist_service.clear_snapshots(session)
        await session.commit()
    assert removed == 2

    async with sessionmaker_() as session:
        assert list((await session.execute(WatchlistItem.__table__.select())).all()) == []
        # Idempotent: a second clear removes nothing.
        assert await watchlist_service.clear_snapshots(session) == 0


async def test_is_watchlisted_ignores_user_and_uses_tmdb_media(
    sessionmaker_: SessionMaker,
) -> None:
    async with sessionmaker_() as session:
        watcher = User(username="watcher", encrypted_plex_token="t")  # noqa: S106
        session.add(watcher)
        await session.flush()
        session.add(WatchlistItem(user_id=watcher.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    async with sessionmaker_() as session:
        assert await watchlist_service.is_watchlisted(session, 603, "movie") is True
        assert await watchlist_service.is_watchlisted(session, 603, "tv") is False
        assert await watchlist_service.is_watchlisted(session, 999, "movie") is False


def test_watchlist_items_has_tmdb_media_index() -> None:
    # The (tmdb_id, media_type) lookup index is what makes is_watchlisted seekable
    # despite the user_id-first composite PK (#296).
    table = cast(Table, WatchlistItem.__table__)
    index_columns = {tuple(col.name for col in index.columns) for index in table.indexes}
    assert ("tmdb_id", "media_type") in index_columns
