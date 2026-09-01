"""The share-revalidation sweep's web-layer half (issue #391 PR-2).

``plex_access_service`` owns the verdict ladder, the persistence and the audit
row (covered in ``tests/services/test_plex_access_sweep.py``); what only this
layer can do is resolve the configured server, read the operator's interval,
re-confirm the server anchor against a LIVE ``/identity`` probe, and CLOSE the
realtime streams of the users the sweep signs out.

Two failure modes get dedicated coverage here because both are install-wide:
stamping ``revoked_at`` while leaving an SSE connection open is not a revocation
(#183), and acting on a stale machine identifier after a server rebuild would
sign out everyone including the admin who is the only one able to repoint it
(ADR-0005 never-locked-out).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from plex_manager.adapters.plex.library import reset_caches
from plex_manager.models import AuditLog, AuthSession, User
from plex_manager.services import plex_access_service
from plex_manager.web import app as app_module
from plex_manager.web.deps import PLEX_MACHINE_ID_SETTING, AuthMethod, SettingsStore
from plex_manager.web.events import EventSubscription, StreamClosed, get_event_hub

SeedFn = Callable[..., Awaitable[None]]

_MACHINE_ID = "configured-server-machine-id"

#: Upper bound on frames ``_close_reason`` will drain before giving up. A closed
#: subscription only ever holds the connect-time sync event plus the sentinel.
_MAX_DRAINED_FRAMES = 100


@pytest.fixture(autouse=True)
def _clear_library_caches() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """A successful capture warms ``PlexLibrary``'s process-wide sections cache;
    never let one test's server answer leak into another's."""
    reset_caches()
    yield
    reset_caches()


def _server_resource(machine_id: str) -> dict[str, object]:
    return {
        "name": "Living Room",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [],
    }


def _sections(*keys: str) -> dict[str, object]:
    """A ``/library/sections`` body the capture can parse."""
    return {
        "MediaContainer": {
            "size": len(keys),
            "Directory": [
                {
                    "key": key,
                    "title": f"S{key}",
                    "type": "movie",
                    "Location": [{"id": 1, "path": "/x"}],
                }
                for key in keys
            ],
        }
    }


def _plex_tv_transport(
    payload: list[dict[str, object]] | int | Mapping[str, list[dict[str, object]]],
    *,
    live_identity: str | None = _MACHINE_ID,
    sections: dict[str, object] | None = None,
) -> httpx.MockTransport:
    """plex.tv ``/resources`` plus the configured server's ``/identity`` probe.

    ``payload`` is plex.tv's answer for every token (a list), a status code that
    rejects every token (an int), or a mapping from ``X-Plex-Token`` to that
    token's own answer, so one sweep can hold users with DIFFERENT verdicts.

    ``live_identity`` is what the CONFIGURED SERVER reports when the sweep
    re-confirms its anchor before acting on a share loss: the same id means the
    anchor holds, a different one means the server was rebuilt/re-claimed, and
    ``None`` makes the probe fail outright.

    ``sections`` is the server's ``/library/sections`` body for any token; left
    ``None`` the server answers with a non-JSON body, so a capture that runs
    fails and no test that never asked for sections captures by accident.
    """

    def resources_for(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, int):
            return httpx.Response(payload, json={})
        if isinstance(payload, Mapping):
            token = request.headers.get("X-Plex-Token")
            assert token is not None and token in payload, "unexpected token at plex.tv"
            return httpx.Response(200, json=payload[token])
        return httpx.Response(200, json=payload)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            return resources_for(request)
        if request.url.path == "/identity":
            if live_identity is None:
                raise httpx.ConnectError("plex server unreachable", request=request)
            return httpx.Response(
                200, json={"MediaContainer": {"machineIdentifier": live_identity}}
            )
        if request.url.path == "/library/sections" and sections is not None:
            return httpx.Response(200, json=sections)
        return httpx.Response(200, text="ok")

    return httpx.MockTransport(handler)


async def _use_transport(app: FastAPI, transport: httpx.MockTransport) -> None:
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=transport)


async def _signed_in_user(
    app: FastAPI,
    *,
    username: str = "viewer",
    permissions: int = 0,
    token: str = "user-token",  # noqa: S107
) -> int:
    now = datetime.now(UTC)
    async with app.state.sessionmaker() as session:
        user = User(username=username, encrypted_plex_token=token, permissions=permissions)
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=f"hash-{username}",
                created_at=now,
                expires_at=now + timedelta(days=1),
                last_seen_at=now,
            )
        )
        await session.commit()
        return user.id


async def _configure_server(app: FastAPI) -> None:
    """A normal install: a cached anchor AND the credentials to re-confirm it.

    ``plex_url``/``plex_token`` matter as much as the cached identifier here --
    without them the sweep cannot re-read the anchor and refuses to act on any
    share loss (see ``_confirm_share_anchor``).
    """
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        await store.set("plex_url", "http://plex.local:32400")
        await store.set("plex_token", "service-token")
        await session.commit()


async def _tick(app: FastAPI) -> int:
    return await app_module._share_sweep_once(app)  # pyright: ignore[reportPrivateUsage]


async def _live_sessions(app: FastAPI, user_id: int) -> int:
    async with app.state.sessionmaker() as session:
        rows = (
            await session.execute(
                select(AuthSession).where(
                    AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
                )
            )
        ).scalars()
        return len(list(rows))


# --------------------------------------------------------------------------- #
# The #183 lesson: revoking sessions without closing streams is not a revocation
# --------------------------------------------------------------------------- #
async def test_confirmed_revoke_closes_the_users_open_realtime_stream(
    app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True)
    await _configure_server(app)
    user_id = await _signed_in_user(app)
    other_id = await _signed_in_user(app, username="bystander")

    hub = get_event_hub(app)
    revoked_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=user_id)
    bystander_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=other_id)
    recovery_stream = hub.subscribe(auth_method=AuthMethod.api_key.value, user_id=None)

    # plex.tv answers authoritatively with a server that is not ours: both users'
    # shares read as confirmed-revoked, but only the targeted stream may close
    # per user -- the close is per-user, exactly like the manual revoke path.
    await _use_transport(app, _plex_tv_transport([_server_resource("some-other-server")]))
    assert await _tick(app) == 2

    assert revoked_stream.closed is True
    assert bystander_stream.closed is True
    # The break-glass recovery stream has no Plex identity and is never collateral.
    assert recovery_stream.closed is False


async def _close_reason(subscription: EventSubscription) -> str | None:
    """Drain a closed subscription and return the reason it was closed with.

    The drain is BOUNDED and the close is caught explicitly rather than wrapped
    in ``pytest.raises(...)`` around a ``while True``: that shape left the
    ``return`` statically unreachable (CodeQL ``py/unreachable-statement``),
    since the only exit from the loop is an exception the context manager
    swallows. Failing loudly when the stream never closes also beats hanging.
    """
    for _ in range(_MAX_DRAINED_FRAMES):
        try:
            await subscription.get()
        except StreamClosed as closed:
            return closed.reason
    raise AssertionError("the subscription delivered events but was never closed")


async def test_a_revoked_share_and_a_stale_token_close_streams_with_different_reasons(
    app: FastAPI, seed: SeedFn
) -> None:
    """Issue #556: the close frame carries WHY, and the two sign-out causes are
    not the same fact. Telling someone their access was removed when plex.tv
    merely rejected their (password-changed) token would be a lie."""
    await seed(initialized=True)
    await _configure_server(app)
    revoked_id = await _signed_in_user(app, username="revoked")
    hub = get_event_hub(app)
    revoked_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=revoked_id)

    await _use_transport(app, _plex_tv_transport([_server_resource("some-other-server")]))
    assert await _tick(app) == 1
    assert await _close_reason(revoked_stream) == "share_revalidation_share_revoked"

    stale_id = await _signed_in_user(app, username="stale")
    stale_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=stale_id)

    # plex.tv rejects the credential outright: TOKEN_STALE, not a share verdict.
    await _use_transport(app, _plex_tv_transport(401))
    assert await _tick(app) == 1
    assert await _close_reason(stale_stream) == "share_revalidation_token_stale"


async def test_transient_failure_never_closes_a_stream_or_revokes(
    app: FastAPI, seed: SeedFn
) -> None:
    """North star #3: an unreachable plex.tv is a degraded sweep with a visible
    counter, never a sign-out."""
    await seed(initialized=True)
    await _configure_server(app)
    user_id = await _signed_in_user(app)

    hub = get_event_hub(app)
    stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=user_id)

    def unreachable(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/resources":
            raise httpx.ConnectError("plex.tv unreachable", request=request)
        return httpx.Response(200, text="ok")

    await _use_transport(app, httpx.MockTransport(unreachable))
    assert await _tick(app) == 1

    status = app.state.share_sweep_status
    assert status.state == "degraded"
    assert status.unknown == 1
    assert status.sessions_revoked == 0
    assert status.last_ok_at is None
    assert stream.closed is False
    async with app.state.sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.share_check_failures == 1
        assert (await session.execute(AuditLog.__table__.select())).first() is None


# --------------------------------------------------------------------------- #
# Status honesty
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Never-locked-out (ADR-0005): the rebuilt-server scenario
# --------------------------------------------------------------------------- #
async def test_rebuilt_server_signs_nobody_out_and_names_the_anchor_mismatch(
    app: FastAPI, seed: SeedFn
) -> None:
    """The install-wide lockout this guard exists for: the Plex server is rebuilt
    and now reports a NEW machineIdentifier, while the cached anchor is still the
    old one. plex.tv then truthfully says nobody reaches the old server, so every
    verdict is SHARE_REVOKED. Acting on that would sign out the entire install --
    admin included -- and the only web-operable repair (repointing via
    PUT /settings) needs the admin session it just destroyed."""
    await seed(initialized=True)
    await _configure_server(app)
    admin_id = await _signed_in_user(app, username="owner", permissions=1)
    viewer_id = await _signed_in_user(app, username="viewer")

    hub = get_event_hub(app)
    admin_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=admin_id)
    viewer_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=viewer_id)

    # plex.tv: nobody reaches the OLD id. The server itself: "I am a new id now."
    await _use_transport(
        app,
        _plex_tv_transport(
            [_server_resource("rebuilt-server-id")], live_identity="rebuilt-server-id"
        ),
    )

    assert await _tick(app) == 0

    status = app.state.share_sweep_status
    assert status.state == "anchor_mismatch"
    assert status.anchor_deferred == 2
    assert status.share_revoked == 0
    assert status.sessions_revoked == 0
    assert status.last_error_type == "PlexAnchorMismatch"

    # Everyone keeps their session AND their stream, and stays due.
    assert admin_stream.closed is False
    assert viewer_stream.closed is False
    async with app.state.sessionmaker() as session:
        for user_id in (admin_id, viewer_id):
            user = await session.get(User, user_id)
            assert user is not None
            assert user.share_state is None
            assert user.share_checked_at is None
        assert (await session.execute(AuditLog.__table__.select())).first() is None


async def test_unreachable_server_cannot_confirm_the_anchor_so_nobody_is_revoked(
    app: FastAPI, seed: SeedFn
) -> None:
    """plex.tv is reachable and says the share is gone, but the configured server
    cannot be reached to prove the anchor still holds. Fail safe: an unconfirmed
    anchor blocks the sign-out exactly as a mismatched one does -- but it must
    NOT claim the server changed, because that was never established."""
    await seed(initialized=True)
    await _configure_server(app)
    user_id = await _signed_in_user(app)
    await _use_transport(
        app, _plex_tv_transport([_server_resource("some-other-server")], live_identity=None)
    )

    assert await _tick(app) == 0
    status = app.state.share_sweep_status
    assert status.state == "anchor_unconfirmed"
    assert status.anchor_deferred == 1
    assert await _live_sessions(app, user_id) == 1


async def test_missing_plex_credentials_cannot_confirm_the_anchor_either(
    app: FastAPI, seed: SeedFn
) -> None:
    """A cached anchor with no url/token to re-read it against: nothing to
    confirm with, so share-loss verdicts are held and the state says only that
    it is unconfirmed."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        await SettingsStore(session).set(PLEX_MACHINE_ID_SETTING, _MACHINE_ID)
        await session.commit()
    user_id = await _signed_in_user(app)
    await _use_transport(app, _plex_tv_transport([_server_resource("some-other-server")]))

    assert await _tick(app) == 0
    status = app.state.share_sweep_status
    assert status.state == "anchor_unconfirmed"
    assert status.anchor_deferred == 1
    assert await _live_sessions(app, user_id) == 1


