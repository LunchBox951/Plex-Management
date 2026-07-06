"""Discovery — TMDB search surfaced through the service + auth enforcement."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import PlexLibraryError
from plex_manager.models import AuthSession, User
from plex_manager.ports.metadata import MediaSearchResult
from plex_manager.repositories import SqlRequestRepository
from plex_manager.web.deps import hash_session_token
from tests.web.fakes import FakeLibrary, FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "discover-key"
_HEADERS = {"X-Api-Key": _API_KEY}


async def _seed_request(
    sessionmaker_: SessionMaker,
    *,
    tmdb_id: int,
    media_type: str,
    status: str,
    user_id: int | None = None,
) -> None:
    """Insert one MediaRequest so a Discover tile can pick up its request-derived state."""
    async with sessionmaker_() as session:
        await SqlRequestRepository(session).create(
            tmdb_id=tmdb_id, media_type=media_type, title="Seeded", status=status, user_id=user_id
        )
        await session.commit()


async def _make_user(app: FastAPI, *, plex_id: int, username: str) -> int:
    """Insert one shared (non-admin) user and return its id."""
    async with app.state.sessionmaker() as session:
        user = User(plex_id=plex_id, username=username, permissions=0)
        session.add(user)
        await session.commit()
        return user.id


async def _shared_session_cookies(app: FastAPI, *, user_id: int, token: str) -> dict[str, str]:
    """Mint a live browser session for ``user_id``; GETs need only the cookie."""
    async with app.state.sessionmaker() as session:
        session.add(
            AuthSession(
                user_id=user_id,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return {"plexmgr.session": token}


async def test_search_returns_results(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    tmdb = FakeTmdb(
        results=[
            MediaSearchResult(tmdb_id=603, media_type="movie", title="The Matrix", year=1999),
        ]
    )
    override_adapters(app, tmdb=tmdb)

    response = await client.get(
        "/api/v1/discover/search", params={"query": "matrix"}, headers=_HEADERS
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results == [
        {
            "tmdb_id": 603,
            "media_type": "movie",
            "title": "The Matrix",
            "year": 1999,
            "overview": None,
            "poster_url": None,
            "backdrop_url": None,
            # Plex not configured + no request row -> an honest "none" (no fake presence).
            "library_state": "none",
        }
    ]


async def test_discovery_requires_api_key(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/discover/search", params={"query": "matrix"})
    assert response.status_code == 401


async def test_home_composes_rows_and_picks_a_spotlight(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    trending = [
        MediaSearchResult(
            tmdb_id=1,
            media_type="movie",
            title="Backdrop One",
            year=2024,
            backdrop_url="http://img/a.jpg",
        ),
    ]
    popular = [MediaSearchResult(tmdb_id=2, media_type="movie", title="Popular Two", year=2023)]
    override_adapters(app, tmdb=FakeTmdb(trending=trending, popular=popular, upcoming=[]))

    response = await client.get("/api/v1/discover/home", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    # The first item with a backdrop becomes the spotlight.
    assert body["spotlight"]["tmdb_id"] == 1
    assert [row["row_type"] for row in body["rows"]] == [
        "trending",
        "popular",
        "upcoming",
        "trending_tv",
        "popular_tv",
    ]
    assert body["rows"][0]["items"][0]["backdrop_url"] == "http://img/a.jpg"
    assert body["rows"][2]["items"] == []  # upcoming was empty — an honest empty row


async def test_category_returns_a_paginated_list(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    trending = [MediaSearchResult(tmdb_id=5, media_type="movie", title="Trending", year=2024)]
    override_adapters(app, tmdb=FakeTmdb(trending=trending))

    response = await client.get("/api/v1/discover/trending", params={"page": 1}, headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 1
    assert body["results"][0]["tmdb_id"] == 5


async def test_unknown_category_is_422(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=FakeTmdb())
    response = await client.get("/api/v1/discover/nonsense", headers=_HEADERS)
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# library-state tile decoration (issue #29)
# --------------------------------------------------------------------------- #
async def test_search_marks_a_movie_present_in_plex_as_available(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # Owned in Plex but never requested through the app: only the server presence bit
    # can flag it (no MediaRequest row) -- the beta's dominant "I already have this" case.
    await seed(initialized=True, app_api_key=_API_KEY)
    tmdb = FakeTmdb(
        results=[MediaSearchResult(tmdb_id=603, media_type="movie", title="The Matrix")]
    )
    override_adapters(app, tmdb=tmdb, library=FakeLibrary(available={603}))

    response = await client.get(
        "/api/v1/discover/search", params={"query": "matrix"}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["library_state"] == "available"


async def test_search_reflects_request_status(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_request(sessionmaker_, tmdb_id=1, media_type="movie", status="pending")
    await _seed_request(sessionmaker_, tmdb_id=2, media_type="movie", status="downloading")
    tmdb = FakeTmdb(
        results=[
            MediaSearchResult(tmdb_id=1, media_type="movie", title="Pending"),
            MediaSearchResult(tmdb_id=2, media_type="movie", title="Downloading"),
        ]
    )
    override_adapters(app, tmdb=tmdb, library=FakeLibrary())

    response = await client.get("/api/v1/discover/search", params={"query": "x"}, headers=_HEADERS)
    assert response.status_code == 200
    states = {r["tmdb_id"]: r["library_state"] for r in response.json()["results"]}
    assert states == {1: "requested", 2: "processing"}


async def test_search_without_plex_configured_still_decorates_requests(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    # No library override -> get_library_optional resolves to None (Plex unconfigured).
    # Presence degrades to empty, but request-derived badges still render honestly.
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_request(sessionmaker_, tmdb_id=1, media_type="movie", status="pending")
    tmdb = FakeTmdb(
        results=[
            MediaSearchResult(tmdb_id=1, media_type="movie", title="Requested"),
            MediaSearchResult(tmdb_id=2, media_type="movie", title="Unknown"),
        ]
    )
    override_adapters(app, tmdb=tmdb)  # deliberately NO library

    response = await client.get("/api/v1/discover/search", params={"query": "x"}, headers=_HEADERS)
    assert response.status_code == 200
    states = {r["tmdb_id"]: r["library_state"] for r in response.json()["results"]}
    assert states == {1: "requested", 2: "none"}


async def test_search_degrades_honestly_when_plex_errors(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # A Plex failure must NEVER 500 Discover and NEVER fabricate a "not present": the
    # endpoint returns 200 with "none" (no badge), the honest degrade.
    await seed(initialized=True, app_api_key=_API_KEY)
    tmdb = FakeTmdb(
        results=[MediaSearchResult(tmdb_id=603, media_type="movie", title="The Matrix")]
    )
    override_adapters(app, tmdb=tmdb, library=FakeLibrary(raises=PlexLibraryError("plex down")))

    response = await client.get(
        "/api/v1/discover/search", params={"query": "matrix"}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["library_state"] == "none"


async def test_search_tv_show_present_and_partial_request(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    # Show 10 is in Plex but never requested -> presence flags it "available".
    # Show 20 carries a partially_available parent rollup -> "partially_available".
    await _seed_request(sessionmaker_, tmdb_id=20, media_type="tv", status="partially_available")
    tmdb = FakeTmdb(
        results=[
            MediaSearchResult(tmdb_id=10, media_type="tv", title="Owned Show"),
            MediaSearchResult(tmdb_id=20, media_type="tv", title="Partial Show"),
        ]
    )
    override_adapters(
        app,
        tmdb=tmdb,
        library=FakeLibrary(available_tv_seasons={10: frozenset({1})}),
    )

    response = await client.get(
        "/api/v1/discover/search", params={"query": "show"}, headers=_HEADERS
    )
    assert response.status_code == 200
    states = {r["tmdb_id"]: r["library_state"] for r in response.json()["results"]}
    assert states == {10: "available", 20: "partially_available"}


async def test_search_with_no_results_short_circuits_state_resolution(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # An honest zero-result page must short-circuit before any presence crawl: even a Plex
    # that would raise never gets consulted, so the empty page returns 200, not a 500.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app,
        tmdb=FakeTmdb(results=[]),
        library=FakeLibrary(raises=PlexLibraryError("must not be called on an empty page")),
    )

    response = await client.get(
        "/api/v1/discover/search", params={"query": "nothingmatches"}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["results"] == []


# --------------------------------------------------------------------------- #
# Shared-session visibility scoping (issue #58) — request-derived states are   #
# per-user for non-admins; Plex PRESENCE stays global (physical reality any    #
# account already sees in Plex itself, not private request activity).          #
# --------------------------------------------------------------------------- #
async def test_search_scopes_request_badges_to_the_shared_users_own_rows(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    neighbor_id = await _make_user(app, plex_id=9001, username="neighbor")
    shared_id = await _make_user(app, plex_id=9002, username="shared")
    cookies = await _shared_session_cookies(app, user_id=shared_id, token="disc-shared")  # noqa: S106
    # tmdb 1: the NEIGHBOR's pending request; tmdb 2: the shared user's OWN;
    # tmdb 3: in Plex only (no request row); tmdb 4: nothing anywhere.
    await _seed_request(
        sessionmaker_, tmdb_id=1, media_type="movie", status="pending", user_id=neighbor_id
    )
    await _seed_request(
        sessionmaker_, tmdb_id=2, media_type="movie", status="pending", user_id=shared_id
    )
    tmdb = FakeTmdb(
        results=[
            MediaSearchResult(tmdb_id=1, media_type="movie", title="Neighbor's"),
            MediaSearchResult(tmdb_id=2, media_type="movie", title="Own"),
            MediaSearchResult(tmdb_id=3, media_type="movie", title="In Plex"),
            MediaSearchResult(tmdb_id=4, media_type="movie", title="Nothing"),
        ]
    )
    override_adapters(app, tmdb=tmdb, library=FakeLibrary(available={3}))

    response = await client.get("/api/v1/discover/search", params={"query": "x"}, cookies=cookies)
    assert response.status_code == 200
    states = {r["tmdb_id"]: r["library_state"] for r in response.json()["results"]}
    # The neighbor's request activity does NOT leak (1 -> none); the shared user's
    # own request decorates (2); Plex presence stays global (3); nothing is nothing.
    assert states == {1: "none", 2: "requested", 3: "available", 4: "none"}


async def test_search_request_badges_remain_unscoped_for_admins(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    neighbor_id = await _make_user(app, plex_id=9003, username="neighbor-adm")
    await _seed_request(
        sessionmaker_, tmdb_id=1, media_type="movie", status="pending", user_id=neighbor_id
    )
    await _seed_request(sessionmaker_, tmdb_id=2, media_type="movie", status="downloading")
    tmdb = FakeTmdb(
        results=[
            MediaSearchResult(tmdb_id=1, media_type="movie", title="Owned"),
            MediaSearchResult(tmdb_id=2, media_type="movie", title="Ownerless"),
        ]
    )
    override_adapters(app, tmdb=tmdb, library=FakeLibrary())

    # API-key auth is an admin context: EVERY row decorates, exactly as before.
    response = await client.get("/api/v1/discover/search", params={"query": "x"}, headers=_HEADERS)
    assert response.status_code == 200
    states = {r["tmdb_id"]: r["library_state"] for r in response.json()["results"]}
    assert states == {1: "requested", 2: "processing"}


async def test_home_scopes_request_badges_for_shared_sessions_too(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    # Home shares _resolve_states with search: a foreign request must not decorate
    # a shared user's home tiles either.
    await seed(initialized=True, app_api_key=_API_KEY)
    neighbor_id = await _make_user(app, plex_id=9004, username="neighbor-home")
    shared_id = await _make_user(app, plex_id=9005, username="shared-home")
    cookies = await _shared_session_cookies(app, user_id=shared_id, token="disc-home")  # noqa: S106
    await _seed_request(
        sessionmaker_, tmdb_id=31, media_type="movie", status="downloading", user_id=neighbor_id
    )
    trending = [MediaSearchResult(tmdb_id=31, media_type="movie", title="Foreign Active")]
    override_adapters(app, tmdb=FakeTmdb(trending=trending, popular=[], upcoming=[]))

    response = await client.get("/api/v1/discover/home", cookies=cookies)
    assert response.status_code == 200
    trending_row = next(r for r in response.json()["rows"] if r["row_type"] == "trending")
    assert trending_row["items"][0]["library_state"] == "none"  # no leak
