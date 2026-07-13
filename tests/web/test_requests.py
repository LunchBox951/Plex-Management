"""Requests — create resolves TMDB detail and dedups; list + get."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from plex_manager.models import AuthSession, Download, MediaRequest, MediaType, RequestStatus, User
from plex_manager.ports.metadata import MovieMetadata, TvMetadata
from plex_manager.ports.repositories import DownloadRecord, SeasonRequestRecord
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.web.deps import hash_session_token
from tests.web.fakes import FakeLibrary, FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "requests-key"
_HEADERS = {"X-Api-Key": _API_KEY}

_SHOW_ID = 900


def _tmdb() -> FakeTmdb:
    return FakeTmdb(
        movies={
            603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999, is_anime=False),
        },
        shows={
            _SHOW_ID: TvMetadata(tmdb_id=_SHOW_ID, title="Some Show", year=2020, season_count=2),
        },
    )


async def _shared_user_cookies(
    app: FastAPI, *, tag: str = "shared"
) -> tuple[dict[str, str], dict[str, str]]:
    token = f"{tag}-session-token"
    csrf = f"{tag}-csrf-token"
    async with app.state.sessionmaker() as session:
        user = User(
            plex_id=5001 if tag == "shared" else None,
            username=f"{tag}-user",
            permissions=0,
        )
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return {"plexmgr.session": token, "plexmgr.csrf": csrf}, {"X-CSRF-Token": csrf}


async def test_create_resolves_detail_and_lists(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "The Matrix"
    assert body["year"] == 1999
    assert body["status"] == "pending"

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    assert len(listed.json()["requests"]) == 1


async def test_create_dedups_active_request(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    first = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    second = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert len(listed.json()["requests"]) == 1


async def test_shared_user_requests_are_limited_to_their_own_records(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app,
        tmdb=FakeTmdb(
            movies={
                603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999),
                604: MovieMetadata(tmdb_id=604, title="Dark City", year=1998),
            }
        ),
    )
    shared_cookies, shared_headers = await _shared_user_cookies(app)

    own = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie"},
        cookies=shared_cookies,
        headers=shared_headers,
    )
    assert own.status_code == 201
    assert own.json()["can_mutate"] is True

    admin = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 604, "media_type": "movie"},
        headers=_HEADERS,
    )
    assert admin.status_code == 201

    listed = await client.get("/api/v1/requests", cookies=shared_cookies)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["requests"]] == [own.json()["id"]]
    assert listed.json()["requests"][0]["can_mutate"] is True

    hidden = await client.get(f"/api/v1/requests/{admin.json()['id']}", cookies=shared_cookies)
    assert hidden.status_code == 404

    pin = await client.post(
        f"/api/v1/requests/{own.json()['id']}/keep-forever",
        json={"keep_forever": True},
        cookies=shared_cookies,
        headers=shared_headers,
    )
    assert pin.status_code == 200
    assert pin.json()["keep_forever"] is True
    assert pin.json()["can_mutate"] is True


@pytest.mark.parametrize(
    ("suffix", "json_body"),
    [
        ("keep-forever", {"keep_forever": True}),
        ("report-issue", {"reason": "bad_quality"}),
        ("cancel", None),
    ],
)
async def test_subscriber_cannot_mutate_another_users_request(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    suffix: str,
    json_body: dict[str, object] | None,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())
    owner_cookies, owner_headers = await _shared_user_cookies(app, tag=f"owner-{suffix}")
    subscriber_cookies, subscriber_headers = await _shared_user_cookies(
        app, tag=f"subscriber-{suffix}"
    )
    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie"},
        cookies=owner_cookies,
        headers=owner_headers,
    )
    assert created.status_code == 201
    subscribed = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie"},
        cookies=subscriber_cookies,
        headers=subscriber_headers,
    )
    assert subscribed.status_code == 200
    assert subscribed.json()["can_mutate"] is False

    response = await client.post(
        f"/api/v1/requests/{created.json()['id']}/{suffix}",
        json=json_body,
        cookies=subscriber_cookies,
        headers=subscriber_headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "request_not_found"


async def _user_with_cookies(
    app: FastAPI, *, tag: str
) -> tuple[int, dict[str, str], dict[str, str]]:
    """Create a non-admin user + session, returning ``(user_id, cookies, headers)``."""
    token = f"{tag}-session-token"
    csrf = f"{tag}-csrf-token"
    async with app.state.sessionmaker() as session:
        user = User(plex_id=None, username=f"{tag}-user", permissions=0)
        session.add(user)
        await session.flush()
        user_id = user.id
        session.add(
            AuthSession(
                user_id=user_id,
                token_hash=hash_session_token(token),
                expires_at=datetime.now(UTC) + timedelta(days=1),
                last_seen_at=datetime.now(UTC),
            )
        )
        await session.commit()
    return user_id, {"plexmgr.session": token, "plexmgr.csrf": csrf}, {"X-CSRF-Token": csrf}


async def test_keep_forever_by_non_admin_does_not_touch_another_users_row(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """A non-admin's keep-forever toggle is scoped to their OWN rows.

    Keep-forever is title-wide (``set_keep_forever_for_title`` sweeps every row
    sharing ``(tmdb_id, media_type)``), and a title can carry rows owned by
    different users -- e.g. user A's older SETTLED request and user B's newer
    ACTIVE one. Before this scoping, user B flipping their own row silently
    pinned/unpinned user A's row too, changing A's eviction protection without
    consent. A non-admin must only ever move their own rows.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())
    user_a, _, _ = await _user_with_cookies(app, tag="owner-a")
    user_b, cookies_b, headers_b = await _user_with_cookies(app, tag="owner-b")

    async with app.state.sessionmaker() as session:
        settled = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="The Matrix",
            status=RequestStatus.available,
            user_id=user_a,
        )
        active = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="The Matrix",
            status=RequestStatus.pending,
            user_id=user_b,
        )
        session.add_all([settled, active])
        await session.commit()
        settled_id, active_id = settled.id, active.id

    pinned = await client.post(
        f"/api/v1/requests/{active_id}/keep-forever",
        json={"keep_forever": True},
        cookies=cookies_b,
        headers=headers_b,
    )
    assert pinned.status_code == 200
    assert pinned.json()["keep_forever"] is True

    async with app.state.sessionmaker() as session:
        a_row = await session.get(MediaRequest, settled_id)
        b_row = await session.get(MediaRequest, active_id)
    assert a_row is not None and b_row is not None
    assert b_row.keep_forever is True  # the caller's own row moved
    assert a_row.keep_forever is False  # another user's row is untouched


