"""Subscriber control endpoints (issue #314):

``DELETE /api/v1/requests/{id}/subscription`` (withdrawal / collaborative
cancellation) and the non-admin-owner branch of ``POST /{id}/cancel``.
Focuses on routing, authZ, and HTTP error mapping -- the deep flow (handoff
selection, teardown reuse) is covered by
``tests/services/test_correction_service.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.models import (
    AuthSession,
    Download,
    MediaRequest,
    MediaType,
    RequestStatus,
    RequestSubscriber,
    User,
)
from plex_manager.web.deps import hash_session_token
from tests.web.fakes import FakeQbittorrent, override_adapters

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "subscription-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_TMDB = 603
_CULPRIT = "3" * 40


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


async def test_withdraw_endpoint_subscriber_gets_204_and_drops_off_their_list(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, _owner_cookies, _owner_headers = await _user_session(app, tag="owner")
    subscriber_id, sub_cookies, sub_headers = await _user_session(app, tag="subscriber")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
            user_id=owner_id,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=owner_id))
        session.add(RequestSubscriber(request_id=request_id, user_id=subscriber_id))
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request_id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()

    response = await client.delete(
        f"/api/v1/requests/{request_id}/subscription", cookies=sub_cookies, headers=sub_headers
    )
    assert response.status_code == 204
    assert response.content == b""

    list_response = await client.get("/api/v1/requests", cookies=sub_cookies, headers=sub_headers)
    assert list_response.status_code == 200
    assert list_response.json()["requests"] == []

    # The owner is untouched -- still owns it, nothing torn down.
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.user_id == owner_id
    assert row.status == RequestStatus.downloading


async def test_withdraw_endpoint_404_for_non_subscriber(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    _outsider_id, outsider_cookies, outsider_headers = await _user_session(app, tag="outsider")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    response = await client.delete(
        f"/api/v1/requests/{request_id}/subscription",
        cookies=outsider_cookies,
        headers=outsider_headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "request_not_found"


async def test_withdraw_endpoint_404_for_unknown_request(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    _user_id, cookies, headers = await _user_session(app, tag="nobody")
    response = await client.delete(
        "/api/v1/requests/999999/subscription", cookies=cookies, headers=headers
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "request_not_found"


async def test_withdraw_endpoint_404_for_admin_api_key_no_user_identity(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # The legacy app API key carries no user identity -- it can never withdraw,
    # even from a request it created via automation. Admins use POST /cancel.
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.commit()
        request_id = request.id

    response = await client.delete(f"/api/v1/requests/{request_id}/subscription", headers=_HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "request_not_found"


async def test_withdraw_endpoint_owner_with_others_hands_off_ownership(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, owner_cookies, owner_headers = await _user_session(app, tag="owner")
    other_id, other_cookies, other_headers = await _user_session(app, tag="other")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.pending,
            user_id=owner_id,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=owner_id))
        session.add(RequestSubscriber(request_id=request_id, user_id=other_id))
        await session.commit()

    response = await client.delete(
        f"/api/v1/requests/{request_id}/subscription", cookies=owner_cookies, headers=owner_headers
    )
    assert response.status_code == 204

    get_response = await client.get(
        f"/api/v1/requests/{request_id}", cookies=other_cookies, headers=other_headers
    )
    assert get_response.status_code == 200
    body = get_response.json()
    assert body["is_owner"] is True
    assert body["can_mutate"] is True
    assert body["can_withdraw"] is True
    assert body["has_other_participants"] is False


async def test_cancel_endpoint_non_admin_owner_with_others_returns_409(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, owner_cookies, owner_headers = await _user_session(app, tag="owner")
    other_id, _other_cookies, _other_headers = await _user_session(app, tag="other")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
            user_id=owner_id,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=owner_id))
        session.add(RequestSubscriber(request_id=request_id, user_id=other_id))
        await session.commit()

    response = await client.post(
        f"/api/v1/requests/{request_id}/cancel", cookies=owner_cookies, headers=owner_headers
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "has_other_participants"
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None and row.status == RequestStatus.downloading


async def test_cancel_endpoint_non_admin_sole_owner_settles_and_keeps_subscription(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, owner_cookies, owner_headers = await _user_session(app, tag="owner")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
            user_id=owner_id,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=owner_id))
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request_id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()

    qbt = FakeQbittorrent()
    override_adapters(app, qbt=qbt)
    response = await client.post(
        f"/api/v1/requests/{request_id}/cancel", cookies=owner_cookies, headers=owner_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["is_owner"] is True
    assert body["can_withdraw"] is True
    assert body["has_other_participants"] is False
    assert (_CULPRIT, True) in qbt.removed


async def test_cancel_endpoint_admin_hard_cancel_keeps_every_subscriber(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, _owner_cookies, _owner_headers = await _user_session(app, tag="owner")
    other_id, _other_cookies, _other_headers = await _user_session(app, tag="other")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
            user_id=owner_id,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=owner_id))
        session.add(RequestSubscriber(request_id=request_id, user_id=other_id))
        await session.commit()

    override_adapters(app, qbt=FakeQbittorrent())
    response = await client.post(f"/api/v1/requests/{request_id}/cancel", headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    async with sessionmaker_() as session:
        subs = (
            (
                await session.execute(
                    select(RequestSubscriber.user_id).where(
                        RequestSubscriber.request_id == request_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(subs) == {owner_id, other_id}


async def test_withdraw_endpoint_409_service_not_configured_last_participant(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # Last participant, active torrent, qBittorrent unconfigured -- the endpoint
    # must refuse honestly (never a silent skip) rather than orphaning the seed.
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, owner_cookies, owner_headers = await _user_session(app, tag="owner")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
            user_id=owner_id,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=owner_id))
        session.add(
            Download(
                torrent_hash=_CULPRIT,
                status="downloading",
                media_request_id=request_id,
                tmdb_id=_TMDB,
            )
        )
        await session.commit()

    response = await client.delete(
        f"/api/v1/requests/{request_id}/subscription", cookies=owner_cookies, headers=owner_headers
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "service_not_configured"
    assert response.json()["service"] == "qbittorrent"
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None and row.status == RequestStatus.downloading


async def test_request_response_flags_for_owner_subscriber_and_admin_non_subscriber(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, owner_cookies, owner_headers = await _user_session(app, tag="owner")
    subscriber_id, sub_cookies, sub_headers = await _user_session(app, tag="subscriber")
    # A browser-session admin (permissions > 0) who never subscribed to the row.
    _admin_id, admin_cookies, admin_headers = await _user_session(
        app, tag="browser-admin", permissions=1
    )
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.pending,
            user_id=owner_id,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=owner_id))
        session.add(RequestSubscriber(request_id=request_id, user_id=subscriber_id))
        await session.commit()

    owner_view = (
        await client.get(
            f"/api/v1/requests/{request_id}", cookies=owner_cookies, headers=owner_headers
        )
    ).json()
    assert owner_view["is_owner"] is True
    assert owner_view["can_mutate"] is True
    assert owner_view["can_withdraw"] is True
    assert owner_view["has_other_participants"] is True

    subscriber_view = (
        await client.get(f"/api/v1/requests/{request_id}", cookies=sub_cookies, headers=sub_headers)
    ).json()
    assert subscriber_view["is_owner"] is False
    assert subscriber_view["can_mutate"] is False
    assert subscriber_view["can_withdraw"] is True
    assert subscriber_view["has_other_participants"] is True

    admin_view = (
        await client.get(
            f"/api/v1/requests/{request_id}", cookies=admin_cookies, headers=admin_headers
        )
    ).json()
    assert admin_view["is_owner"] is False
    assert admin_view["can_mutate"] is True  # admin always mutates
    assert admin_view["can_withdraw"] is False  # not a subscriber -- uses POST /cancel
    assert admin_view["has_other_participants"] is True  # 2 real subscribers, admin isn't one

    # The list endpoint's batched path must agree with the single-record path.
    owner_list = (
        await client.get("/api/v1/requests", cookies=owner_cookies, headers=owner_headers)
    ).json()["requests"]
    assert len(owner_list) == 1
    assert owner_list[0]["can_withdraw"] is True
    assert owner_list[0]["has_other_participants"] is True

    admin_list = (
        await client.get("/api/v1/requests", cookies=admin_cookies, headers=admin_headers)
    ).json()["requests"]
    assert len(admin_list) == 1
    assert admin_list[0]["can_withdraw"] is False
    assert admin_list[0]["has_other_participants"] is True
