"""Discovery — TMDB search surfaced through the service + auth enforcement."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import PlexLibraryError
from plex_manager.config import get_settings
from plex_manager.models import AuthSession, User
from plex_manager.ports.metadata import (
    MediaSearchResult,
    RecommendationFacet,
    RecommendationProfile,
)
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
    title: str = "Seeded",
    is_anime: bool = False,
) -> None:
    """Insert one MediaRequest so a Discover tile can pick up its request-derived state."""
    async with sessionmaker_() as session:
        await SqlRequestRepository(session).create(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=title,
            status=status,
            user_id=user_id,
            is_anime=is_anime,
        )
        await session.commit()


async def _make_user(app: FastAPI, *, plex_id: int, username: str, permissions: int = 0) -> int:
    """Insert one shared (non-admin) user and return its id."""
    async with app.state.sessionmaker() as session:
        user = User(plex_id=plex_id, username=username, permissions=permissions)
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


async def test_home_composes_rows_and_returns_ordered_plural_spotlights(
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
        MediaSearchResult(
            tmdb_id=2,
            media_type="movie",
            title="Backdrop Two",
            backdrop_url="http://img/b.jpg",
        ),
        MediaSearchResult(
            tmdb_id=3,
            media_type="movie",
            title="Backdrop Three",
            backdrop_url="http://img/c.jpg",
        ),
    ]
    popular = [MediaSearchResult(tmdb_id=20, media_type="movie", title="No Backdrop")]
    shows = [
        MediaSearchResult(
            tmdb_id=11,
            media_type="tv",
            title="Show One",
            backdrop_url="http://img/show-a.jpg",
        ),
        MediaSearchResult(
            tmdb_id=12,
            media_type="tv",
            title="Show Two",
            backdrop_url="http://img/show-b.jpg",
        ),
        MediaSearchResult(
            tmdb_id=13,
            media_type="tv",
            title="Show Three",
            backdrop_url="http://img/show-c.jpg",
        ),
    ]
    library = FakeLibrary(available={2}, available_tv_seasons={12: frozenset({1})})
    override_adapters(
        app,
        tmdb=FakeTmdb(
            trending=trending,
            popular=popular,
            upcoming=[],
            trending_tv_results=shows,
            popular_tv_results=[shows[0]],  # duplicate row appearance collapses in hero only
        ),
        library=library,
    )

    response = await client.get("/api/v1/discover/home", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert [(item["media_type"], item["tmdb_id"]) for item in body["spotlights"]] == [
        ("movie", 1),
        ("tv", 11),
        ("movie", 2),
        ("tv", 12),
        ("movie", 3),
        ("tv", 13),
    ]
    states = {item["tmdb_id"]: item["library_state"] for item in body["spotlights"]}
    assert states[2] == "available"
    assert states[12] == "available"
    assert [row["row_type"] for row in body["rows"]] == [
        "trending",
        "trending_tv",
        "popular_tv",
        "popular",
        "upcoming",
    ]
    assert body["rows"][0]["items"][0]["backdrop_url"] == "http://img/a.jpg"
    assert [item["tmdb_id"] for item in body["rows"][0]["items"]] == [1, 2, 3]
    assert [item["tmdb_id"] for item in body["rows"][1]["items"]] == [11, 12, 13]
    assert [item["tmdb_id"] for item in body["rows"][2]["items"]] == [11]
    assert body["rows"][4]["items"] == []  # upcoming was empty — an honest empty row
    assert library.present_ids_calls == 1


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
    trending = [
        MediaSearchResult(
            tmdb_id=31,
            media_type="movie",
            title="Foreign Active",
            backdrop_url="https://image/foreign.jpg",
        )
    ]
    override_adapters(app, tmdb=FakeTmdb(trending=trending, popular=[], upcoming=[]))

    response = await client.get("/api/v1/discover/home", cookies=cookies)
    assert response.status_code == 200
    body = response.json()
    trending_row = next(r for r in body["rows"] if r["row_type"] == "trending")
    assert trending_row["items"][0]["library_state"] == "none"  # no leak
    assert body["spotlights"][0]["library_state"] == "none"  # no leak in hero either


# --------------------------------------------------------------------------- #
# Personalized home rows (issue #191)                                        #
# --------------------------------------------------------------------------- #
_LOAD_ID = "00000000-0000-0000-0000-000000000001"


def _personalized_tmdb(
    seed_id: int, recommendation_id: int, *, extra_seed_id: int | None = None
) -> FakeTmdb:
    profiles: dict[tuple[int, Literal["movie", "tv"]], RecommendationProfile] = {
        (seed_id, "movie"): RecommendationProfile(
            facets=(RecommendationFacet(metric="genre", value_id=27, label="Horror"),)
        )
    }
    if extra_seed_id is not None:
        profiles[(extra_seed_id, "movie")] = RecommendationProfile(
            facets=(RecommendationFacet(metric="genre", value_id=28, label="Action"),)
        )
    return FakeTmdb(
        trending=[],
        popular=[],
        upcoming=[],
        trending_tv_results=[],
        popular_tv_results=[],
        recommendation_profiles=profiles,
        recommendations={
            # >= discovery_service._MIN_SHELF_TITLES (issue #277): a single-item
            # page would now be filtered out as a too-thin shelf, so pad with two
            # extra distinct titles behind the id every caller asserts on at [0].
            ("movie", "genre", 27): [
                MediaSearchResult(
                    tmdb_id=recommendation_id,
                    media_type="movie",
                    title="Recommended",
                ),
                MediaSearchResult(
                    tmdb_id=recommendation_id + 1000,
                    media_type="movie",
                    title="Recommended 2",
                ),
                MediaSearchResult(
                    tmdb_id=recommendation_id + 2000,
                    media_type="movie",
                    title="Recommended 3",
                ),
            ]
        },
    )


async def test_shared_session_personalization_uses_only_own_history(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    shared_id = await _make_user(app, plex_id=9101, username="personal-shared")
    neighbor_id = await _make_user(app, plex_id=9102, username="personal-neighbor")
    cookies = await _shared_session_cookies(
        app,
        user_id=shared_id,
        token="personal-shared-token",  # noqa: S106
    )
    await _seed_request(
        sessionmaker_,
        tmdb_id=101,
        media_type="movie",
        status="pending",
        user_id=shared_id,
        title="Own Seed",
    )
    await _seed_request(
        sessionmaker_,
        tmdb_id=102,
        media_type="movie",
        status="pending",
        user_id=neighbor_id,
        title="Neighbor Secret",
    )
    tmdb = _personalized_tmdb(101, 901, extra_seed_id=102)
    # A profile for the foreign row makes a privacy regression observable: a
    # global-history fallback would call it and expose its title in copy.
    override_adapters(app, tmdb=tmdb, library=FakeLibrary(available={901}))

    response = await client.get(
        "/api/v1/discover/home", params={"load_id": _LOAD_ID}, cookies=cookies
    )

    assert response.status_code == 200
    body = response.json()
    personalized = [row for row in body["rows"] if row["row_type"].startswith("personalized:")]
    assert len(personalized) == 1
    assert personalized[0]["row_type"] == "personalized:genre:movie:101"
    assert personalized[0]["title"] == "Because you requested Own Seed"
    assert personalized[0]["subtitle"] == "more horror"
    assert personalized[0]["items"][0]["library_state"] == "available"
    assert "Neighbor Secret" not in response.text
    assert tmdb.recommendation_profile_calls == [(101, "movie")]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Cookie, X-Api-Key"


async def test_plex_admin_personalization_is_still_scoped_to_the_admins_own_rows(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    admin_id = await _make_user(app, plex_id=9201, username="personal-admin", permissions=1)
    neighbor_id = await _make_user(app, plex_id=9202, username="admin-neighbor")
    cookies = await _shared_session_cookies(
        app,
        user_id=admin_id,
        token="personal-admin-token",  # noqa: S106
    )
    await _seed_request(
        sessionmaker_,
        tmdb_id=201,
        media_type="movie",
        status="available",
        user_id=admin_id,
        title="Admin Seed",
    )
    await _seed_request(
        sessionmaker_,
        tmdb_id=202,
        media_type="movie",
        status="pending",
        user_id=neighbor_id,
        title="Other User Secret",
    )
    tmdb = _personalized_tmdb(201, 902)
    override_adapters(app, tmdb=tmdb)

    first = await client.get("/api/v1/discover/home", params={"load_id": _LOAD_ID}, cookies=cookies)
    second = await client.get(
        "/api/v1/discover/home", params={"load_id": _LOAD_ID}, cookies=cookies
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert "Because you requested Admin Seed" in first.text
    assert "Other User Secret" not in first.text
    assert tmdb.recommendation_profile_calls == [(201, "movie"), (201, "movie")]


async def test_home_load_id_validation_and_omitted_id_backward_compatibility(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id = await _make_user(app, plex_id=9301, username="omitted-load")
    cookies = await _shared_session_cookies(
        app,
        user_id=user_id,
        token="omitted-load-token",  # noqa: S106
    )
    await _seed_request(
        sessionmaker_,
        tmdb_id=301,
        media_type="movie",
        status="pending",
        user_id=user_id,
        title="Must Stay Standard",
    )
    tmdb = _personalized_tmdb(301, 903)
    override_adapters(app, tmdb=tmdb)

    invalid = await client.get(
        "/api/v1/discover/home", params={"load_id": "not-a-uuid"}, cookies=cookies
    )
    omitted = await client.get("/api/v1/discover/home", cookies=cookies)

    assert invalid.status_code == 422
    assert omitted.status_code == 200
    assert all(not row["row_type"].startswith("personalized:") for row in omitted.json()["rows"])
    assert tmdb.recommendation_profile_calls == []
    assert "cache-control" not in omitted.headers


@pytest.mark.parametrize("auth_mode", ["api_key", "dev_bypass"])
async def test_identityless_credentials_never_personalize(
    auth_mode: str,
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    tmdb = _personalized_tmdb(401, 904)
    override_adapters(app, tmdb=tmdb)
    headers: dict[str, str] = _HEADERS
    if auth_mode == "dev_bypass":
        monkeypatch.setenv("PLEX_MANAGER_DEV_AUTH_BYPASS", "true")
        get_settings.cache_clear()
        headers = {}

    response = await client.get(
        "/api/v1/discover/home", params={"load_id": _LOAD_ID}, headers=headers
    )

    assert response.status_code == 200
    assert all(not row["row_type"].startswith("personalized:") for row in response.json()["rows"])
    assert tmdb.recommendation_profile_calls == []
    assert "cache-control" not in response.headers


async def test_no_history_omits_personalized_placeholders_but_keeps_response_private(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id = await _make_user(app, plex_id=9401, username="empty-history")
    cookies = await _shared_session_cookies(
        app,
        user_id=user_id,
        token="empty-history-token",  # noqa: S106
    )
    tmdb = FakeTmdb(trending=[], popular=[], upcoming=[])
    override_adapters(app, tmdb=tmdb)

    response = await client.get(
        "/api/v1/discover/home", params={"load_id": _LOAD_ID}, cookies=cookies
    )

    assert response.status_code == 200
    assert len(response.json()["rows"]) == 5
    assert response.headers["cache-control"] == "private, no-store"