async def test_keep_forever_by_admin_sweeps_the_whole_title(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Admins retain the unrestricted, whole-title keep-forever sweep."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())
    user_a, _, _ = await _user_with_cookies(app, tag="admin-sweep-a")
    user_b, _, _ = await _user_with_cookies(app, tag="admin-sweep-b")

    async with app.state.sessionmaker() as session:
        settled = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="The Matrix",
            status=RequestStatus.available,
            user_id=user_a,
        )
        active = MediaRequest(
            tmdb_id=603,
            media_type=MediaType.movie,
            title="The Matrix",
            status=RequestStatus.pending,
            user_id=user_b,
        )
        session.add_all([settled, active])
        await session.commit()
        settled_id, active_id = settled.id, active.id

    pinned = await client.post(
        f"/api/v1/requests/{active_id}/keep-forever",
        json={"keep_forever": True},
        headers=_HEADERS,
    )
    assert pinned.status_code == 200

    async with app.state.sessionmaker() as session:
        a_row = await session.get(MediaRequest, settled_id)
        b_row = await session.get(MediaRequest, active_id)
    assert a_row is not None and b_row is not None
    assert a_row.keep_forever is True
    assert b_row.keep_forever is True


async def test_shared_user_request_progress_is_filtered_with_their_records(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app,
        tmdb=FakeTmdb(
            movies={
                603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999),
                604: MovieMetadata(tmdb_id=604, title="Dark City", year=1998),
            }
        ),
    )
    shared_cookies, shared_headers = await _shared_user_cookies(app)

    own = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie"},
        cookies=shared_cookies,
        headers=shared_headers,
    )
    hidden = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 604, "media_type": "movie"},
        headers=_HEADERS,
    )
    assert own.status_code == hidden.status_code == 201

    async with app.state.sessionmaker() as session:
        own_row = await session.get(MediaRequest, own.json()["id"])
        hidden_row = await session.get(MediaRequest, hidden.json()["id"])
        assert own_row is not None and hidden_row is not None
        own_row.status = RequestStatus.downloading
        hidden_row.status = RequestStatus.downloading
        session.add_all(
            [
                Download(
                    torrent_hash="shared-own-progress",
                    status="downloading",
                    media_request_id=own_row.id,
                    media_type=MediaType.movie,
                    progress=0.42,
                ),
                Download(
                    torrent_hash="admin-hidden-progress",
                    status="downloading",
                    media_request_id=hidden_row.id,
                    media_type=MediaType.movie,
                    progress=0.91,
                ),
            ]
        )
        await session.commit()

    listed = await client.get("/api/v1/requests", cookies=shared_cookies)
    assert listed.status_code == 200
    assert listed.json()["requests"] == [
        {
            **own.json(),
            "status": "downloading",
            "download_progress": 0.42,
        }
    ]


