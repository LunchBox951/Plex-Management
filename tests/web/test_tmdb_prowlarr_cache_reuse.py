"""``resolve_tmdb`` / ``resolve_prowlarr`` session-reuse tests (issue #214).

Before this fix, ``get_tmdb`` / ``get_prowlarr`` built a BRAND NEW
``TmdbMetadata`` / ``ProwlarrIndexer`` on every call -- including on every
router request AND on every auto-grab tick -- discarding each adapter's
instance-local TTL cache (TMDB: one-hour movie/TV/search/page caches;
Prowlarr: five-minute indexer-priority cache) between calls. This meant a
title just resolved moments earlier was re-fetched from the live service on
the very next call, entirely defeating caching that only works if the SAME
adapter instance survives across requests/ticks.

Mirrors ``tests/web/test_qbittorrent_session_reuse.py``'s pattern exactly:
prove instance identity is reused (and therefore the adapter's own cache
actually prevents a second upstream call) across resolves sharing the same
effective settings + ``httpx.AsyncClient``, and that a settings change or an
ASGI-lifespan client swap correctly invalidates the cache and rebuilds.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import State

from plex_manager.domain.release import IndexerSearchRequest
from plex_manager.web.deps import (
    ServiceNotConfiguredError,
    SettingsStore,
    resolve_prowlarr,
    resolve_tmdb,
)

SessionMaker = async_sessionmaker[AsyncSession]

_TMDB_API_KEY = "tmdb-key-never-logged"
_PROWLARR_URL = "http://prowlarr.local:9696"
_PROWLARR_API_KEY = "prowlarr-key-never-logged"
_TMDB_ID = 27205


async def _seed_tmdb_settings(sessionmaker_: SessionMaker, *, api_key: str = _TMDB_API_KEY) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set("tmdb_api_key", api_key)
        await session.commit()


async def _seed_prowlarr_settings(
    sessionmaker_: SessionMaker, *, api_key: str = _PROWLARR_API_KEY
) -> None:
    async with sessionmaker_() as session:
        store = SettingsStore(session)
        await store.set("prowlarr_url", _PROWLARR_URL)
        await store.set("prowlarr_api_key", api_key)
        await session.commit()


def _movie_router(call_counter: list[int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/3/movie/{_TMDB_ID}"
        call_counter[0] += 1
        return httpx.Response(
            200,
            json={"id": _TMDB_ID, "title": "Inception", "release_date": "2010-07-16"},
        )

    return httpx.MockTransport(handler)


async def test_tmdb_instance_reused_across_calls_so_the_movie_cache_actually_hits(
    sessionmaker_: SessionMaker,
) -> None:
    await _seed_tmdb_settings(sessionmaker_)
    call_counter = [0]
    http_client = httpx.AsyncClient(transport=_movie_router(call_counter))
    state = State()

    try:
        async with sessionmaker_() as session:
            tmdb1 = await resolve_tmdb(state, session, http_client)
        movie1 = await tmdb1.get_movie(_TMDB_ID)

        async with sessionmaker_() as session:
            tmdb2 = await resolve_tmdb(state, session, http_client)
        movie2 = await tmdb2.get_movie(_TMDB_ID)
    finally:
        await http_client.aclose()

    assert tmdb1 is tmdb2
    assert movie1 is not None
    assert movie1 == movie2
    # The second resolve reused the SAME instance, so its instance-local
    # one-hour movie cache served the second lookup with NO second HTTP call.
    assert call_counter[0] == 1


async def test_tmdb_settings_change_rebuilds_client(sessionmaker_: SessionMaker) -> None:
    await _seed_tmdb_settings(sessionmaker_)
    call_counter = [0]
    http_client = httpx.AsyncClient(transport=_movie_router(call_counter))
    state = State()

    try:
        async with sessionmaker_() as session:
            tmdb1 = await resolve_tmdb(state, session, http_client)
        await tmdb1.get_movie(_TMDB_ID)

        async with sessionmaker_() as session:
            await SettingsStore(session).set("tmdb_api_key", "new-key")
            await session.commit()

        async with sessionmaker_() as session:
            tmdb2 = await resolve_tmdb(state, session, http_client)
        await tmdb2.get_movie(_TMDB_ID)
    finally:
        await http_client.aclose()

    assert tmdb2 is not tmdb1
    # A brand-new instance has an EMPTY movie cache, so the second lookup made
    # a genuine second HTTP call -- proving the rebuild, not a stale hit.
    assert call_counter[0] == 2


async def test_tmdb_lifespan_restart_rebuilds_client_bound_to_the_new_http_client(
    sessionmaker_: SessionMaker,
) -> None:
    await _seed_tmdb_settings(sessionmaker_)
    state = State()
    call_counter1 = [0]
    http_client1 = httpx.AsyncClient(transport=_movie_router(call_counter1))

    async with sessionmaker_() as session:
        tmdb1 = await resolve_tmdb(state, session, http_client1)
    await tmdb1.get_movie(_TMDB_ID)
    await http_client1.aclose()

    call_counter2 = [0]
    http_client2 = httpx.AsyncClient(transport=_movie_router(call_counter2))
    try:
        async with sessionmaker_() as session:
            tmdb2 = await resolve_tmdb(state, session, http_client2)
        movie = await tmdb2.get_movie(_TMDB_ID)
    finally:
        await http_client2.aclose()

    assert tmdb2 is not tmdb1
    assert movie is not None
    # Rebuilt against the NEW (open) client rather than the closed old one.
    assert call_counter2[0] == 1


async def test_tmdb_unconfigured_raises_service_not_configured(sessionmaker_: SessionMaker) -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))
    state = State()

    try:
        async with sessionmaker_() as session:
            with pytest.raises(ServiceNotConfiguredError) as exc_info:
                await resolve_tmdb(state, session, http_client)
    finally:
        await http_client.aclose()

    assert exc_info.value.service == "tmdb"
    assert getattr(state, "tmdb_client", None) is None


_INDEXERS: list[dict[str, object]] = [
    {"id": 1, "name": "Indexer A", "priority": 10, "enable": True, "protocol": "torrent"},
]


def _prowlarr_router(indexer_calls: list[int]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/indexer":
            indexer_calls[0] += 1
            return httpx.Response(200, json=_INDEXERS)
        assert request.url.path == "/api/v1/search"
        return httpx.Response(200, json=[])

    return httpx.MockTransport(handler)


async def test_prowlarr_instance_reused_across_calls_so_the_priority_cache_actually_hits(
    sessionmaker_: SessionMaker,
) -> None:
    await _seed_prowlarr_settings(sessionmaker_)
    indexer_calls = [0]
    http_client = httpx.AsyncClient(transport=_prowlarr_router(indexer_calls))
    state = State()

    try:
        async with sessionmaker_() as session:
            prowlarr1 = await resolve_prowlarr(state, session, http_client)
        await prowlarr1.search(IndexerSearchRequest(query="x"))

        async with sessionmaker_() as session:
            prowlarr2 = await resolve_prowlarr(state, session, http_client)
        await prowlarr2.search(IndexerSearchRequest(query="x"))
    finally:
        await http_client.aclose()

    assert prowlarr1 is prowlarr2
    # The second resolve reused the SAME instance, so its instance-local
    # five-minute indexer-priority cache served the second search with NO
    # second ``/api/v1/indexer`` call.
    assert indexer_calls[0] == 1


async def test_prowlarr_settings_change_rebuilds_client(sessionmaker_: SessionMaker) -> None:
    await _seed_prowlarr_settings(sessionmaker_)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))
    state = State()

    try:
        async with sessionmaker_() as session:
            prowlarr1 = await resolve_prowlarr(state, session, http_client)

        async with sessionmaker_() as session:
            await SettingsStore(session).set("prowlarr_api_key", "new-key")
            await session.commit()

        async with sessionmaker_() as session:
            prowlarr2 = await resolve_prowlarr(state, session, http_client)
    finally:
        await http_client.aclose()

    assert prowlarr2 is not prowlarr1


async def test_prowlarr_unconfigured_raises_service_not_configured(
    sessionmaker_: SessionMaker,
) -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(404)))
    state = State()

    try:
        async with sessionmaker_() as session:
            with pytest.raises(ServiceNotConfiguredError) as exc_info:
                await resolve_prowlarr(state, session, http_client)
    finally:
        await http_client.aclose()

    assert exc_info.value.service == "prowlarr"
    assert getattr(state, "prowlarr_client", None) is None
