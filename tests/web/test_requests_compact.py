"""Compact (folded) live-state view + title-scoped rows (issue #370 phase 2).

Three surfaces pinned here:

* ``POST /api/v1/requests/live-state`` -- the folded live-state per tile key,
  the client tile-overlay's freshness poll.
* ``GET /api/v1/requests/by-title`` -- every visible raw row for one title,
  the title-detail modal's full match list.
* The Stage-A legacy retirement (``list_subscribed_request_ids`` swapped for
  the page-scoped ``subscribed_request_ids_among``): byte-identical
  ``can_withdraw``/``has_other_participants`` before/after.

The core #370 guarantee -- "a fold representative can span raw keyset pages,
but the compact query never does" -- is pinned directly against the
repository (never by walking ``GET /requests`` pages).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    AuthSession,
    MediaRequest,
    MediaType,
    RequestStatus,
    RequestSubscriber,
    User,
)
from plex_manager.repositories import requests as requests_repo_module
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.web.deps import hash_session_token

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "compact-key"
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


async def _add_request(
    sessionmaker_: SessionMaker,
    *,
    tmdb_id: int,
    media_type: MediaType = MediaType.movie,
    status: RequestStatus = RequestStatus.pending,
    user_id: int | None = None,
    title: str = "Some Title",
) -> int:
    async with sessionmaker_() as session:
        row = MediaRequest(
            tmdb_id=tmdb_id, media_type=media_type, title=title, status=status, user_id=user_id
        )
        session.add(row)
        await session.flush()
        row_id = row.id
        await session.commit()
    return row_id


# --------------------------------------------------------------------------- #
# Repository-level: the fold-across-page-boundaries safety pin
# --------------------------------------------------------------------------- #


async def test_compact_state_folds_representative_spanning_raw_pages(
    sessionmaker_: SessionMaker,
) -> None:
    """The active representative sits at a LOW id; 250 unrelated settled rows are
    inserted after it (any raw keyset page with limit < 250 would put the newest
    filler rows on page 1 and only reach this row many pages later); a settled
    row for the SAME title is then inserted at a much HIGHER id still. The
    compact query -- keyed by tmdb_id, never a keyset window -- must return the
    active representative regardless: this is the literal "a fold
    representative can span keyset pages" guarantee from issue #370."""
    active_id = await _add_request(
        sessionmaker_, tmdb_id=90000, status=RequestStatus.pending, title="Spans Pages"
    )
    for i in range(250):
        await _add_request(sessionmaker_, tmdb_id=91000 + i, status=RequestStatus.available)
    settled_id = await _add_request(
        sessionmaker_, tmdb_id=90000, status=RequestStatus.available, title="Spans Pages"
    )
    assert settled_id > active_id + 250  # the group genuinely spans a wide id range

    async with sessionmaker_() as session:
        result = await SqlRequestRepository(session).compact_states_by_tmdb_ids([(90000, "movie")])
    state = result[(90000, "movie")]
    assert state.status == "pending"
    assert state.request_id == active_id  # the ACTIVE row wins, never the newest settled one


async def test_compact_state_movie_coexisting_available(sessionmaker_: SessionMaker) -> None:
    """A settled ``available`` movie row alongside an active re-request sets
    ``has_coexisting_available``; without the settled sibling it is ``False``;
    tv never sets it even with an analogous shape."""
    async with sessionmaker_() as session:
        result = await SqlRequestRepository(session).compact_states_by_tmdb_ids(
            [(1, "movie"), (2, "movie"), (3, "tv")]
        )
    assert result == {}  # nothing seeded yet -- sanity on the empty-DB case

    await _add_request(sessionmaker_, tmdb_id=1, status=RequestStatus.available)
    await _add_request(sessionmaker_, tmdb_id=1, status=RequestStatus.pending)  # re-request
    await _add_request(sessionmaker_, tmdb_id=2, status=RequestStatus.pending)  # no sibling
    await _add_request(
        sessionmaker_, tmdb_id=3, media_type=MediaType.tv, status=RequestStatus.available
    )
    await _add_request(
        sessionmaker_, tmdb_id=3, media_type=MediaType.tv, status=RequestStatus.pending
    )

    async with sessionmaker_() as session:
        result = await SqlRequestRepository(session).compact_states_by_tmdb_ids(
            [(1, "movie"), (2, "movie"), (3, "tv")]
        )
    assert result[(1, "movie")].has_coexisting_available is True
    assert result[(1, "movie")].status == "pending"
    assert result[(2, "movie")].has_coexisting_available is False
    assert result[(3, "tv")].has_coexisting_available is False  # never set for tv


