"""Keyset pagination of ``GET /api/v1/requests`` (issue #218 phase 1).

Covers the bounded page contract (limit/cursor/next_cursor), SQL-side
shared-user visibility, page-scoped season loads, the scale bounds the issue's
acceptance criteria name (query materialization, payload size), and rollout
compatibility (the legacy no-``limit`` mode keeps its pre-#218 folded behavior
plus an ignorable ``next_cursor: null``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    AuthSession,
    MediaRequest,
    MediaType,
    RequestStatus,
    RequestSubscriber,
    SeasonRequest,
    User,
)
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.web.deps import hash_session_token

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "pagination-key"
_HEADERS = {"X-Api-Key": _API_KEY}


async def _user_session(
    app: FastAPI, *, tag: str, permissions: int = 0
) -> tuple[int, dict[str, str], dict[str, str]]:
    token = f"{tag}-session-token"
    csrf = f"{tag}-csrf-token"
    async with app.state.sessionmaker() as session:
        user = User(username=f"{tag}-user", permissions=permissions)
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


async def _seed_movies(
    sessionmaker_: SessionMaker, count: int, *, tmdb_base: int, user_id: int | None = None
) -> list[int]:
    """Insert ``count`` movie request rows in one commit; returns ids ascending."""
    async with sessionmaker_() as session:
        rows = [
            MediaRequest(
                tmdb_id=tmdb_base + i,
                media_type=MediaType.movie,
                title=f"Movie {tmdb_base + i}",
                status=RequestStatus.pending,
                user_id=user_id,
            )
            for i in range(count)
        ]
        session.add_all(rows)
        await session.flush()
        ids = [r.id for r in rows]
        if user_id is not None:
            session.add_all(RequestSubscriber(request_id=rid, user_id=user_id) for rid in ids)
        await session.commit()
    return ids


async def test_page_orders_newest_first_and_pages_through_completely(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Keyset walk: pages are id-descending, disjoint, exhaustive, and the final
    page's ``next_cursor`` is null -- with no phantom empty page when the total is
    an exact multiple of the limit."""
    await seed(initialized=True, app_api_key=_API_KEY)
    ids = await _seed_movies(sessionmaker_, 7, tmdb_base=41000)

    seen: list[int] = []
    cursor: int | None = None
    pages = 0
    while True:
        params: dict[str, int] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get("/api/v1/requests", params=params, headers=_HEADERS)
        assert response.status_code == 200
        body = response.json()
        page_ids = [r["id"] for r in body["requests"]]
        assert page_ids == sorted(page_ids, reverse=True)  # newest first within the page
        seen.extend(page_ids)
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3  # 3 + 3 + 1
    assert seen == sorted(ids, reverse=True)  # disjoint + exhaustive + globally ordered

    # Exact-multiple boundary: 6 remaining rows below the newest id, limit 3 ->
    # the second page is the last (has-more probe finds nothing), never an empty
    # phantom page 3.
    response = await client.get(
        "/api/v1/requests", params={"limit": 6, "cursor": ids[-1]}, headers=_HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert [r["id"] for r in body["requests"]] == sorted(ids[:-1], reverse=True)
    assert body["next_cursor"] is None


async def test_page_past_the_end_is_empty_with_null_cursor(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    ids = await _seed_movies(sessionmaker_, 3, tmdb_base=42000)
    response = await client.get(
        "/api/v1/requests", params={"limit": 5, "cursor": min(ids)}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json() == {"requests": [], "next_cursor": None}


@pytest.mark.parametrize("params", [{"cursor": 10}, {"limit": 0}, {"limit": 201}])
async def test_page_parameter_validation(
    client: httpx.AsyncClient, seed: SeedFn, params: dict[str, int]
) -> None:
    """``cursor`` without ``limit`` is an explicit 422 with the ErrorDetail literal
    (never silently ignored); ``limit`` outside 1..200 is FastAPI's own validation
    422 with the standard array-detail shape -- both are documented (see the
    endpoint's 422 anyOf)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    response = await client.get("/api/v1/requests", params=params, headers=_HEADERS)
    assert response.status_code == 422
    detail = response.json()["detail"]
    if "limit" in params:
        assert isinstance(detail, list)  # FastAPI's standard validation shape
    else:
        assert detail == "cursor_requires_limit"  # this endpoint's own literal


async def test_openapi_422_documents_both_validation_and_cursor_literal(app: FastAPI) -> None:
    """#218 Codex round 1: a route-level 422 override REPLACES FastAPI's automatic
    validation entry, so the endpoint documents the union -- the framework's
    ``HTTPValidationError`` (limit/cursor validation, rejected before the handler)
    AND the ``ErrorDetail`` carrying ``cursor_requires_limit``."""
    schema = app.openapi()
    responses = schema["paths"]["/api/v1/requests"]["get"]["responses"]
    any_of = responses["422"]["content"]["application/json"]["schema"]["anyOf"]
    assert {entry["$ref"] for entry in any_of} == {
        "#/components/schemas/HTTPValidationError",
        "#/components/schemas/ErrorDetail",
    }
    # Both referenced components actually exist in the document.
    assert "HTTPValidationError" in schema["components"]["schemas"]
    assert "ErrorDetail" in schema["components"]["schemas"]


async def test_shared_user_page_visibility_is_enforced_in_sql(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared user's page contains ONLY their subscribed rows, and the paginated
    mode never touches the unbounded list paths -- the subscriber predicate rides
    in the page query itself, not a Python post-filter over a full scan."""
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id, cookies, headers = await _user_session(app, tag="shared")
    mine = await _seed_movies(sessionmaker_, 4, tmdb_base=43000, user_id=user_id)
    await _seed_movies(sessionmaker_, 6, tmdb_base=43100)  # foreign/unsubscribed rows

    async def full_scan_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("paginated mode must never materialize the full list")

    async def whole_set_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "paginated mode must never run the O(all-subscriptions) whole-set membership read"
        )

    monkeypatch.setattr(SqlRequestRepository, "list_by_status", full_scan_forbidden)
    monkeypatch.setattr(SqlRequestRepository, "list_for_user", full_scan_forbidden)
    monkeypatch.setattr(SqlRequestRepository, "list_subscribed_request_ids", whole_set_forbidden)

    seen: list[int] = []
    cursor: int | None = None
    while True:
        params: dict[str, int] = {"limit": 3}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(
            "/api/v1/requests", params=params, cookies=cookies, headers=headers
        )
        assert response.status_code == 200
        body = response.json()
        seen.extend(r["id"] for r in body["requests"])
        # A shared user's page rows are subscriber-filtered in the page SQL, so
        # membership derives from the page itself -- truthful can_withdraw with
        # no whole-set read (poisoned above) and no extra membership query.
        assert all(r["can_withdraw"] is True for r in body["requests"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(mine, reverse=True)  # every subscribed row, nothing foreign


async def test_admin_page_membership_is_scoped_to_the_page_ids(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin's paginated page derives ``can_withdraw`` from ONE membership read
    scoped to exactly the page's ids (the composite-index lookup), never the
    O(all-subscriptions) whole-set read the legacy mode uses (#218 Codex round 1)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    admin_id, cookies, headers = await _user_session(app, tag="admin", permissions=1)
    subscribed = await _seed_movies(sessionmaker_, 2, tmdb_base=49000, user_id=admin_id)
    unsubscribed = await _seed_movies(sessionmaker_, 4, tmdb_base=49100)

    async def whole_set_forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError(
            "paginated mode must never run the O(all-subscriptions) whole-set membership read"
        )

    real_among = SqlRequestRepository.subscribed_request_ids_among
    seen_batches: list[list[int]] = []

    async def spying_among(
        self: SqlRequestRepository, user_id: int, request_ids: list[int]
    ) -> set[int]:
        seen_batches.append(list(request_ids))
        return await real_among(self, user_id, request_ids)

    monkeypatch.setattr(SqlRequestRepository, "list_subscribed_request_ids", whole_set_forbidden)
    monkeypatch.setattr(SqlRequestRepository, "subscribed_request_ids_among", spying_among)

    response = await client.get(
        "/api/v1/requests", params={"limit": 5}, cookies=cookies, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    page_ids = [r["id"] for r in body["requests"]]
    # Admin scope: unfiltered page -- the newest 5 of the 6 rows, both populations.
    assert page_ids == sorted([*subscribed, *unsubscribed], reverse=True)[:5]
    assert seen_batches == [page_ids]  # ONE membership read, exactly the page ids
    flags = {r["id"]: r["can_withdraw"] for r in body["requests"]}
    for rid in page_ids:
        assert flags[rid] is (rid in set(subscribed))  # membership still truthful


async def test_page_bounds_row_materialization(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scale bound (query): with N seeded rows, a page request materializes at most
    ``limit + 1`` request records (the has-more probe row) -- never O(N)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_movies(sessionmaker_, 60, tmdb_base=44000)

    calls = {"n": 0}

    import plex_manager.repositories.requests as repo_module

    real_to_record = getattr(repo_module, "_to_record")  # noqa: B009 - private by name, spied deliberately

    def counting_to_record(row: MediaRequest) -> object:
        calls["n"] += 1
        return real_to_record(row)

    monkeypatch.setattr(repo_module, "_to_record", counting_to_record)

    response = await client.get("/api/v1/requests", params={"limit": 10}, headers=_HEADERS)
    assert response.status_code == 200
    assert len(response.json()["requests"]) == 10
    assert calls["n"] <= 11  # limit + the single has-more probe row


async def test_page_payload_is_bounded_and_a_fraction_of_the_full_list(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Scale bound (payload): the page byte size tracks the LIMIT, not the lifetime
    history size."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _seed_movies(sessionmaker_, 200, tmdb_base=45000)

    page = await client.get("/api/v1/requests", params={"limit": 20}, headers=_HEADERS)
    full = await client.get("/api/v1/requests", headers=_HEADERS)
    assert page.status_code == 200 and full.status_code == 200
    assert len(page.json()["requests"]) == 20
    assert len(full.json()["requests"]) == 200
    assert len(page.content) < 64_000  # absolute bound for a 20-row page
    assert len(page.content) * 4 < len(full.content)  # and a small fraction of the full list


async def test_page_scopes_season_loads_to_the_page_ids(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seasons are batched for exactly the page's TV ids -- never for the whole
    lifetime history (the issue's page-scoped season-load criterion)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        shows = [
            MediaRequest(
                tmdb_id=46000 + i,
                media_type=MediaType.tv,
                title=f"Show {i}",
                status=RequestStatus.pending,
            )
            for i in range(8)
        ]
        session.add_all(shows)
        await session.flush()
        show_ids = [s.id for s in shows]
        session.add_all(
            SeasonRequest(media_request_id=sid, season_number=1, status=RequestStatus.pending)
            for sid in show_ids
        )
        await session.commit()

    real_list_for_requests = SqlSeasonRequestRepository.list_for_requests
    seen_batches: list[list[int]] = []

    async def spying_list_for_requests(
        self: SqlSeasonRequestRepository, media_request_ids: list[int]
    ) -> object:
        seen_batches.append(list(media_request_ids))
        return await real_list_for_requests(self, media_request_ids)

    monkeypatch.setattr(SqlSeasonRequestRepository, "list_for_requests", spying_list_for_requests)

    response = await client.get("/api/v1/requests", params={"limit": 3}, headers=_HEADERS)
    assert response.status_code == 200
    page_ids = [r["id"] for r in response.json()["requests"]]
    assert len(page_ids) == 3
    assert seen_batches == [page_ids]  # exactly the page's tv ids, nothing more


async def test_legacy_mode_is_unchanged_and_carries_a_null_cursor(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """Rollout compatibility: a client that never sends ``limit`` (a cached
    pre-#218 SPA bundle) gets the whole folded list exactly as before, plus an
    ignorable ``next_cursor: null``."""
    await seed(initialized=True, app_api_key=_API_KEY)
    ids = await _seed_movies(sessionmaker_, 5, tmdb_base=47000)
    response = await client.get("/api/v1/requests", headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert [r["id"] for r in body["requests"]] == ids  # legacy id-ASCENDING whole list
    assert body["next_cursor"] is None


async def test_page_rows_are_raw_history_not_folded(
    client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """A history page deliberately shows RAW lifetime rows: a settled row and its
    later active re-request both appear (the display fold's active-else-newest
    representative can span pages; that collapse is the legacy mode's -- and
    phase 2's -- job, documented in the endpoint contract)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        settled = MediaRequest(
            tmdb_id=48000,
            media_type=MediaType.movie,
            title="Movie",
            status=RequestStatus.cancelled,
        )
        active = MediaRequest(
            tmdb_id=48000,
            media_type=MediaType.movie,
            title="Movie",
            status=RequestStatus.pending,
        )
        session.add_all([settled, active])
        await session.flush()
        pair = {settled.id, active.id}
        await session.commit()

    response = await client.get("/api/v1/requests", params={"limit": 10}, headers=_HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert {r["id"] for r in body["requests"]} >= pair  # both rows, unfolded

    legacy = await client.get("/api/v1/requests", headers=_HEADERS)
    legacy_ids = {r["id"] for r in legacy.json()["requests"]}
    assert len(legacy_ids & pair) == 1  # the legacy mode still folds the pair