async def test_shared_user_dedup_subscribes_to_unowned_request_without_claiming_authority(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())
    shared_cookies, shared_headers = await _shared_user_cookies(app)

    # The api-key automation path creates a request with NO user identity.
    admin = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert admin.status_code == 201

    # A shared user requesting the same title dedups onto that ownerless request.
    shared = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie"},
        cookies=shared_cookies,
        headers=shared_headers,
    )
    assert shared.status_code == 200
    assert shared.json()["id"] == admin.json()["id"]
    assert shared.json()["can_mutate"] is False

    # It is now adopted by the requester, so it shows up in THEIR filtered list
    # rather than succeeding yet vanishing behind the per-user filter.
    listed = await client.get("/api/v1/requests", cookies=shared_cookies)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["requests"]] == [admin.json()["id"]]
    assert listed.json()["requests"][0]["can_mutate"] is False


@pytest.mark.parametrize(
    ("suffix", "json_body"),
    [
        ("keep-forever", {"keep_forever": True}),
        ("report-issue", {"reason": "bad_quality"}),
        ("cancel", None),
    ],
)
async def test_ownerless_automation_subscriber_cannot_mutate_request(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    suffix: str,
    json_body: dict[str, object] | None,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())
    automation = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie"},
        headers=_HEADERS,
    )
    cookies, headers = await _shared_user_cookies(app, tag=f"ownerless-{suffix}")
    subscribed = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie"},
        cookies=cookies,
        headers=headers,
    )
    assert subscribed.status_code == 200
    assert subscribed.json()["can_mutate"] is False

    response = await client.post(
        f"/api/v1/requests/{automation.json()['id']}/{suffix}",
        json=json_body,
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "request_not_found"


async def test_create_records_already_in_plex_as_available(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # A movie already in Plex is recorded directly as `available` (poster art
    # persisted), short-circuiting search/grab — never a wasted request.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb(), library=FakeLibrary(available={603}))

    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    assert created.json()["status"] == "available"


async def test_create_proceeds_when_not_in_plex(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb(), library=FakeLibrary(available=set()))

    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    assert created.json()["status"] == "pending"


async def test_create_force_reacquire_returns_pending_201(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # Re-acquire (issue #131): `force=True` bypasses the already-in-library
    # short-circuit even though Plex still reports the movie present, proving the
    # endpoint threads `force` through to `create_request_result`. Contrast with the
    # SAME library state minus `force` (a different tmdb id), which still
    # short-circuits to `available`.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app,
        tmdb=FakeTmdb(
            movies={
                603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999),
                604: MovieMetadata(tmdb_id=604, title="Dark City", year=1998),
            }
        ),
        library=FakeLibrary(available={603, 604}),
    )

    forced = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie", "force": True},
        headers=_HEADERS,
    )
    assert forced.status_code == 201
    assert forced.json()["status"] == "pending"

    not_forced = await client.post(
        "/api/v1/requests", json={"tmdb_id": 604, "media_type": "movie"}, headers=_HEADERS
    )
    assert not_forced.status_code == 201
    assert not_forced.json()["status"] == "available"