async def test_admin_is_never_signed_out_by_a_genuine_revocation(
    app: FastAPI, seed: SeedFn
) -> None:
    """Anchor confirmed, share genuinely gone -- the viewer is cut, the admin is
    recorded and left signed in. Losing the admin session would leave the install
    with no web-operable way back (ADR-0005)."""
    await seed(initialized=True)
    await _configure_server(app)
    admin_id = await _signed_in_user(app, username="owner", permissions=1)
    viewer_id = await _signed_in_user(app, username="viewer")

    hub = get_event_hub(app)
    admin_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=admin_id)
    viewer_stream = hub.subscribe(auth_method=AuthMethod.plex_session.value, user_id=viewer_id)

    await _use_transport(app, _plex_tv_transport([_server_resource("some-other-server")]))

    assert await _tick(app) == 2
    status = app.state.share_sweep_status
    assert status.share_revoked == 2
    assert status.admins_exempted == 1
    assert status.sessions_revoked == 1
    # Two share-loss VERDICTS but only one person cut: the reported tally is the
    # measured one, never share_revoked + token_stale (which would say 2).
    assert status.signed_out == 1

    assert admin_stream.closed is False
    assert viewer_stream.closed is True
    assert await _live_sessions(app, admin_id) == 1
    assert await _live_sessions(app, viewer_id) == 0


