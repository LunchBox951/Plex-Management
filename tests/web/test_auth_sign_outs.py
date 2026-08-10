"""The web-readable audit surface for automatic sign-outs (issue #556).

``GET /api/v1/auth/sign-outs`` reads back the ``AuditLog`` rows the
share-revalidation sweep (#391) writes when it signs someone out. Before this,
the durable record existed but was reachable only by SQL, so the only
web-operable answer to "why was I signed out?" was a Logs-page line that log
retention eventually trims — a terminal-only answer path, which north star #2
forbids.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from plex_manager.models import AuditLog, AuthSession, User
from plex_manager.services import audit_service
from plex_manager.web.deps import hash_session_token

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "sign-outs-app-key"

Cookies = dict[str, str]


async def _admin_cookies(app: FastAPI, *, tag: str = "adm") -> Cookies:
    """Mint an admin browser session and return its cookies."""
    token = f"sess-{tag}"
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as session:
        user = User(plex_id=100, username=f"admin-{tag}", permissions=1)
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(token),
                expires_at=now + timedelta(days=30),
                last_seen_at=now,
            )
        )
        await session.commit()
    return {"plexmgr.session": token, "plexmgr.csrf": f"csrf-{tag}"}


async def _add_user(app: FastAPI, *, plex_id: int, username: str) -> int:
    async with app.state.sessionmaker() as session:
        user = User(plex_id=plex_id, username=username, permissions=0)
        session.add(user)
        await session.commit()
        return user.id


async def _record_sign_out(
    app: FastAPI,
    *,
    user_id: int | None,
    action_type: str,
    previous_state: str | None = "authorized",
    share_state: str = "share_revoked",
    sessions_revoked: int = 1,
    admin_exempt: bool = False,
    username: str | None = None,
    description: str = "Automatic share revalidation: …",
) -> None:
    """Write one sign-out audit row exactly as ``plex_access_service`` does.

    ``username=None`` reproduces a LEGACY row — one written before the sweep
    started stamping the account name into the payload — so the join fallback
    stays covered.
    """
    new_value: dict[str, object] = {
        "share_state": share_state,
        "sessions_revoked": sessions_revoked,
        "admin_exempt": admin_exempt,
    }
    if username is not None:
        new_value["username"] = username
    async with app.state.sessionmaker() as session:
        await audit_service.record(
            session,
            actor_user_id=None,
            action_type=action_type,
            entity_type=audit_service.USER_ENTITY_TYPE,
            entity_id=user_id,
            old_value={"share_state": previous_state},
            new_value=new_value,
            description=description,
        )
        await session.commit()


async def test_sign_outs_requires_admin(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """The rows name other people's accounts and access state — admin-only, the
    same posture as the sessions list they sit beside."""
    await seed(initialized=True, app_api_key=_API_KEY)
    token = "sess-shared"  # noqa: S105 - an opaque session cookie value, not a secret
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as session:
        user = User(plex_id=300, username="shared", permissions=0)
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(token),
                expires_at=now + timedelta(days=30),
                last_seen_at=now,
            )
        )
        await session.commit()

    client.cookies.update({"plexmgr.session": token, "plexmgr.csrf": "csrf-shared"})
    response = await client.get("/api/v1/auth/sign-outs")

    assert response.status_code == 403
    assert response.json()["detail"] == "admin_required"


async def test_sign_outs_requires_authentication(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/auth/sign-outs")

    assert response.status_code == 401


async def test_sign_outs_empty_when_nothing_was_ever_swept(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    client.cookies.update(await _admin_cookies(app))

    response = await client.get("/api/v1/auth/sign-outs")

    assert response.status_code == 200
    assert response.json() == {"entries": [], "limit": 25}


async def test_sign_outs_report_the_revoked_share_with_its_counts(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id = await _add_user(app, plex_id=201, username="guest")
    await _record_sign_out(
        app,
        user_id=user_id,
        action_type=audit_service.SHARE_REVOKED_ACTION,
        sessions_revoked=3,
        username="guest",
    )
    client.cookies.update(await _admin_cookies(app))

    response = await client.get("/api/v1/auth/sign-outs")

    assert response.status_code == 200
    entry = response.json()["entries"][0]
    assert entry["action_type"] == "user.share_revoked"
    assert entry["user_id"] == user_id
    assert entry["username"] == "guest"
    assert entry["previous_share_state"] == "authorized"
    assert entry["share_state"] == "share_revoked"
    assert entry["sessions_revoked"] == 3
    assert entry["admin_exempt"] is False
    assert entry["signed_out"] is True
    assert entry["occurred_at"].endswith("Z") or "+00:00" in entry["occurred_at"]


async def test_sign_outs_keep_a_stale_token_distinct_from_a_revoked_share(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """The two causes must never collapse: a rejected credential says nothing
    about the share, and reporting it as a removal would be a lie."""
    await seed(initialized=True, app_api_key=_API_KEY)
    revoked_id = await _add_user(app, plex_id=201, username="revoked")
    stale_id = await _add_user(app, plex_id=202, username="stale")
    await _record_sign_out(app, user_id=revoked_id, action_type=audit_service.SHARE_REVOKED_ACTION)
    await _record_sign_out(
        app,
        user_id=stale_id,
        action_type=audit_service.PLEX_SIGN_IN_EXPIRED_ACTION,
        share_state="token_stale",
    )
    client.cookies.update(await _admin_cookies(app))

    entries = (await client.get("/api/v1/auth/sign-outs")).json()["entries"]

    by_user = {entry["user_id"]: entry for entry in entries}
    assert by_user[revoked_id]["action_type"] == "user.share_revoked"
    assert by_user[stale_id]["action_type"] == "user.plex_sign_in_expired"
    assert by_user[stale_id]["share_state"] == "token_stale"


async def test_sign_outs_report_an_admin_exempt_verdict_as_not_signed_out(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """An admin's share loss is RECORDED but never acted on (ADR-0005). Calling
    it a sign-out would misreport what the app actually did."""
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id = await _add_user(app, plex_id=203, username="owner")
    await _record_sign_out(
        app,
        user_id=user_id,
        action_type=audit_service.admin_exempt_action(audit_service.SHARE_REVOKED_ACTION),
        sessions_revoked=0,
        admin_exempt=True,
    )
    client.cookies.update(await _admin_cookies(app))

    entry = (await client.get("/api/v1/auth/sign-outs")).json()["entries"][0]

    assert entry["action_type"] == "user.share_revoked_admin_exempt"
    assert entry["admin_exempt"] is True
    assert entry["signed_out"] is False
    assert entry["sessions_revoked"] == 0


async def test_sign_outs_survive_the_deletion_of_the_account_they_describe(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """The name is stamped INTO the row, so the record still says who it was
    after the account is gone — and cannot be re-pointed at whoever inherits the
    freed primary key."""
    await seed(initialized=True, app_api_key=_API_KEY)
    client.cookies.update(await _admin_cookies(app))
    user_id = await _add_user(app, plex_id=204, username="departed")
    await _record_sign_out(
        app,
        user_id=user_id,
        action_type=audit_service.SHARE_REVOKED_ACTION,
        username="departed",
    )
    async with app.state.sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        await session.delete(user)
        await session.commit()
    # A SEPARATE transaction: SQLite only frees the highest rowid for reuse once
    # the delete is committed, and a unit of work that batches both orders the
    # INSERT before the DELETE — which would hand the newcomer a fresh id and
    # make this test silently stop exercising id reuse at all.
    async with app.state.sessionmaker() as session:
        newcomer = User(plex_id=999, username="newcomer", permissions=0)
        session.add(newcomer)
        await session.commit()
        # The whole point of the test: without this the guard could degrade to
        # passing on a row the join would never have mis-resolved anyway.
        assert newcomer.id == user_id

    entry = (await client.get("/api/v1/auth/sign-outs")).json()["entries"][0]

    assert entry["user_id"] == user_id
    assert entry["username"] == "departed"


async def test_legacy_sign_out_rows_report_an_unknown_subject_not_a_joined_name(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Rows written before the username stamp existed (the sweep shipped in
    #557) must NOT be attributed by joining ``entity_id`` back to ``users``:
    that id is reusable and the account may since have been renamed, so the join
    can confidently display an unrelated person. An honest unknown is the only
    safe answer."""
    await seed(initialized=True, app_api_key=_API_KEY)
    client.cookies.update(await _admin_cookies(app))
    present_id = await _add_user(app, plex_id=205, username="still-here")
    await _record_sign_out(app, user_id=present_id, action_type=audit_service.SHARE_REVOKED_ACTION)
    await _record_sign_out(app, user_id=4242, action_type=audit_service.SHARE_REVOKED_ACTION)

    entries = (await client.get("/api/v1/auth/sign-outs")).json()["entries"]

    by_user = {entry["user_id"]: entry for entry in entries}
    # The account still exists under that exact id — and is STILL not named,
    # because the row itself never recorded who it was.
    assert by_user[present_id]["username"] is None
    assert by_user[4242]["username"] is None