async def test_create_force_reacquire_allowed_for_shared_user(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # Same authZ bar as any create (`require_api_key`): a non-admin shared user can
    # force-reacquire too -- this is NOT an admin-gated verb.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb(), library=FakeLibrary(available={603}))
    shared_cookies, shared_headers = await _shared_user_cookies(app)

    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": 603, "media_type": "movie", "force": True},
        cookies=shared_cookies,
        headers=shared_headers,
    )
    assert created.status_code == 201
    assert created.json()["status"] == "pending"


async def test_create_unknown_media_is_404(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=FakeTmdb())
    response = await client.post(
        "/api/v1/requests", json={"tmdb_id": 999, "media_type": "movie"}, headers=_HEADERS
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "media_not_found"


async def test_create_tv_request_with_no_aired_seasons_waits(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=FakeTmdb(shows={44: TvMetadata(tmdb_id=44, title="Show")}))

    response = await client.post(
        "/api/v1/requests", json={"tmdb_id": 44, "media_type": "tv"}, headers=_HEADERS
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "waiting_for_air_date"
    assert body["tv_request_mode"] == "whole_show"

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    listed_body = listed.json()["requests"]
    assert len(listed_body) == 1
    assert listed_body[0]["status"] == "waiting_for_air_date"


def test_create_contract_documents_manual_error_bodies(app: FastAPI) -> None:
    responses = app.openapi()["paths"]["/api/v1/requests"]["post"]["responses"]

    assert responses["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorDetail"
    )
    assert "409" not in responses


def test_get_request_contract_documents_not_found(app: FastAPI) -> None:
    responses = app.openapi()["paths"]["/api/v1/requests/{request_id}"]["get"]["responses"]

    assert responses["404"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorDetail"
    )


async def test_get_missing_request_is_404(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/requests/12345", headers=_HEADERS)
    assert response.status_code == 404


async def test_movie_request_seasons_is_none(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # Movies have no SeasonRequest rows -- ``seasons`` is always None, never [].
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())
    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.json()["seasons"] is None

    fetched = await client.get(f"/api/v1/requests/{created.json()['id']}", headers=_HEADERS)
    assert fetched.json()["seasons"] is None


async def test_create_tv_request_with_no_seasons_tracks_the_whole_aired_series(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # Omitted `seasons` = whole aired series: every season 1..season_count.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": _SHOW_ID, "media_type": "tv"}, headers=_HEADERS
    )
    assert created.status_code == 201
    body = created.json()
    assert body["media_type"] == "tv"
    assert sorted(s["season_number"] for s in body["seasons"]) == [1, 2]
    assert all(s["status"] == "pending" for s in body["seasons"])


async def test_create_tv_request_with_explicit_seasons_tracks_only_those(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    created = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": _SHOW_ID, "media_type": "tv", "seasons": [1]},
        headers=_HEADERS,
    )
    assert created.status_code == 201
    assert [s["season_number"] for s in created.json()["seasons"]] == [1]


async def test_second_post_with_a_new_season_grows_the_tracked_set(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # A repeat POST for the same show naming a NEW season GROWS the tracked set
    # rather than being dropped by the request-level (tmdb_id, media_type) dedup.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_tmdb())

    first = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": _SHOW_ID, "media_type": "tv", "seasons": [1]},
        headers=_HEADERS,
    )
    assert first.status_code == 201
    request_id = first.json()["id"]
    assert [s["season_number"] for s in first.json()["seasons"]] == [1]

    second = await client.post(
        "/api/v1/requests",
        json={"tmdb_id": _SHOW_ID, "media_type": "tv", "seasons": [1, 2]},
        headers=_HEADERS,
    )
    assert second.status_code == 200
    assert second.json()["id"] == request_id  # the SAME request row, dedup'd
    assert sorted(s["season_number"] for s in second.json()["seasons"]) == [1, 2]

    # The list endpoint reflects the grown set too.
    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert len(listed.json()["requests"]) == 1
    listed_seasons = listed.json()["requests"][0]["seasons"]
    assert sorted(s["season_number"] for s in listed_seasons) == [1, 2]


async def test_list_requests_batches_season_rows_not_one_query_per_tv_row(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tv shows + a movie on the list endpoint must issue exactly ONE
    ``list_for_requests`` batch call and ZERO per-row ``list_for_request`` calls --
    proving the N+1 the blueprint calls out is actually avoided, not just that the
    result happens to look right."""
    await seed(initialized=True, app_api_key=_API_KEY)
    tmdb = FakeTmdb(
        movies={603: MovieMetadata(tmdb_id=603, title="The Matrix", year=1999)},
        shows={
            900: TvMetadata(tmdb_id=900, title="Show One", year=2020, season_count=1),
            901: TvMetadata(tmdb_id=901, title="Show Two", year=2021, season_count=1),
        },
    )
    override_adapters(app, tmdb=tmdb)

    await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    await client.post(
        "/api/v1/requests", json={"tmdb_id": 900, "media_type": "tv"}, headers=_HEADERS
    )
    await client.post(
        "/api/v1/requests", json={"tmdb_id": 901, "media_type": "tv"}, headers=_HEADERS
    )

    batch_calls = {"n": 0}
    per_row_calls = {"n": 0}
    download_batch_calls = {"n": 0}
    real_batch = SqlSeasonRequestRepository.list_for_requests
    real_per_row = SqlSeasonRequestRepository.list_for_request
    real_download_batch = SqlDownloadRepository.list_active_for_requests

    async def counting_batch(
        self: SqlSeasonRequestRepository, media_request_ids: list[int]
    ) -> dict[int, list[SeasonRequestRecord]]:
        batch_calls["n"] += 1
        return await real_batch(self, media_request_ids)

    async def counting_per_row(
        self: SqlSeasonRequestRepository, media_request_id: int
    ) -> list[SeasonRequestRecord]:
        per_row_calls["n"] += 1
        return await real_per_row(self, media_request_id)

    async def counting_download_batch(
        self: SqlDownloadRepository, media_request_ids: list[int]
    ) -> dict[int, list[DownloadRecord]]:
        download_batch_calls["n"] += 1
        return await real_download_batch(self, media_request_ids)

    monkeypatch.setattr(SqlSeasonRequestRepository, "list_for_requests", counting_batch)
    monkeypatch.setattr(SqlSeasonRequestRepository, "list_for_request", counting_per_row)
    monkeypatch.setattr(SqlDownloadRepository, "list_active_for_requests", counting_download_batch)

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    assert len(listed.json()["requests"]) == 3

    assert batch_calls["n"] == 1
    assert per_row_calls["n"] == 0
    assert download_batch_calls["n"] == 1


async def test_request_download_progress_is_truthful_or_null(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Known zero/42% survive, while missing, ambiguous, non-downloading, and
    non-transferring (client-missing / metadata-fetching / import-pending)
    projections remain null rather than fabricating an aggregate, a default, a
    stale value, or a live-looking bar for a phase that isn't a transfer — and
    one live transfer beside a non-transferring sibling still projects its own
    honest number."""
    await seed(initialized=True, app_api_key=_API_KEY)

    async with app.state.sessionmaker() as session:
        known_zero = MediaRequest(
            tmdb_id=610,
            media_type=MediaType.movie,
            title="Known Zero",
            status=RequestStatus.downloading,
        )
        known_42 = MediaRequest(
            tmdb_id=611,
            media_type=MediaType.movie,
            title="Known Forty Two",
            status=RequestStatus.downloading,
        )
        missing = MediaRequest(
            tmdb_id=612,
            media_type=MediaType.movie,
            title="Missing Download",
            status=RequestStatus.downloading,
        )
        ambiguous_tv = MediaRequest(
            tmdb_id=613,
            media_type=MediaType.tv,
            title="Two Seasons",
            status=RequestStatus.downloading,
        )
        not_downloading = MediaRequest(
            tmdb_id=614,
            media_type=MediaType.movie,
            title="Still Searching",
            status=RequestStatus.searching,
        )
        vanished = MediaRequest(
            tmdb_id=615,
            media_type=MediaType.movie,
            title="Vanished From Client",
            status=RequestStatus.downloading,
        )
        one_live_one_missing = MediaRequest(
            tmdb_id=616,
            media_type=MediaType.tv,
            title="One Live One Missing",
            status=RequestStatus.downloading,
        )
        fetching_metadata = MediaRequest(
            tmdb_id=617,
            media_type=MediaType.movie,
            title="Fetching Metadata",
            status=RequestStatus.downloading,
        )
        awaiting_import = MediaRequest(
            tmdb_id=618,
            media_type=MediaType.movie,
            title="Awaiting Import",
            status=RequestStatus.downloading,
        )
        session.add_all(
            [
                known_zero,
                known_42,
                missing,
                ambiguous_tv,
                not_downloading,
                vanished,
                one_live_one_missing,
                fetching_metadata,
                awaiting_import,
            ]
        )
        await session.flush()
        session.add_all(
            [
                Download(
                    torrent_hash="known-zero",
                    status="downloading",
                    media_request_id=known_zero.id,
                    media_type=MediaType.movie,
                    progress=0.0,
                ),
                Download(
                    torrent_hash="known-42",
                    status="downloading",
                    media_request_id=known_42.id,
                    media_type=MediaType.movie,
                    progress=0.42,
                ),
                Download(
                    torrent_hash="tv-s1",
                    status="downloading",
                    media_request_id=ambiguous_tv.id,
                    media_type=MediaType.tv,
                    season=1,
                    progress=0.25,
                ),
                Download(
                    torrent_hash="tv-s2",
                    status="downloading",
                    media_request_id=ambiguous_tv.id,
                    media_type=MediaType.tv,
                    season=2,
                    progress=0.75,
                ),
                Download(
                    torrent_hash="searching-row",
                    status="downloading",
                    media_request_id=not_downloading.id,
                    media_type=MediaType.movie,
                    progress=0.8,
                ),
                # Non-terminal but NOT live: the torrent vanished from qBittorrent
                # and the reconciler parked the row in ``client_missing`` while the
                # request rides out the grace window still reading ``downloading``.
                # Its frozen last-known 97% must not be presented as live progress.
                Download(
                    torrent_hash="vanished-row",
                    status="client_missing",
                    media_request_id=vanished.id,
                    media_type=MediaType.movie,
                    progress=0.97,
                ),
                # A client-missing sibling does not make the ONE genuinely live
                # transfer ambiguous: its live value is still the only honest number.
                Download(
                    torrent_hash="olom-live",
                    status="downloading",
                    media_request_id=one_live_one_missing.id,
                    media_type=MediaType.tv,
                    season=1,
                    progress=0.33,
                ),
                Download(
                    torrent_hash="olom-missing",
                    status="client_missing",
                    media_request_id=one_live_one_missing.id,
                    media_type=MediaType.tv,
                    season=2,
                    progress=0.66,
                ),
                # Non-terminal but NOT transferring: no payload transfer exists yet
                # (metadata) / the transfer is already over (import wait). Queue
                # shows these as honest state labels with no progress bar; the
                # Requests projection must not turn them into a live-looking one.
                Download(
                    torrent_hash="metadata-row",
                    status="metadata_fetching",
                    media_request_id=fetching_metadata.id,
                    media_type=MediaType.movie,
                    progress=0.0,
                ),
                Download(
                    torrent_hash="import-pending-row",
                    status="import_pending",
                    media_request_id=awaiting_import.id,
                    media_type=MediaType.movie,
                    progress=1.0,
                ),
            ]
        )
        await session.commit()

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    by_title = {item["title"]: item["download_progress"] for item in listed.json()["requests"]}
    assert by_title == {
        "Known Zero": 0.0,
        "Known Forty Two": 0.42,
        "Missing Download": None,
        "Two Seasons": None,
        "Still Searching": None,
        "Vanished From Client": None,
        "One Live One Missing": 0.33,
        "Fetching Metadata": None,
        "Awaiting Import": None,
    }

    # Single-record responses use the same one-request projection semantics.
    single = await client.get(f"/api/v1/requests/{known_42.id}", headers=_HEADERS)
    assert single.status_code == 200
    assert single.json()["download_progress"] == 0.42


# --- Change 1: fold duplicate rows in the requests list ------------------------
# (request-dedup healing, spec-request-dedup-healing.md)


async def _insert_request(
    app: FastAPI,
    *,
    tmdb_id: int,
    status: RequestStatus,
    media_type: MediaType = MediaType.movie,
    user_id: int | None = None,
    library_path: str | None = None,
) -> int:
    async with app.state.sessionmaker() as session:
        row = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=f"Movie {tmdb_id}",
            status=status,
            user_id=user_id,
            library_path=library_path,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _insert_user(app: FastAPI, *, username: str) -> int:
    async with app.state.sessionmaker() as session:
        user = User(username=username, permissions=0)
        session.add(user)
        await session.commit()
        return user.id


async def test_list_folds_duplicate_movie_rows_prefers_active(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Two rows for the same (tmdb, movie), same (ownerless) owner -- one
    'available', one 'pending' -- fold to ONE row: the non-settled 'pending'."""
    await seed(initialized=True, app_api_key=_API_KEY)
    available_id = await _insert_request(app, tmdb_id=603, status=RequestStatus.available)
    pending_id = await _insert_request(app, tmdb_id=603, status=RequestStatus.pending)

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    items = listed.json()["requests"]
    assert [item["id"] for item in items] == [pending_id]
    assert items[0]["status"] == "pending"
    assert available_id != pending_id  # sanity: two distinct rows really exist


async def test_list_folds_prefers_newest_when_all_settled(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Two SETTLED rows for the same media (available + failed) -- fold to the
    newest (highest id) row."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _insert_request(app, tmdb_id=603, status=RequestStatus.available)
    newest_id = await _insert_request(app, tmdb_id=603, status=RequestStatus.failed)

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    items = listed.json()["requests"]
    assert [item["id"] for item in items] == [newest_id]
    assert items[0]["status"] == "failed"


async def test_list_does_not_fold_distinct_users(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """Two rows for the same media, owned by DIFFERENT users -- the admin
    (unfiltered) view must show BOTH; folding across owners would silently hide
    one user's request from the admin (an honesty regression)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    user_a = await _insert_user(app, username="user-a")
    user_b = await _insert_user(app, username="user-b")
    # 'available' (SETTLED) rows are excluded from the active-dedup partial
    # unique index, so two rows for the SAME (tmdb_id, media_type) can coexist
    # here regardless of owner -- exactly the legitimate remove-then-reacquire /
    # per-owner shape this test targets.
    id_a = await _insert_request(app, tmdb_id=603, status=RequestStatus.available, user_id=user_a)
    id_b = await _insert_request(app, tmdb_id=603, status=RequestStatus.available, user_id=user_b)

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    assert {item["id"] for item in listed.json()["requests"]} == {id_a, id_b}


async def test_subscriber_scoped_list_folds_cross_owner_duplicates(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    viewer_cookies, _viewer_headers = await _shared_user_cookies(app, tag="fold-viewer")
    user_a = await _insert_user(app, username="fold-owner-a")
    user_b = await _insert_user(app, username="fold-owner-b")
    older_id = await _insert_request(
        app, tmdb_id=603, status=RequestStatus.available, user_id=user_a
    )
    newer_id = await _insert_request(app, tmdb_id=603, status=RequestStatus.failed, user_id=user_b)
    async with app.state.sessionmaker() as session:
        viewer_id = (
            await session.execute(select(User.id).where(User.username == "fold-viewer-user"))
        ).scalar_one()
        repo = SqlRequestRepository(session)
        await repo.add_subscriber(older_id, viewer_id)
        await repo.add_subscriber(newer_id, viewer_id)
        await session.commit()

    listed = await client.get("/api/v1/requests", cookies=viewer_cookies)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["requests"]] == [newer_id]
    assert listed.json()["requests"][0]["can_mutate"] is False


async def test_list_leaves_underlying_rows_untouched(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """The fold is display-only: after listing, each underlying row is still
    individually GET-able (never deleted or mutated by the fold)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    available_id = await _insert_request(app, tmdb_id=603, status=RequestStatus.available)
    pending_id = await _insert_request(app, tmdb_id=603, status=RequestStatus.pending)

    listed = await client.get("/api/v1/requests", headers=_HEADERS)
    assert listed.status_code == 200
    assert len(listed.json()["requests"]) == 1  # folded for display

    for request_id, expected_status in (
        (available_id, "available"),
        (pending_id, "pending"),
    ):
        got = await client.get(f"/api/v1/requests/{request_id}", headers=_HEADERS)
        assert got.status_code == 200
        assert got.json()["status"] == expected_status
