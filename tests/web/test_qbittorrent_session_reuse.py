"""``resolve_qbittorrent`` session-reuse tests (PR-C concern 1).

Before this fix, every caller of ``get_qbittorrent`` — including the reconcile
and auto-grab loops on their 15s/60s ticks — built a BRAND NEW
``QbittorrentClient`` with ``_logged_in = False``, so it re-``POST``ed
``/auth/login`` every cycle. Since #177 the qBittorrent session cookie is held
BY the adapter instance (``_session_cookie``), never the process-wide
``httpx.AsyncClient`` jar, so a fresh instance loses the captured SID itself —
instance reuse is the ONLY thing that keeps the session alive across cycles.
``resolve_qbittorrent`` caches ONE client instance on ``app.state``, keyed on
the effective qbt settings, so both ``_logged_in`` and the adapter-local
``_session_cookie`` persist across cycles and a genuine login only happens once
per process lifetime (or once per settings change / genuine 403).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import State

from plex_manager.web.deps import ServiceNotConfiguredError, SettingsStore, resolve_qbittorrent

SessionMaker = async_sessionmaker[AsyncSession]

_BASE_URL = "http://qbit.local:8080"
_USERNAME = "admin"
_PASSWORD = "s3cret"  # noqa: S105


async def _seed_qbt_settings(sessionmaker_: SessionMaker) -> None:
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("qbittorrent_url", _BASE_URL)
        await store.set("qbittorrent_username", _USERNAME)
        await store.set("qbittorrent_password", _PASSWORD)
        await session.commit()


def _login_response() -> httpx.Response:
    return httpx.Response(200, text="Ok.", headers={"Set-Cookie": "SID=test-session-id; path=/"})


def _counting_router(
    *,
    login_counter: list[int],
    first_info_status: int = 200,
    info_cookies: list[str] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """A router that counts ``/auth/login`` POSTs and, optionally, returns a
    single 403 on the first ``/torrents/info`` call before succeeding. When
    ``info_cookies`` is given, each ``/torrents/info`` call's ``Cookie`` header is
    recorded — proving the adapter authenticates via its explicit adapter-local
    cookie header (post-#177) rather than the shared client jar."""
    info_calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/api/v2/auth/login" and method == "POST":
            login_counter[0] += 1
            return _login_response()
        if path == "/api/v2/torrents/info" and method == "GET":
            info_calls["count"] += 1
            if info_cookies is not None:
                info_cookies.append(request.headers.get("Cookie", ""))
            if info_calls["count"] == 1 and first_info_status != 200:
                return httpx.Response(first_info_status, text="")
            return httpx.Response(200, json=[])
        return httpx.Response(404, text="unhandled")

    return handler


async def test_client_identity_reused_across_cycles_single_login(
    sessionmaker_: SessionMaker,
) -> None:
    await _seed_qbt_settings(sessionmaker_)
    login_counter = [0]
    info_cookies: list[str] = []
    transport = httpx.MockTransport(
        _counting_router(login_counter=login_counter, info_cookies=info_cookies)
    )
    http_client = httpx.AsyncClient(transport=transport)
    state = State()

    try:
        async with sessionmaker_() as session:
            c1 = await resolve_qbittorrent(state, session, http_client)
        await c1.get_all_statuses()

        async with sessionmaker_() as session:
            c2 = await resolve_qbittorrent(state, session, http_client)
        await c2.get_all_statuses()
    finally:
        await http_client.aclose()

    assert c1 is c2
    assert login_counter[0] == 1
    # Both cycles authenticated via the reused instance's adapter-local session
    # cookie (post-#177: the SID lives on the adapter, not the shared jar).
    assert info_cookies == ["SID=test-session-id", "SID=test-session-id"]


async def test_settings_change_rebuilds_client(sessionmaker_: SessionMaker) -> None:
    await _seed_qbt_settings(sessionmaker_)
    login_counter = [0]
    transport = httpx.MockTransport(_counting_router(login_counter=login_counter))
    http_client = httpx.AsyncClient(transport=transport)
    state = State()

    try:
        async with sessionmaker_() as session:
            c1 = await resolve_qbittorrent(state, session, http_client)
        await c1.get_all_statuses()

        async with sessionmaker_() as session:
            await SettingsStore(session).set("qbittorrent_password", "new-pw")
            await session.commit()

        async with sessionmaker_() as session:
            c3 = await resolve_qbittorrent(state, session, http_client)
        await c3.get_all_statuses()
    finally:
        await http_client.aclose()

    assert c3 is not c1
    assert login_counter[0] == 2


async def test_unconfigured_raises_service_not_configured(sessionmaker_: SessionMaker) -> None:
    transport = httpx.MockTransport(_counting_router(login_counter=[0]))
    http_client = httpx.AsyncClient(transport=transport)
    state = State()

    try:
        async with sessionmaker_() as session:
            with pytest.raises(ServiceNotConfiguredError) as exc_info:
                await resolve_qbittorrent(state, session, http_client)
    finally:
        await http_client.aclose()

    assert exc_info.value.service == "qbittorrent"
    assert getattr(state, "qbittorrent_client", None) is None


async def test_lifespan_restart_rebuilds_client_bound_to_the_new_http_client(
    sessionmaker_: SessionMaker,
) -> None:
    """Codex P2 (concern 3): an ASGI lifespan shutdown/startup on the SAME app
    object (``state`` persists across the cycle) closes the old
    ``httpx.AsyncClient`` and installs a fresh one, but ``app.state.
    qbittorrent_client`` is a separate attribute that survives the swap
    untouched. Before this fix, the cache only compared settings, so it kept
    returning a ``QbittorrentClient`` still wrapping the CLOSED client and
    every qbt call failed until settings changed. The next resolve after a
    restart must rebuild against the NEW client -- and repeated restarts must
    each work cleanly, not just the first one."""
    await _seed_qbt_settings(sessionmaker_)
    login_counter = [0]
    state = State()

    # "Startup" generation 1.
    transport1 = httpx.MockTransport(_counting_router(login_counter=login_counter))
    http_client1 = httpx.AsyncClient(transport=transport1)
    async with sessionmaker_() as session:
        c1 = await resolve_qbittorrent(state, session, http_client1)
    await c1.get_all_statuses()
    # Reusing the SAME generation's client must still hit the cache (no new login).
    async with sessionmaker_() as session:
        c1_again = await resolve_qbittorrent(state, session, http_client1)
    assert c1_again is c1
    assert login_counter[0] == 1

    # "Shutdown": the lifespan closes the old client (mirrors ``web/app.py``'s
    # ``await app.state.http_client.aclose()``). ``state.qbittorrent_client`` is
    # untouched by this -- it is a SEPARATE attribute the old lifespan never
    # cleared.
    await http_client1.aclose()

    # "Startup" generation 2: a brand-new httpx client, same app.state object,
    # same qbt settings (unchanged).
    transport2 = httpx.MockTransport(_counting_router(login_counter=login_counter))
    http_client2 = httpx.AsyncClient(transport=transport2)
    try:
        async with sessionmaker_() as session:
            c2 = await resolve_qbittorrent(state, session, http_client2)
        # The rebuilt client must actually work against the NEW transport, not
        # raise against the closed old one.
        statuses = await c2.get_all_statuses()
        assert statuses == []
        assert c2 is not c1
        assert login_counter[0] == 2

        # A THIRD restart must also work cleanly (not just the first swap).
        transport3 = httpx.MockTransport(_counting_router(login_counter=login_counter))
        http_client3 = httpx.AsyncClient(transport=transport3)
        try:
            async with sessionmaker_() as session:
                c3 = await resolve_qbittorrent(state, session, http_client3)
            await c3.get_all_statuses()
            assert c3 is not c2
            assert login_counter[0] == 3

            # Reusing generation 3's client again still hits the cache.
            async with sessionmaker_() as session:
                c3_again = await resolve_qbittorrent(state, session, http_client3)
            assert c3_again is c3
            assert login_counter[0] == 3
        finally:
            await http_client3.aclose()
    finally:
        await http_client2.aclose()


async def test_403_triggers_single_relogin_on_reused_client(sessionmaker_: SessionMaker) -> None:
    await _seed_qbt_settings(sessionmaker_)
    login_counter = [0]
    transport = httpx.MockTransport(
        _counting_router(login_counter=login_counter, first_info_status=403)
    )
    http_client = httpx.AsyncClient(transport=transport)
    state = State()

    try:
        async with sessionmaker_() as session:
            c1 = await resolve_qbittorrent(state, session, http_client)
        statuses = await c1.get_all_statuses()

        async with sessionmaker_() as session:
            c2 = await resolve_qbittorrent(state, session, http_client)
    finally:
        await http_client.aclose()

    assert statuses == []
    # Initial login + one transparent re-login triggered by the 403.
    assert login_counter[0] == 2
    assert c2 is c1