async def test_clean_sweep_reports_ok(app: FastAPI, seed: SeedFn) -> None:
    await seed(initialized=True)
    await _configure_server(app)
    await _signed_in_user(app)
    await _use_transport(app, _plex_tv_transport([_server_resource(_MACHINE_ID)]))

    assert await _tick(app) == 1
    status = app.state.share_sweep_status
    assert status.state == "ok"
    assert status.authorized == 1
    assert status.last_ok_at is not None
    assert status.last_run_at is not None
    # A properly configured install never flies the capture-disabled flag: it is
    # a real signal, not a permanent decoration.
    assert status.capture_unavailable is None


async def test_unconfigured_server_reports_not_configured_and_revokes_nobody(
    app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True)
    user_id = await _signed_in_user(app)
    await _use_transport(app, _plex_tv_transport([_server_resource(_MACHINE_ID)]))

    assert await _tick(app) == 0
    assert app.state.share_sweep_status.state == "not_configured"
    async with app.state.sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.share_state is None


async def test_probe_failure_reports_probe_failed_and_revokes_nobody(
    app: FastAPI, seed: SeedFn
) -> None:
    """Configured but unreachable is an OUTAGE, not an absence -- and certainly
    not evidence that anybody lost their share."""
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        # No cached machine identifier: resolution falls through to a live probe.
        await store.set("plex_url", "http://plex.local:32400")
        await store.set("plex_token", "service-token")
        await session.commit()
    user_id = await _signed_in_user(app)

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server unreachable", request=request)

    await _use_transport(app, httpx.MockTransport(unreachable))

    assert await _tick(app) == 0
    status = app.state.share_sweep_status
    assert status.state == "probe_failed"
    assert status.last_error_type is not None
    async with app.state.sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.share_state is None


