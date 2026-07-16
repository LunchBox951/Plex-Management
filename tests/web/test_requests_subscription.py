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
import pytest
from fastapi import FastAPI, HTTPException
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
from plex_manager.web.deps import AuthContext, AuthMethod, hash_session_token
from plex_manager.web.routers import requests as requests_router
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


async def test_withdraw_endpoint_subscriber_gets_settled_false_and_drops_off_their_list(
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
    assert response.status_code == 200
    # Mere subscription removal -- others remain, nothing torn down (#351).
    assert response.json() == {"settled": False}

    list_response = await client.get("/api/v1/requests", cookies=sub_cookies, headers=sub_headers)
    assert list_response.status_code == 200
    assert list_response.json()["requests"] == []

    # The owner is untouched -- still owns it, nothing torn down.
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.user_id == owner_id
    assert row.status == RequestStatus.downloading


async def test_withdraw_endpoint_last_participant_teardown_returns_settled_true(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    """The sole participant on a cancellable row settles it (#351): a ``pending``
    request with no active downloads is a pure-DB cancel (no qBittorrent needed),
    and the endpoint echoes ``{"settled": true}`` so the client can word its toast
    off the real outcome rather than a click-time snapshot."""
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, owner_cookies, owner_headers = await _user_session(app, tag="owner")
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
        await session.commit()

    response = await client.delete(
        f"/api/v1/requests/{request_id}/subscription", cookies=owner_cookies, headers=owner_headers
    )
    assert response.status_code == 200
    assert response.json() == {"settled": True}

    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.status == RequestStatus.cancelled  # torn down + settled
    assert row.user_id is None  # ownerless, zero subscribers


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
    """The legacy app API key carries no user identity -- it can never withdraw,
    even from a request it created via automation. Admins use POST /cancel.

    This is an observable, black-box pin on the WHOLE endpoint's behavior; see
    ``test_require_subscriber_none_identity_disjunct_is_load_bearing`` below for
    the disjunct-level fidelity this test alone cannot provide (issue #380
    finding 2)."""
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


async def test_require_subscriber_none_identity_disjunct_is_load_bearing(
    sessionmaker_: SessionMaker, seed: SeedFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Directly pins ``_require_subscriber``'s ``auth.user_id is None`` disjunct
    in isolation (issue #380 finding 2) -- something no HTTP-level test of
    ``DELETE /subscription`` can do.

    ``_require_subscriber``'s guard ORs three disjuncts: ``record is None``,
    ``auth.user_id is None``, and ``not is_request_visible_to_user(...)``. A
    black-box HTTP test cannot isolate the middle one: a zero-subscriber request
    makes the visibility disjunct ALSO 404 any caller (``is_request_visible_to_
    user(..., None)`` is provably ``False`` regardless of subscriber population,
    since a subscriber row's ``user_id`` is NOT NULL and can never match
    ``None``) -- and even populating real subscribers plus forcing the
    visibility check to always succeed does not help, because
    ``withdraw_subscription_endpoint`` carries its OWN defensive
    ``auth.user_id is None`` re-check (``# pragma: no cover``, requests.py
    :~943) that produces the exact same 404 if this dependency's guard is ever
    weakened. Both were verified directly (not merely asserted) before writing
    this test.

    This test therefore calls ``_require_subscriber`` itself, bypassing the
    endpoint's second safety net entirely, with the visibility check
    monkeypatched to ALWAYS succeed -- so the ``auth.user_id is None`` disjunct
    is the ONLY thing left able to raise. If it is ever removed, this test (and
    only this test) goes red.
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    async with sessionmaker_() as session:
        other = User(username="other-real-subscriber", permissions=0)
        session.add(other)
        await session.flush()
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.downloading,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=other.id))
        await session.commit()

    async def _always_visible(session: AsyncSession, request_id: int, user_id: int) -> bool:
        del session, request_id, user_id
        return True

    monkeypatch.setattr(
        requests_router.request_service, "is_request_visible_to_user", _always_visible
    )

    admin_auth = AuthContext(method=AuthMethod.api_key, is_admin=True, via_api_key_header=True)
    assert admin_auth.user_id is None
    async with sessionmaker_() as session:
        with pytest.raises(HTTPException) as exc_info:
            await requests_router._require_subscriber(  # pyright: ignore[reportPrivateUsage]
                request_id, admin_auth, session
            )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "request_not_found"


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
    assert response.status_code == 200
    # Owner handoff to the remaining participant -- a mere removal, not a teardown.
    assert response.json() == {"settled": False}

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


async def test_withdraw_endpoint_409_last_participant_on_import_blocked(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, sessionmaker_: SessionMaker
) -> None:
    # Codex #333, Finding 1: the last participant of an ACTIVE non-cancellable
    # row (import_blocked) cannot withdraw -- the endpoint refuses with an honest,
    # actionable 409 rather than stranding a dedup-blocking row ownerless.
    await seed(initialized=True, app_api_key=_API_KEY)
    owner_id, owner_cookies, owner_headers = await _user_session(app, tag="owner")
    async with sessionmaker_() as session:
        request = MediaRequest(
            tmdb_id=_TMDB,
            media_type=MediaType.movie,
            title="Some Movie",
            status=RequestStatus.import_blocked,
            user_id=owner_id,
        )
        session.add(request)
        await session.flush()
        request_id = request.id
        session.add(RequestSubscriber(request_id=request_id, user_id=owner_id))
        await session.commit()

    response = await client.delete(
        f"/api/v1/requests/{request_id}/subscription", cookies=owner_cookies, headers=owner_headers
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "withdrawal_blocked_active_request"
    # Nothing touched: still owned, still subscribed, still import_blocked.
    async with sessionmaker_() as session:
        row = await session.get(MediaRequest, request_id)
    assert row is not None
    assert row.user_id == owner_id
    assert row.status == RequestStatus.import_blocked


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