async def test_compact_state_visibility_scoped(sessionmaker_: SessionMaker) -> None:
    """A shared user's compact lookup sees only their own rows for a title; an
    unscoped (admin/API-key) lookup sees every row."""
    async with sessionmaker_() as session:
        user = User(username="scoped-user", permissions=0)
        session.add(user)
        await session.flush()
        user_id = user.id
        mine = MediaRequest(
            tmdb_id=55, media_type=MediaType.movie, title="Mine", status=RequestStatus.pending
        )
        theirs = MediaRequest(
            tmdb_id=55, media_type=MediaType.movie, title="Theirs", status=RequestStatus.available
        )
        session.add_all([mine, theirs])
        await session.flush()
        session.add(RequestSubscriber(request_id=mine.id, user_id=user_id))
        await session.commit()
        mine_id = mine.id

    async with sessionmaker_() as session:
        scoped = await SqlRequestRepository(session).compact_states_by_tmdb_ids(
            [(55, "movie")], for_user_id=user_id
        )
        unscoped = await SqlRequestRepository(session).compact_states_by_tmdb_ids([(55, "movie")])
    assert scoped[(55, "movie")].request_id == mine_id
    assert scoped[(55, "movie")].status == "pending"
    # Unscoped sees BOTH rows; the settled one is newer, but the active `mine`
    # row still wins the fold (active-else-newest), same representative.
    assert unscoped[(55, "movie")].request_id == mine_id


async def test_compact_state_visibility_scoped_coexisting_available(
    sessionmaker_: SessionMaker,
) -> None:
    """Issue #402: the movie presence-contradiction bit must be evaluated over
    the CALLER's own subscribed rows only. A shared user's own settled
    ``available`` row alongside their own active re-request sets the flag
    (driven by user 1's own rows); a sibling ``available`` row that belongs to
    a DIFFERENT user -- invisible to this scope's JOIN -- must never leak into
    it, proven by user 2 (who has only their own settled row, no active
    sibling of their own) seeing the flag as ``False``."""
    async with sessionmaker_() as session:
        user1 = User(username="scoped-coexist-1", permissions=0)
        user2 = User(username="scoped-coexist-2", permissions=0)
        session.add_all([user1, user2])
        await session.flush()
        user1_id, user2_id = user1.id, user2.id

        mine_available = MediaRequest(
            tmdb_id=66, media_type=MediaType.movie, title="Mine", status=RequestStatus.available
        )
        mine_pending = MediaRequest(
            tmdb_id=66, media_type=MediaType.movie, title="Mine", status=RequestStatus.pending
        )
        theirs_available = MediaRequest(
            tmdb_id=66, media_type=MediaType.movie, title="Theirs", status=RequestStatus.available
        )
        session.add_all([mine_available, mine_pending, theirs_available])
        await session.flush()
        session.add_all(
            [
                RequestSubscriber(request_id=mine_available.id, user_id=user1_id),
                RequestSubscriber(request_id=mine_pending.id, user_id=user1_id),
                RequestSubscriber(request_id=theirs_available.id, user_id=user2_id),
            ]
        )
        await session.commit()
        mine_pending_id = mine_pending.id

    async with sessionmaker_() as session:
        scoped_user1 = await SqlRequestRepository(session).compact_states_by_tmdb_ids(
            [(66, "movie")], for_user_id=user1_id
        )
        scoped_user2 = await SqlRequestRepository(session).compact_states_by_tmdb_ids(
            [(66, "movie")], for_user_id=user2_id
        )
    # User 1: own settled `available` row + own active re-request -> the flag
    # is set, and the pending re-request wins the fold as the representative.
    assert scoped_user1[(66, "movie")].request_id == mine_pending_id
    assert scoped_user1[(66, "movie")].status == "pending"
    assert scoped_user1[(66, "movie")].has_coexisting_available is True
    # User 2: only their own settled `available` row is visible in this scope
    # -- user 1's sibling `available` row must not leak in as a coexisting one.
    assert scoped_user2[(66, "movie")].has_coexisting_available is False