async def test_a_renamed_account_cannot_relabel_an_older_sign_out(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """The stamped name is a snapshot of who it was, not a live lookup."""
    await seed(initialized=True, app_api_key=_API_KEY)
    client.cookies.update(await _admin_cookies(app))
    user_id = await _add_user(app, plex_id=208, username="before")
    await _record_sign_out(
        app,
        user_id=user_id,
        action_type=audit_service.SHARE_REVOKED_ACTION,
        username="before",
    )
    async with app.state.sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        user.username = "after"
        await session.commit()

    entry = (await client.get("/api/v1/auth/sign-outs")).json()["entries"][0]

    assert entry["username"] == "before"


async def test_a_revoke_that_cut_nothing_is_not_reported_as_a_sign_out(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Due-selection sees a live session, but the conditioned revoke runs later
    and can find nothing left to cut (the user logged out mid-sweep). The
    verdict is real; the sign-out is not."""
    await seed(initialized=True, app_api_key=_API_KEY)
    client.cookies.update(await _admin_cookies(app))
    user_id = await _add_user(app, plex_id=209, username="already-gone")
    await _record_sign_out(
        app,
        user_id=user_id,
        action_type=audit_service.SHARE_REVOKED_ACTION,
        sessions_revoked=0,
        username="already-gone",
    )

    entry = (await client.get("/api/v1/auth/sign-outs")).json()["entries"][0]

    assert entry["admin_exempt"] is False
    assert entry["sessions_revoked"] == 0
    # Not exempt, but nothing was cut — claiming a sign-out would describe an
    # action the app never took.
    assert entry["signed_out"] is False


async def test_sign_outs_are_not_a_generic_audit_browser(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """Audit rows outside the sign-out family must not leak through this
    endpoint — the query surface is deliberately narrow."""
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id = await _add_user(app, plex_id=205, username="requester")
    async with app.state.sessionmaker() as session:
        await audit_service.record(
            session,
            actor_user_id=user_id,
            action_type="request.owner_handoff",
            entity_type="media_request",
            entity_id=9,
            description="not a sign-out",
        )
        await audit_service.record(
            session,
            actor_user_id=None,
            action_type="update.coordinator_phase_force_reset",
            entity_type="user",
            entity_id=user_id,
            description="also not a sign-out",
        )
        await session.commit()
    client.cookies.update(await _admin_cookies(app))

    response = await client.get("/api/v1/auth/sign-outs")

    assert response.json()["entries"] == []
    # The rows themselves are untouched — this endpoint only declines to show them.
    async with app.state.sessionmaker() as session:
        assert len((await session.execute(select(AuditLog))).scalars().all()) == 2


async def test_sign_outs_are_newest_first_and_bounded_by_limit(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id = await _add_user(app, plex_id=206, username="repeat")
    for _ in range(3):
        await _record_sign_out(app, user_id=user_id, action_type=audit_service.SHARE_REVOKED_ACTION)
    client.cookies.update(await _admin_cookies(app))

    body = (await client.get("/api/v1/auth/sign-outs", params={"limit": 2})).json()

    assert body["limit"] == 2
    ids = [entry["id"] for entry in body["entries"]]
    assert len(ids) == 2
    assert ids == sorted(ids, reverse=True)


async def test_sign_outs_reject_an_out_of_range_limit(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    client.cookies.update(await _admin_cookies(app))

    assert (await client.get("/api/v1/auth/sign-outs", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/v1/auth/sign-outs", params={"limit": 201})).status_code == 422


async def test_sign_outs_tolerate_a_wrong_shaped_audit_payload(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """The JSON columns are schemaless, so a row written by an older/newer build
    must degrade to honest defaults rather than 500 the panel that explains
    sign-outs."""
    await seed(initialized=True, app_api_key=_API_KEY)
    user_id = await _add_user(app, plex_id=207, username="odd")
    async with app.state.sessionmaker() as session:
        session.add(
            AuditLog(
                user_id=None,
                action_type=audit_service.SHARE_REVOKED_ACTION,
                entity_type=audit_service.USER_ENTITY_TYPE,
                entity_id=user_id,
                old_value=None,
                new_value={"share_state": 7, "sessions_revoked": "many", "admin_exempt": "yes"},
                description=None,
            )
        )
        await session.commit()
    client.cookies.update(await _admin_cookies(app))

    entry = (await client.get("/api/v1/auth/sign-outs")).json()["entries"][0]

    assert entry["share_state"] is None
    assert entry["sessions_revoked"] == 0
    assert entry["admin_exempt"] is False
    # An unreadable count degrades to 0, and 0 sessions cut is not a sign-out —
    # a row we cannot parse must not be reported as a confident action.
    assert entry["signed_out"] is False
    assert entry["username"] is None