# --------------------------------------------------------------------------- #
# Operator-tunable interval
# --------------------------------------------------------------------------- #
async def test_tick_honors_a_shortened_revalidation_interval(app: FastAPI, seed: SeedFn) -> None:
    """The knob is the per-user DUE cutoff, re-read every tick -- so narrowing
    the exposure window takes effect on the next tick, no restart."""
    await seed(initialized=True)
    await _configure_server(app)
    user_id = await _signed_in_user(app)
    async with app.state.sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        # Checked 2h ago: not due under the 6h default, due under a 1h setting.
        user.share_checked_at = datetime.now(UTC) - timedelta(hours=2)
        user.share_state = "authorized"
        await session.commit()
    await _use_transport(app, _plex_tv_transport([_server_resource(_MACHINE_ID)]))

    assert await _tick(app) == 0

    async with app.state.sessionmaker() as session:
        await SettingsStore(session).set("share_revalidation_interval_hours", "1")
        await session.commit()

    assert await _tick(app) == 1


# --------------------------------------------------------------------------- #
# Loop resilience
# --------------------------------------------------------------------------- #
async def test_loop_survives_a_failing_tick_and_records_it(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad cycle must never kill the loop -- the same contract as
    ``_session_sweep_loop`` -- and the failure must be visible on the status
    rather than swallowed."""
    calls = 0

    async def exploding_tick(_app: FastAPI) -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("boom")

    async def no_sleep(_seconds: float) -> None:
        if calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(app_module, "_share_sweep_once", exploding_tick)
    monkeypatch.setattr(app_module.asyncio, "sleep", no_sleep)

    with pytest.raises(asyncio.CancelledError):
        await app_module._share_sweep_loop(app)  # pyright: ignore[reportPrivateUsage]

    assert calls == 2
    status = app.state.share_sweep_status
    assert isinstance(status, plex_access_service.ShareSweepStatus)
    assert status.state == "error"
    assert status.last_error_type == "RuntimeError"


# --------------------------------------------------------------------------- #
# Entitlement capture wiring (issue #484 PR-3)
# --------------------------------------------------------------------------- #
async def test_capture_is_declined_without_an_operator_verified_anchor(
    app: FastAPI, seed: SeedFn, caplog: pytest.LogCaptureFixture
) -> None:
    """An upgraded install (url + token, no cached identifier) resolves an
    identifier by probing ``/identity`` live -- but that probe is NOT the anchor.

    Every writer of ``plex_machine_identifier`` commits it only after the full
    verification ladder (``/identity`` derive + an AUTHENTICATED ``list_sections``
    + the ownership assertion), because ``/identity`` is unauthenticated and
    reachability alone would bless a replaced server. A background sweep must not
    route around that, so it declines capture rather than backfilling the anchor
    itself -- and, critically, never writes the setting.

    Declining is not the same as going quiet, though: the skipped users are
    counted and the REASON is reported, because three permanent zeros on an
    otherwise ``ok`` sweep are indistinguishable from a healthy tick that had
    nothing to capture (north star #3).
    """
    await seed(initialized=True)
    async with app.state.sessionmaker() as session:
        store = SettingsStore(session)
        await store.set("plex_url", "http://plex.local:32400")
        await store.set("plex_token", "service-token")
        await session.commit()
    user_id = await _signed_in_user(app)
    await _use_transport(app, _plex_tv_transport([_server_resource(_MACHINE_ID)]))

    # The verdict work runs normally against the live-probed identity.
    with caplog.at_level("WARNING"):
        assert await _tick(app) == 1
    status = app.state.share_sweep_status
    assert status.authorized == 1
    assert status.captured == 0
    assert status.capture_failed == 0
    # Counted, not silently absent -- and the reason names the remedy.
    assert status.capture_skipped == 1
    assert status.capture_unavailable == "no_server_anchor"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "entitlement capture did not run this tick" in message and "no_server_anchor" in message
        for message in warnings
    )

    async with app.state.sessionmaker() as session:
        user = await session.get(User, user_id)
        assert user is not None
        assert user.share_state == "authorized"
        assert user.entitled_section_keys is None
        # The anchor was NOT silently backfilled from the unauthenticated probe.
        assert await SettingsStore(session).get(PLEX_MACHINE_ID_SETTING) is None


async def test_one_tick_captures_the_authorized_user_and_signs_out_the_revoked_one(
    app: FastAPI, seed: SeedFn
) -> None:
    """The composed success path (#572): a tick that BOTH stores a capture and
    signs someone out, through the real ``_entitlement_capture_context`` wiring.

    Every other test here pins one half in isolation, and none asserted a
    ``captured`` count above zero at this layer -- so the wiring that #484 PR-4/5
    will gate on had no test proving it ever produced a stored snapshot. This
    one holds a user whose share is live next to one whose share is gone, and
    checks each got exactly their own outcome.
    """
    await seed(initialized=True)
    await _configure_server(app)
    kept_id = await _signed_in_user(app, username="kept", token="kept-token")  # noqa: S106
    revoked_id = await _signed_in_user(app, username="revoked", token="revoked-token")  # noqa: S106
    await _use_transport(
        app,
        _plex_tv_transport(
            {
                "kept-token": [_server_resource(_MACHINE_ID)],
                "revoked-token": [_server_resource("some-other-server")],
            },
            sections=_sections("1", "3"),
        ),
    )

    assert await _tick(app) == 2

    status = app.state.share_sweep_status
    assert status.state == "ok"
    assert status.authorized == 1
    assert status.share_revoked == 1
    assert status.signed_out == 1
    assert status.sessions_revoked == 1
    assert status.captured == 1
    assert status.capture_failed == 0
    assert status.capture_skipped == 0
    assert status.capture_anchor_blocked == 0
    assert status.capture_unavailable is None

    assert await _live_sessions(app, kept_id) == 1
    assert await _live_sessions(app, revoked_id) == 0
    async with app.state.sessionmaker() as session:
        kept = await session.get(User, kept_id)
        assert kept is not None
        assert kept.share_state == "authorized"
        assert kept.entitled_section_keys == ["1", "3"]
        assert kept.entitlements_machine_id == _MACHINE_ID
        revoked = await session.get(User, revoked_id)
        assert revoked is not None
        assert revoked.share_state == "share_revoked"
        # A capture is a snapshot of ACCESS; a user with none has nothing to capture.
        assert revoked.entitled_section_keys is None