async def test_compact_state_absent_key_no_fabrication(sessionmaker_: SessionMaker) -> None:
    await _add_request(sessionmaker_, tmdb_id=77, status=RequestStatus.pending)
    async with sessionmaker_() as session:
        result = await SqlRequestRepository(session).compact_states_by_tmdb_ids(
            [(77, "movie"), (999999, "movie")]
        )
    assert (999999, "movie") not in result
    assert (77, "movie") in result


async def test_compact_scan_plan_uses_an_index_never_a_full_table_scan(
    sessionmaker_: SessionMaker,
) -> None:
    """EXPLAIN QUERY PLAN pin (mirrors ``_page_stmt``'s): the compact/display
    ``tmdb_id IN (...)`` scan is served by AN existing index on ``tmdb_id``
    (either the single-column ``ix_media_requests_tmdb_id`` or the composite
    ``ix_media_requests_tmdb_media`` -- the query planner's call, since this
    query never filters on ``media_type`` in SQL) -- never a full ``SCAN
    media_requests``. No new index ships with issue #370."""
    tmdb_scan_stmt = getattr(requests_repo_module, "_tmdb_scan_stmt")  # noqa: B009 - private, pinned
    stmt = tmdb_scan_stmt({90000}, for_user_id=None)
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    async with sessionmaker_() as session:
        rows = (await session.execute(sa_text("EXPLAIN QUERY PLAN " + sql))).all()
    plan = " | ".join(str(row[-1]) for row in rows)
    assert "SEARCH media_requests USING INDEX ix_media_requests_tmdb" in plan, plan
    assert "SCAN media_requests" not in plan, plan


# --------------------------------------------------------------------------- #
# Router: POST /requests/live-state
# --------------------------------------------------------------------------- #


