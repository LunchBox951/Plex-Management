"""Watchlist worker lifecycle and honest skip-state coverage."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.adapters.plex.oauth import PlexTvClient
from plex_manager.models import User, WatchlistItem
from plex_manager.web import app as app_module
from plex_manager.web.deps import PLEX_MACHINE_ID_SETTING, SettingsStore

SeedFn = Callable[..., Awaitable[None]]

_MACHINE_ID = "configured-server-machine-id"


def _plex_tv_resources_transport(resources: list[dict[str, object]]) -> httpx.MockTransport:
    """A transport answering plex.tv ``/api/v2/resources`` (used by watchlist
    revalidation). Any other path answers a trivial 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=resources)
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handler)


def _server_resource(machine_id: str) -> dict[str, object]:
    return {
        "name": "Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [],
    }


async def test_watchlist_tick_reports_disabled_without_claiming_success(
    app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        await SettingsStore(session).set("watchlist_sync_enabled", "false")
        await session.commit()

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.state == "disabled"
    assert status.last_run_at is not None
    assert status.last_ok_at is None


async def test_disabling_sync_clears_stale_snapshot_rows(app: FastAPI, seed: SeedFn) -> None:
    """Turning off watchlist sync must END eviction protection, not merely stop
    future ticks: the disabled tick clears the stored snapshot (#296)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        await SettingsStore(session).set("watchlist_sync_enabled", "false")
        user = User(username="watcher", encrypted_plex_token="t")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    assert app.state.watchlist_status.state == "disabled"

    async with app.state.sessionmaker() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert remaining == []


async def test_stale_user_is_skipped_and_snapshot_cleared(app: FastAPI, seed: SeedFn) -> None:
    """A stored token that no longer reaches the configured server (e.g. after a
    repoint) is skipped AND its pre-existing snapshot rows are deleted, so a stale
    old-server account can neither create nor keep PROTECTING titles from eviction
    on the new server (#296 finding 1 -- both halves)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("tmdb_api_key", "tmdb-key")
        await store.set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        user = User(username="stale-watcher", encrypted_plex_token="old-token")  # noqa: S106
        session.add(user)
        await session.flush()
        # A snapshot row left behind from when this account WAS authorized: without
        # the clear-on-stale fix it would keep protecting tmdb 603 forever.
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    # plex.tv only advertises a DIFFERENT server for this token: not authorized.
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(
        transport=_plex_tv_resources_transport([_server_resource("some-other-server")])
    )

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.skipped_users == 1
    assert status.created == 0
    # A tick that only skipped users fetched nothing: it must read degraded and not
    # advance last_ok_at, so /health cannot claim success (#296, north star #3).
    assert status.state == "degraded"
    assert status.last_ok_at is None
    async with app.state.sessionmaker() as session:
        assert list((await session.execute(WatchlistItem.__table__.select())).all()) == []


async def test_unknown_authorization_retains_snapshot(app: FastAPI, seed: SeedFn) -> None:
    """A transient plex.tv outage (authorization UNKNOWN) must skip the tick but
    RETAIN the snapshot -- it must never be mistaken for a revoked account and
    have its eviction-protection rows deleted (#296)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("tmdb_api_key", "tmdb-key")
        await store.set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        user = User(username="watcher", encrypted_plex_token="live-token")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    def _unreachable(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            raise httpx.ConnectError("plex.tv unreachable", request=request)
        return httpx.Response(200, text="ok")

    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_unreachable))

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.skipped_users == 1
    # Nothing was fetched (plex.tv unreachable): the tick is degraded, not ok.
    assert status.state == "degraded"
    assert status.last_ok_at is None
    async with app.state.sessionmaker() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert len(remaining) == 1


async def test_watchlist_tick_reports_not_configured_without_tmdb(
    app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True)

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.state == "not_configured"
    assert status.last_run_at is not None
    assert status.last_ok_at is None


async def test_unconfigured_server_clears_stale_snapshot(app: FastAPI, seed: SeedFn) -> None:
    """A truly unconfigured server (no url/token AND no cached identifier -- an
    operator explicitly walked away from Plex) must clear existing watchlist
    snapshot rows: they can never be revalidated against a server that no
    longer exists in config, so keeping them would protect titles from
    eviction indefinitely (issue #327 facet 2)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        user = User(username="orphaned-watcher", encrypted_plex_token="old-token")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.state == "not_configured"
    async with app.state.sessionmaker() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert remaining == []


async def test_reconfigure_mid_tick_prevents_spurious_snapshot_clear(
    app: FastAPI, seed: SeedFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent settings PUT that reconfigures Plex in the gap between the
    tick's ``not_configured`` read and its destructive snapshot clear must not
    lose the race: the re-confirm immediately before the delete retains the
    snapshot for the next tick to re-evaluate instead of clearing it out from
    under a config that is valid again by the time the delete runs (issue
    #327 adversarial race review)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        user = User(username="watcher", encrypted_plex_token="live-token")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    real_resolve = app_module._resolve_watchlist_server_identity  # pyright: ignore[reportPrivateUsage]
    calls = 0

    async def flaky_resolve(
        store: SettingsStore, plex_tv: PlexTvClient
    ) -> app_module._ServerIdentityResolution:  # pyright: ignore[reportPrivateUsage]
        nonlocal calls
        calls += 1
        if calls == 1:
            return await real_resolve(store, plex_tv)  # unconfigured, as seeded
        # A concurrent settings PUT reconfigures Plex between the two reads.
        async with app.state.sessionmaker() as reconfigure_session:
            await SettingsStore(reconfigure_session).set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
            await reconfigure_session.commit()
        return await real_resolve(store, plex_tv)

    monkeypatch.setattr(app_module, "_resolve_watchlist_server_identity", flaky_resolve)

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    assert calls == 2
    assert app.state.watchlist_status.state == "not_configured"
    async with app.state.sessionmaker() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert len(remaining) == 1


async def test_probe_failure_reports_probe_failed_and_retains_snapshot(
    app: FastAPI, seed: SeedFn
) -> None:
    """An upgraded/pre-rework install that has ``plex_url``/``plex_token`` but no
    cached machine identifier yet: a transient ``/identity`` probe failure must
    surface as a distinct ``probe_failed`` state -- never ``not_configured``,
    which would mislabel an outage as an absence -- and must RETAIN any
    existing watchlist snapshot, since a transient failure must never drop
    eviction protection (issue #327 facets 1 and 2)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("plex_url", "http://plex.example.com:32400")
        await store.set("plex_token", "service-token")
        user = User(username="watcher", encrypted_plex_token="live-token")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    def _unreachable(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity":
            raise httpx.ConnectError("plex server unreachable", request=request)
        return httpx.Response(200, text="ok")

    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(_unreachable))

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.state == "probe_failed"
    assert status.state != "not_configured"
    assert status.last_error_type == "PlexVerifyError"
    async with app.state.sessionmaker() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert len(remaining) == 1


async def test_tick_persists_minted_client_identifier(app: FastAPI, seed: SeedFn) -> None:
    """The plex.tv device identifier minted by a tick must be COMMITTED, not rolled
    back when the settings session closes: an uncommitted mint would still be used
    for that tick's plex.tv calls, registering a fresh phantom device on every tick
    of an install lacking the setting (the helper's create-once contract)."""
    await seed(initialized=True)

    await app_module._watchlist_sync_once(app)  # pyright: ignore[reportPrivateUsage]
    async with app.state.sessionmaker() as session:
        first = await SettingsStore(session).get("plex_oauth_client_identifier")
    assert first  # persisted, visible to a fresh session

    # A second tick reuses the stored identifier instead of minting another.
    await app_module._watchlist_sync_once(app)  # pyright: ignore[reportPrivateUsage]
    async with app.state.sessionmaker() as session:
        assert await SettingsStore(session).get("plex_oauth_client_identifier") == first


async def test_stale_cleanup_runs_even_without_tmdb_configured(app: FastAPI, seed: SeedFn) -> None:
    """Stale-token cleanup needs only the stored Plex tokens and the configured
    server identity -- it must NOT be gated on a TMDB key. After a repoint on an
    install without TMDB, the old server's rows must still stop protecting titles;
    only request CREATION is TMDB-gated (#296)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        await SettingsStore(session).set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        user = User(username="stale-watcher", encrypted_plex_token="old-token")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(
        transport=_plex_tv_resources_transport([_server_resource("some-other-server")])
    )

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    # The tick still ends not_configured (no TMDB key -> no request creation)...
    assert status.state == "not_configured"
    # ...but the stale snapshot was cleared BEFORE the TMDB gate...
    async with app.state.sessionmaker() as session:
        assert list((await session.execute(WatchlistItem.__table__.select())).all()) == []
    # ...and that cleanup is NOT erased from /health: a tick that actually
    # cleared a stale-after-repoint snapshot must not report a clean
    # not_configured with no trace of the cleanup (issue #327 facet 3).
    assert status.skipped_users == 1


async def test_stale_delete_skipped_when_server_repointed_mid_tick(
    app: FastAPI, seed: SeedFn
) -> None:
    """Repoint race: the STALE verdict was computed against the server identity
    resolved at tick start. If the admin repoints AGAIN before the delete runs, the
    verdict belonged to the PREVIOUS machine identifier -- the deleting transaction
    re-resolves the current identity and must retain the snapshot for
    re-evaluation against the new server (#296)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("tmdb_api_key", "tmdb-key")
        await store.set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        user = User(username="stale-watcher", encrypted_plex_token="old-token")  # noqa: S106
        session.add(user)
        await session.flush()
        session.add(WatchlistItem(user_id=user.id, tmdb_id=603, media_type="movie"))
        await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            # The revalidation call itself is the race window: repoint the server
            # (as the settings router's verified-repoint path would) BEFORE
            # answering with a stale verdict for the OLD identity.
            async with app.state.sessionmaker() as session:
                await SettingsStore(session).set(PLEX_MACHINE_ID_SETTING, "repointed-again-mid")
                await session.commit()
            return httpx.Response(200, json=[_server_resource("some-other-server")])
        return httpx.Response(200, text="ok")

    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.skipped_users == 1
    # The snapshot survives: the stale verdict was against the outdated identity.
    async with app.state.sessionmaker() as session:
        remaining = list((await session.execute(WatchlistItem.__table__.select())).all())
    assert len(remaining) == 1


async def test_sync_pass_skipped_when_server_repointed_after_authorization(
    app: FastAPI, seed: SeedFn
) -> None:
    """Repoint race, AUTHORIZED branch: tokens vetted against the tick-start
    identity must not drive a sync pass once the admin repoints -- unlike snapshot
    rows, requests created from the old server's watchlists cannot be undone by
    the next tick. The pass is skipped wholesale (users counted as skipped, tick
    degraded); the repoint's own wake re-authorizes imminently (#296)."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("tmdb_api_key", "tmdb-key")
        await store.set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        user = User(username="old-server-watcher", encrypted_plex_token="a-token")  # noqa: S106
        session.add(user)
        await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            # AUTHORIZED for the tick-start identity -- then the admin repoints
            # before the sync pass runs (the settings router's verified-repoint
            # path). Any later watchlist fetch would hit the non-JSON fallthrough
            # below and fail the test loudly if the guard regressed.
            async with app.state.sessionmaker() as session:
                await SettingsStore(session).set(PLEX_MACHINE_ID_SETTING, "repointed-post-auth")
                await session.commit()
            return httpx.Response(200, json=[_server_resource(_MACHINE_ID)])
        return httpx.Response(200, text="ok")

    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    assert await app_module._watchlist_sync_once(app) == 0  # pyright: ignore[reportPrivateUsage]
    status = app.state.watchlist_status
    assert status.state == "degraded"
    assert status.skipped_users == 1
    assert status.fetched == 0
    assert status.created == 0


async def test_watchlist_loop_wakes_immediately_when_settings_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_tick = asyncio.Event()
    second_tick = asyncio.Event()
    calls = 0

    async def fake_tick(_app: FastAPI) -> int:
        nonlocal calls
        calls += 1
        (first_tick if calls == 1 else second_tick).set()
        return 0

    async def long_interval(_session: object) -> float:
        return 10_080

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(app_module, "_watchlist_sync_once", fake_tick)
    monkeypatch.setattr(app_module, "get_watchlist_sync_interval_minutes", long_interval)
    app = FastAPI()
    app.state.sessionmaker = lambda: SessionContext()
    app.state.watchlist_wake_event = asyncio.Event()
    task = asyncio.create_task(
        app_module._watchlist_sync_loop(app)  # pyright: ignore[reportPrivateUsage]
    )
    try:
        await asyncio.wait_for(first_tick.wait(), timeout=1)
        app.state.watchlist_wake_event.set()
        await asyncio.wait_for(second_tick.wait(), timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert calls >= 2