async def test_live_state_endpoint_scope_and_shape(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id, cookies, headers = await _user_session(app, tag="shared")
    mine_id = await _add_request(
        sessionmaker_, tmdb_id=100, status=RequestStatus.downloading, user_id=None
    )
    async with sessionmaker_() as session:
        session.add(RequestSubscriber(request_id=mine_id, user_id=user_id))
        await session.commit()
    await _add_request(sessionmaker_, tmdb_id=200, status=RequestStatus.available)  # not theirs

    # Empty keys -> empty map, no error.
    response = await client.post(
        "/api/v1/requests/live-state", json={"keys": []}, cookies=cookies, headers=headers
    )
    assert response.status_code == 200
    assert response.json() == {"states": {}}

    response = await client.post(
        "/api/v1/requests/live-state",
        json={
            "keys": [
                {"media_type": "movie", "tmdb_id": 100},
                {"media_type": "movie", "tmdb_id": 200},
                {"media_type": "movie", "tmdb_id": 300},  # no history at all
            ]
        },
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200
    states = response.json()["states"]
    assert set(states) == {"movie:100"}  # shared user: only their own row, absent key omitted
    assert states["movie:100"] == {
        "status": "downloading",
        "request_id": mine_id,
        "has_history": True,
        "has_coexisting_available": False,
    }

    # Admin/API-key: unscoped, sees both.
    response = await client.post(
        "/api/v1/requests/live-state",
        json={
            "keys": [
                {"media_type": "movie", "tmdb_id": 100},
                {"media_type": "movie", "tmdb_id": 200},
            ]
        },
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert set(response.json()["states"]) == {"movie:100", "movie:200"}


async def test_live_state_endpoint_caps_key_count(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    too_many = [{"media_type": "movie", "tmdb_id": i} for i in range(501)]
    response = await client.post(
        "/api/v1/requests/live-state", json={"keys": too_many}, headers=_HEADERS
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Router: GET /requests/by-title
# --------------------------------------------------------------------------- #


async def test_by_title_returns_all_rows_scoped(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id, cookies, headers = await _user_session(app, tag="shared")
    first_id = await _add_request(
        sessionmaker_, tmdb_id=500, status=RequestStatus.available, title="History"
    )
    async with sessionmaker_() as session:
        session.add(RequestSubscriber(request_id=first_id, user_id=user_id))
        await session.commit()
    second_id = await _add_request(
        sessionmaker_,
        tmdb_id=500,
        status=RequestStatus.pending,
        title="History",
        user_id=user_id,
    )
    async with sessionmaker_() as session:
        session.add(RequestSubscriber(request_id=second_id, user_id=user_id))
        await session.commit()
    await _add_request(sessionmaker_, tmdb_id=999, status=RequestStatus.available)  # other title

    response = await client.get(
        "/api/v1/requests/by-title",
        params={"tmdb_id": 500, "media_type": "movie"},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert [r["id"] for r in body["requests"]] == [first_id, second_id]  # id-ascending, ALL rows
    assert body["next_cursor"] is None
    assert all(r["can_withdraw"] is True for r in body["requests"])

    # Unknown title: empty list, never a 404.
    response = await client.get(
        "/api/v1/requests/by-title",
        params={"tmdb_id": 12345, "media_type": "movie"},
        headers=_HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {"requests": [], "next_cursor": None}


# --------------------------------------------------------------------------- #
# Stage A: legacy ``can_withdraw`` swap is byte-identical
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("as_admin", [False, True])
async def test_legacy_can_withdraw_byte_identical_after_among_swap(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    as_admin: bool,
) -> None:
    """Issue #370 Stage A swapped the legacy (no-``limit``) list's membership read
    from the whole-set ``list_subscribed_request_ids`` to the page-scoped
    ``subscribed_request_ids_among`` (now the only such method -- Stage B
    deleted the whole-set one). Both an admin's unfiltered view and a shared
    user's own-rows view must show the exact same ``can_withdraw``/
    ``has_other_participants`` the old whole-set read would have produced."""
    await seed(initialized=True, app_api_key=_API_KEY)
    caller_id, cookies, headers = await _user_session(
        app, tag="caller", permissions=1 if as_admin else 0
    )
    other_id, _other_cookies, _other_headers = await _user_session(app, tag="other")

    async with sessionmaker_() as session:
        solo = MediaRequest(
            tmdb_id=700,
            media_type=MediaType.movie,
            title="Solo",
            status=RequestStatus.pending,
            user_id=caller_id,
        )
        shared = MediaRequest(
            tmdb_id=701,
            media_type=MediaType.movie,
            title="Shared",
            status=RequestStatus.downloading,
            user_id=caller_id,
        )
        foreign = MediaRequest(
            tmdb_id=702,
            media_type=MediaType.movie,
            title="Foreign",
            status=RequestStatus.pending,
            user_id=other_id,
        )
        session.add_all([solo, shared, foreign])
        await session.flush()
        session.add_all(
            [
                RequestSubscriber(request_id=solo.id, user_id=caller_id),
                RequestSubscriber(request_id=shared.id, user_id=caller_id),
                RequestSubscriber(request_id=shared.id, user_id=other_id),
                RequestSubscriber(request_id=foreign.id, user_id=other_id),
            ]
        )
        await session.commit()
        solo_id, shared_id, foreign_id = solo.id, shared.id, foreign.id

    response = await client.get("/api/v1/requests", cookies=cookies, headers=headers)
    assert response.status_code == 200
    by_id = {r["id"]: r for r in response.json()["requests"]}
    if as_admin:
        # Admin unfiltered: sees every row; can_withdraw reflects TRUE membership.
        assert set(by_id) == {solo_id, shared_id, foreign_id}
        assert by_id[solo_id]["can_withdraw"] is True
        assert by_id[solo_id]["has_other_participants"] is False
        assert by_id[shared_id]["can_withdraw"] is True
        assert by_id[shared_id]["has_other_participants"] is True
        assert by_id[foreign_id]["can_withdraw"] is False
        # The caller is not a participant at all, but ``other_id`` genuinely IS
        # one -- "has OTHER participants" is true regardless of the caller's own
        # membership.
        assert by_id[foreign_id]["has_other_participants"] is True
    else:
        # Shared user: only their own rows, every one a subscription by construction.
        assert set(by_id) == {solo_id, shared_id}
        assert by_id[solo_id]["can_withdraw"] is True
        assert by_id[solo_id]["has_other_participants"] is False
        assert by_id[shared_id]["can_withdraw"] is True
        assert by_id[shared_id]["has_other_participants"] is True
