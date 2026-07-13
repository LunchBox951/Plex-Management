"""The single browser-token sign-in endpoint: ``POST /api/v1/auth/plex``.

The browser runs the plex.tv PIN flow itself and hands the resulting token to
this endpoint, which NEVER trusts the browser's claims: identity and server
ownership are re-derived server-side from plex.tv's v2 API before any user or
session row is written. Pre-init the first owner to sign in claims setup
(exclusively, via a CAS on ``system_settings.setup_started_at``); post-init an
account is admitted iff it has access to the configured server (admin iff it
owns it).

The plex.tv fixtures below mirror the real ``api/v2`` JSON payload shapes (a FLAT
``/api/v2/user`` object and a ``/api/v2/resources`` ARRAY), matching
``tests/adapters/plex/test_oauth.py`` so the router is exercised against the
shapes plex.tv actually serves.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.config import get_settings
from plex_manager.models import AuthSession, Setting, SystemSettings, User
from plex_manager.web.deps import SETUP_TOKEN_HEADER_NAME, SettingsStore
from plex_manager.web.routers import auth as auth_module

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "s3cr3t-app-key"
_TOKEN = "browser-obtained-plex-token"  # noqa: S105 - fake token used by MockTransport tests
_MACHINE_ID = "abc123machine"


# --------------------------------------------------------------------------- #
# Order-independence
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def reset_throttle() -> None:
    """Clear the in-process sign-in throttle so tests never leak attempt counts."""
    auth_module.reset_sign_in_throttle()


# --------------------------------------------------------------------------- #
# plex.tv v2 JSON fixtures (mirroring the real payload shapes)
# --------------------------------------------------------------------------- #
_OWNER_USER: dict[str, object] = {
    "id": 42,
    "uuid": "owner-uuid",
    "username": "plex-owner",
    "title": "plex-owner",
    "email": "owner@example.test",
    "thumb": "https://plex.tv/users/owner-uuid/avatar?c=1",
}

_SECOND_USER: dict[str, object] = {
    "id": 99,
    "uuid": "second-uuid",
    "username": "second-account",
    "title": "second-account",
    "email": "second@example.test",
}

_A_PLAYER: dict[str, object] = {
    "name": "A Player",
    "clientIdentifier": "player-1",
    "provides": "client,player",
    "owned": True,
    "connections": [],
}


def _owned_server(machine_id: str = _MACHINE_ID) -> dict[str, object]:
    return {
        "name": "Apollo",
        "product": "Plex Media Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [
            {
                "protocol": "https",
                "address": "203.0.113.7",
                "port": 32400,
                "uri": "https://203-0-113-7.abc.plex.direct:32400",
                "local": False,
                "relay": False,
            }
        ],
    }


def _shared_server(machine_id: str = _MACHINE_ID) -> dict[str, object]:
    return {
        "name": "SomeoneElses",
        "product": "Plex Media Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": False,
        "connections": [],
    }


def _plex_tv_transport(
    *,
    user: dict[str, object],
    resources: list[dict[str, object]],
    identity: str | None = None,
    seen: list[str] | None = None,
) -> httpx.MockTransport:
    """A MockTransport answering the plex.tv v2 endpoints (and optional /identity)."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        host = request.url.host
        path = request.url.path
        if host == "plex.tv" and path == "/api/v2/user":
            assert request.headers.get("X-Plex-Token")  # the submitted token is forwarded
            return httpx.Response(200, json=user)
        if host == "plex.tv" and path == "/api/v2/resources":
            assert request.headers.get("X-Plex-Token")
            return httpx.Response(200, json=resources)
        if path == "/identity":
            if identity is None:
                raise AssertionError(f"unexpected /identity request: {request.url}")
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": identity}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def _use_transport(app: FastAPI, transport: httpx.MockTransport) -> None:
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=transport)


async def _store_setting(sessionmaker_: SessionMaker, key: str, value: str) -> None:
    async with sessionmaker_() as session:
        await SettingsStore(session).set(key, value)
        await session.commit()


# --------------------------------------------------------------------------- #
# Pre-init: exclusive first claim
# --------------------------------------------------------------------------- #
async def test_pre_init_owner_claims_admin_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=False)
    await _use_transport(
        app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server(), _A_PLAYER])
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    body = response.json()
    assert body["auth_method"] == "plex_session"
    assert body["is_admin"] is True
    assert body["user"]["plex_id"] == 42
    assert body["user"]["is_admin"] is True
    assert response.cookies.get("plexmgr.session")
    assert response.cookies.get("plexmgr.csrf")

    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 42))).scalars().one()
        system = (await db.execute(select(SystemSettings))).scalars().one()
    assert user.permissions == 1
    # The verified token is persisted on the user row (EncryptedStr at rest).
    assert user.encrypted_plex_token == _TOKEN
    assert system.setup_started_at is not None


async def test_pre_init_no_owned_servers_rejected(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=False)
    # Only a shared (not-owned) server and a player — nothing this account OWNS.
    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_shared_server("other"), _A_PLAYER])
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 403
    assert response.json()["detail"] == "no_owned_servers"
    assert response.cookies.get("plexmgr.session") is None


async def test_pre_init_second_different_account_after_claim_rejected(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=False)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))
    first = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert first.status_code == 200

    # A DIFFERENT owner account signs in; the exclusive claim is already taken.
    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_owned_server("second-machine")])
    )
    second = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert second.status_code == 403
    assert second.json()["detail"] == "setup_already_claimed"
    assert second.cookies.get("plexmgr.session") is None


async def test_pre_init_same_account_resumes(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=False)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    first = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert first.status_code == 200
    # The SAME owner signing in again resumes the claim rather than being locked out.
    second = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert second.status_code == 200
    assert second.json()["user"]["is_admin"] is True


async def test_pre_init_concurrent_claim_loser_rejected(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=False)
    # Simulate the winning worker's claim landing first: setup_started_at already
    # stamped when a DIFFERENT account's request runs the CAS.
    async with sessionmaker_() as db:
        system = (await db.execute(select(SystemSettings))).scalars().one()
        system.setup_started_at = datetime.now(UTC)
        await db.commit()

    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_owned_server("second-machine")])
    )
    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 403
    assert response.json()["detail"] == "setup_already_claimed"


# --------------------------------------------------------------------------- #
# Pre-init: the optional PLEX_MANAGER_SETUP_TOKEN gates the FIRST-OWNER CLAIM
# --------------------------------------------------------------------------- #
async def test_pre_init_setup_token_gates_the_claim(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured setup token must be required BEFORE the exclusive claim.

    Without this, any owner of any Plex server could win the pre-init claim and
    permanently lock out the true owner. The token gates completion AND the claim.
    """
    await seed(initialized=False)
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
    get_settings.cache_clear()
    # A legitimate-looking owner token: the claim would succeed if the gate were
    # absent. The plex.tv transport must never be reached — the token check fires
    # before any account/resource fetch — so a hit here fails the test.
    seen: list[str] = []
    await _use_transport(
        app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()], seen=seen)
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_setup_token"
    assert response.cookies.get("plexmgr.session") is None
    # The gate short-circuits before any plex.tv call: no proxying for a caller
    # who cannot prove the setup token.
    assert seen == []
    # The claim never happened: no owner row, setup_started_at still unstamped.
    async with sessionmaker_() as db:
        assert (await db.execute(select(User).where(User.plex_id == 42))).scalars().first() is None
        system = (await db.execute(select(SystemSettings))).scalars().one()
    assert system.setup_started_at is None


async def test_pre_init_setup_token_valid_allows_the_claim(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the matching X-Setup-Token, the owner claims setup as normal."""
    await seed(initialized=False)
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
    get_settings.cache_clear()
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    response = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={SETUP_TOKEN_HEADER_NAME: "boot-token"},
    )

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True
    async with sessionmaker_() as db:
        system = (await db.execute(select(SystemSettings))).scalars().one()
    assert system.setup_started_at is not None


async def test_post_init_ignores_setup_token(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-init the token is never consulted: sign-in works without it.

    The token hardens ONLY the pre-init window; a configured token must not brick
    ordinary post-init sign-in (which carries no X-Setup-Token).
    """
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
    get_settings.cache_clear()
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True


# --------------------------------------------------------------------------- #
# Post-init: server-access gate
# --------------------------------------------------------------------------- #
async def test_post_init_owner_gets_admin_and_skips_identity(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    seen: list[str] = []
    await _use_transport(
        app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()], seen=seen)
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True
    # A stored machine identifier means the backend never re-probes /identity.
    assert "/identity" not in seen


async def test_post_init_shared_gets_limited_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_SECOND_USER, resources=[_shared_server()]))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is False
    # The client jar carries the freshly-minted session cookie into the next call.
    settings = await client.get("/api/v1/settings")
    assert settings.status_code == 403
    assert settings.json()["detail"] == "admin_required"

    async with sessionmaker_() as db:
        user = (await db.execute(select(User).where(User.plex_id == 99))).scalars().one()
    assert user.permissions == 0


async def test_post_init_no_access_rejected(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    # The account owns a server, but not the configured one.
    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_owned_server("different-machine")])
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 403
    assert response.json()["detail"] == "server_access_denied"
    assert response.cookies.get("plexmgr.session") is None


async def test_post_init_falls_back_to_identity_when_machine_id_unset(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    # A pre-rework DB has no stored machine id, only the Plex url/token.
    await _store_setting(sessionmaker_, "plex_url", "http://plex.local:32400")
    await _store_setting(sessionmaker_, "plex_token", "service-token")
    seen: list[str] = []
    await _use_transport(
        app,
        _plex_tv_transport(
            user=_OWNER_USER, resources=[_owned_server()], identity=_MACHINE_ID, seen=seen
        ),
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200
    assert response.json()["user"]["is_admin"] is True
    assert "/identity" in seen


async def test_post_init_service_not_configured_when_nothing_stored(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    # Neither a stored machine id nor a configured Plex url/token to probe.
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 409
    assert response.json()["detail"] == "service_not_configured"


# --------------------------------------------------------------------------- #
# plex.tv verification failures render the envelope (never a bare 500)
# --------------------------------------------------------------------------- #
async def test_plex_token_invalid_surfaces_envelope(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(401, json={"error": "unauthorized"})
        raise AssertionError(f"unexpected request: {request.url}")

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 502
    assert response.json()["detail"] == "plex_token_invalid"
    assert _TOKEN not in response.text
    assert response.cookies.get("plexmgr.session") is None


async def test_plex_tv_unreachable_surfaces_envelope(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 502
    assert response.json()["detail"] == "plex_tv_unreachable_server"
    assert _TOKEN not in response.text


async def test_error_body_never_leaks_token(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """No error path echoes the submitted token anywhere in the response body."""
    await seed(initialized=False)
    distinctive = "DISTINCTIVE-do-not-leak-4f2a"
    await _use_transport(
        app, _plex_tv_transport(user=_SECOND_USER, resources=[_shared_server("other")])
    )

    response = await client.post("/api/v1/auth/plex", json={"auth_token": distinctive})

    assert response.status_code == 403
    assert response.json()["detail"] == "no_owned_servers"
    assert distinctive not in response.text


# --------------------------------------------------------------------------- #
# Throttle
# --------------------------------------------------------------------------- #
async def test_sign_in_throttled_after_limit(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for _ in range(10):
        ok = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
        assert ok.status_code == 200
    throttled = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert throttled.status_code == 429
    assert throttled.json()["detail"] == "sign_in_throttled"


async def test_sign_in_throttle_default_ignores_x_forwarded_for(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """``trusted_proxy_hops`` defaults to 0: a caller-supplied X-Forwarded-For must
    NOT carve out extra budget beyond the direct-peer throttle."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for i in range(10):
        ok = await client.post(
            "/api/v1/auth/plex",
            json={"auth_token": _TOKEN},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        )
        assert ok.status_code == 200
    throttled = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": "10.0.0.99"},
    )

    assert throttled.status_code == 429


async def test_sign_in_throttle_trusted_hop_keys_on_forwarded_client(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``trusted_proxy_hops=1``, each distinct forwarded client gets its own
    budget -- fixing the global-cap risk of keying on ``request.client.host`` (the
    reverse proxy's own address) alone."""
    monkeypatch.setenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for _ in range(10):
        ok = await client.post(
            "/api/v1/auth/plex",
            json={"auth_token": _TOKEN},
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
        assert ok.status_code == 200
    throttled = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": "203.0.113.5"},
    )
    assert throttled.status_code == 429

    # A DIFFERENT forwarded client is unaffected -- its own budget, not the proxy's.
    other = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    assert other.status_code == 200

    # No X-Forwarded-For header at all still falls back to the direct peer rather
    # than raising -- a fresh budget, since nothing above hit that key.
    absent = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert absent.status_code == 200


async def test_sign_in_throttle_trusted_hop_falls_back_on_short_header(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A header shorter than the configured hop count cannot be trusted -- fall back
    to the direct peer rather than indexing into an attacker-shaped header."""
    monkeypatch.setenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", "2")
    get_settings.cache_clear()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for i in range(10):
        ok = await client.post(
            "/api/v1/auth/plex",
            json={"auth_token": _TOKEN},
            # Only ONE entry while hops=2 -- too short to trust, so every request
            # still falls back to (and shares) the direct-peer key.
            headers={"X-Forwarded-For": f"198.51.100.{i}"},
        )
        assert ok.status_code == 200
    throttled = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": "198.51.100.250"},
    )

    assert throttled.status_code == 429


async def test_sign_in_throttle_ignores_blank_entries_outside_trusted_suffix(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank/malformed entry in the CLIENT-CONTROLLED portion of the header (to
    the left of the trusted suffix) must not collapse every forwarded client onto
    the shared direct-peer key -- only the trusted suffix's own well-formedness
    gates the fallback. Otherwise an attacker could send a distinct blank-entry
    header on every request and force everyone behind the proxy to share one
    budget, recreating the exact global-cap lockout this throttle key exists to
    prevent."""
    monkeypatch.setenv("PLEX_MANAGER_TRUSTED_PROXY_HOPS", "1")
    get_settings.cache_clear()
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    for _ in range(10):
        ok = await client.post(
            "/api/v1/auth/plex",
            json={"auth_token": _TOKEN},
            # A blank leading (client-controlled) entry ahead of the proxy's own
            # trusted-suffix entry -- must still key on the trusted "203.0.113.5".
            headers={"X-Forwarded-For": ", 203.0.113.5"},
        )
        assert ok.status_code == 200
    throttled = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": ", 203.0.113.5"},
    )
    assert throttled.status_code == 429

    # A DIFFERENT trusted client, still preceded by a blank entry, has its own
    # budget rather than colliding on a shared fallback key.
    other = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"X-Forwarded-For": ", 203.0.113.10"},
    )
    assert other.status_code == 200


# --------------------------------------------------------------------------- #
# /me and /logout behavior preserved
# --------------------------------------------------------------------------- #
async def test_me_unauthenticated(client: httpx.AsyncClient, app: FastAPI, seed: SeedFn) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 200
    assert response.json()["authenticated"] is False


async def test_me_authenticated_after_sign_in(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    signin = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert signin.status_code == 200
    me = await client.get("/api/v1/auth/me")  # jar carries the session cookie

    assert me.status_code == 200
    body = me.json()
    assert body["authenticated"] is True
    assert body["auth_method"] == "plex_session"
    assert body["is_admin"] is True
    assert body["user"]["plex_id"] == 42


async def test_logout_revokes_session_cookie(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    signin = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    session_cookie = signin.cookies["plexmgr.session"]
    csrf_cookie = signin.cookies["plexmgr.csrf"]

    # The jar carries session + CSRF cookies; the header echoes the CSRF token.
    logout = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_cookie})
    assert logout.status_code == 204

    # Replay the now server-side-revoked session token: rejected on the merits,
    # not merely because logout cleared the cookie from the jar.
    client.cookies.set("plexmgr.session", session_cookie)
    settings = await client.get("/api/v1/settings")
    assert settings.status_code == 401


# --------------------------------------------------------------------------- #
# First-sign-in races: two concurrent racers on a clean DB must both succeed
# --------------------------------------------------------------------------- #
async def test_concurrent_client_id_create_race_loser_reuses_winner_id(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two simultaneous first-ever POST /auth/plex on a clean DB both observe no
    ``plex_oauth_client_identifier`` and both mint a candidate; ``settings.key``
    is UNIQUE, so only one can persist. The loser must complete its sign-in under
    the WINNER's device identity — never a 500, never a second identifier, and
    (the store-retry x create-once composition hazard) never an OVERWRITE that
    rotates the winner's committed identifier out from under it. This pins the
    BEHAVIOR, not the recovery mechanism (which lives in
    ``SettingsStore.set_if_absent``): the loser's initial row lookup is stubbed
    to MISS once — as it genuinely would mid-race, before the winner's commit
    became visible — so its INSERT collides on the real unique index."""
    await seed(initialized=False)
    # The WINNER's identifier is committed (its request got there first).
    await _store_setting(sessionmaker_, "plex_oauth_client_identifier", "winner-client-id")

    real_row = SettingsStore._row  # pyright: ignore[reportPrivateUsage]
    missed = {"n": 0}

    async def racing_row(self: SettingsStore, key: str) -> Setting | None:
        # The loser's FIRST lookup ran before the winner committed: miss once.
        if key == "plex_oauth_client_identifier" and missed["n"] == 0:
            missed["n"] = 1
            return None
        return await real_row(self, key)

    monkeypatch.setattr(SettingsStore, "_row", racing_row)

    # Capture the device identifier every plex.tv call actually carried.
    used_identifiers: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        used_identifiers.append(request.headers.get("X-Plex-Client-Identifier"))
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_OWNER_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_owned_server()])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert response.status_code == 200  # the loser signs in, never a 500
    assert response.json()["user"]["is_admin"] is True
    # Every plex.tv call ran under the WINNER's identifier — the loser adopted it.
    assert used_identifiers and set(used_identifiers) == {"winner-client-id"}
    async with sessionmaker_() as db:
        stored = await SettingsStore(db).get("plex_oauth_client_identifier")
    assert stored == "winner-client-id"  # the winner's identifier survived intact


async def test_concurrent_same_account_first_sign_in_yields_one_user_two_sessions(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent FIRST-TIME post-init sign-ins for the SAME Plex account both
    pass the no-row lookup and both INSERT; ``users.plex_id`` is UNIQUE, so the
    loser's flush raises ``IntegrityError``. The loser must recover onto the
    winner's row and still mint its session: both browsers end up signed in, ONE
    user row exists. Simulated like the request-dedup race tests: the winner's
    sign-in commits first, then the loser's first lookup is forced to MISS (as it
    would mid-race) so its INSERT genuinely collides on the unique index."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    await _use_transport(app, _plex_tv_transport(user=_OWNER_USER, resources=[_owned_server()]))

    winner = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert winner.status_code == 200

    real_find = auth_module.find_user_by_plex_id
    missed = {"n": 0}

    async def racing_find(session: AsyncSession, plex_id: int) -> User | None:
        # The loser looked BEFORE the winner committed: miss once, then real.
        if missed["n"] == 0:
            missed["n"] = 1
            return None
        return await real_find(session, plex_id)

    monkeypatch.setattr(auth_module, "find_user_by_plex_id", racing_find)
    client.cookies.clear()  # the loser is a separate browser, not the winner's session

    loser = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert loser.status_code == 200  # never a 500 — the same account merely raced itself
    assert loser.cookies.get("plexmgr.session")
    assert missed["n"] == 1  # the race path (miss -> collide -> recover) actually ran
    async with sessionmaker_() as db:
        users = (await db.execute(select(User).where(User.plex_id == 42))).scalars().all()
        assert len(users) == 1  # ONE shared user row — the loser adopted the winner's
        assert users[0].permissions == 1  # the recovery re-applied the admin decision
        session_rows = (
            (await db.execute(select(AuthSession).where(AuthSession.user_id == users[0].id)))
            .scalars()
            .all()
        )
    assert len(session_rows) == 2  # BOTH racers hold a live session


# --------------------------------------------------------------------------- #
# Recovery-key exchange: POST /auth/api-key trades X-Api-Key for the SAME cookie
# (CodeQL #263 — the key never needs JS-readable storage)
# --------------------------------------------------------------------------- #
async def test_api_key_exchange_mints_session_cookie(
    client: httpx.AsyncClient,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A valid recovery key is exchanged for the same HTTP-only session cookie the
    Plex flow issues — an admin recovery session with NO owning user."""
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})

    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is True
    assert body["auth_method"] == "api_key"
    assert body["is_admin"] is True
    assert body["user"] is None  # no Plex identity backs the recovery session
    assert response.cookies.get("plexmgr.session")
    assert response.cookies.get("plexmgr.csrf")

    # The minted row is a genuine recovery session: user_id NULL, not revoked.
    async with sessionmaker_() as db:
        rows = (await db.execute(select(AuthSession))).scalars().all()
    assert len(rows) == 1
    assert rows[0].user_id is None
    assert rows[0].revoked_at is None


async def test_api_key_exchange_cookie_authenticates_later_requests(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """After the exchange the browser needs NO X-Api-Key header: the cookie in the
    jar authenticates the admin-only settings read on its own."""
    await seed(initialized=True, app_api_key=_API_KEY)

    exchange = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})
    assert exchange.status_code == 200

    # No X-Api-Key header — the session cookie alone carries the request.
    settings = await client.get("/api/v1/settings")
    assert settings.status_code == 200

    me = await client.get("/api/v1/auth/me")
    assert me.json()["auth_method"] == "api_key"
    assert me.json()["is_admin"] is True


async def test_api_key_exchange_session_enforces_csrf_on_unsafe_methods(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """The recovery session is a COOKIE credential, so unsafe methods carry the
    double-submit CSRF check exactly like a Plex session — a missing token is a
    403, the echoed CSRF cookie succeeds (logout is the handy unsafe endpoint)."""
    await seed(initialized=True, app_api_key=_API_KEY)

    exchange = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})
    csrf_cookie = exchange.cookies["plexmgr.csrf"]

    # The jar carries the session + CSRF cookies but no X-CSRF-Token header.
    blocked = await client.post("/api/v1/auth/logout")
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "csrf_token_required"

    ok = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_cookie})
    assert ok.status_code == 204


async def test_api_key_exchange_wrong_key_rejected_no_cookie(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": "nope"})

    assert response.status_code == 401
    # A DISTINCT code from the expired-session 401 (issue #293 finding 2): the SPA
    # branches on it so a mistyped recovery key does not trip the global "session
    # expired -> bounce to Plex login" handler.
    assert response.json()["detail"] == "recovery_key_rejected"
    assert response.cookies.get("plexmgr.session") is None


async def test_api_key_exchange_missing_key_rejected(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post("/api/v1/auth/api-key")

    assert response.status_code == 401
    assert response.json()["detail"] == "recovery_key_rejected"


async def test_api_key_exchange_does_not_accept_plex_session(
    client: httpx.AsyncClient,
    app: FastAPI,
    seed: SeedFn,
    sessionmaker_: SessionMaker,
) -> None:
    """A non-owner Plex session must NOT be able to mint an ADMIN recovery session
    by calling the exchange with no key: the endpoint validates the recovery key
    specifically, never falling back to whatever cookie is already in the jar."""
    await seed(initialized=True, app_api_key=_API_KEY)
    await _store_setting(sessionmaker_, "plex_machine_identifier", _MACHINE_ID)
    # A shared (non-owner) account signs in: it holds a limited (non-admin) session.
    await _use_transport(app, _plex_tv_transport(user=_SECOND_USER, resources=[_shared_server()]))
    signin = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert signin.json()["is_admin"] is False

    # With that non-admin session cookie in the jar but NO recovery key header, the
    # exchange must refuse rather than hand out an admin recovery session.
    response = await client.post("/api/v1/auth/api-key")
    assert response.status_code == 401
    assert response.json()["detail"] == "recovery_key_rejected"


async def test_api_key_exchange_throttled_after_limit(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """The exchange shares the sign-in throttle: another session-minting write on
    the unauthenticated surface, braked the same way."""
    await seed(initialized=True, app_api_key=_API_KEY)

    for _ in range(10):
        ok = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})
        assert ok.status_code == 200
    throttled = await client.post("/api/v1/auth/api-key", headers={"X-Api-Key": _API_KEY})

    assert throttled.status_code == 429
    assert throttled.json()["detail"] == "sign_in_throttled"


async def test_openapi_advertises_both_cookie_and_apikey_auth(app: FastAPI) -> None:
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["APIKeyCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "plexmgr.session",
    }
    assert schemes["CSRFHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-CSRF-Token",
    }
    # A protected route advertises EITHER credential — a LIST of requirement
    # objects is OpenAPI's OR; a single object would mean both are required.
    logout_security = schema["paths"]["/api/v1/auth/logout"]["post"]["security"]
    assert {"APIKeyHeader": []} in logout_security
    assert {"APIKeyCookie": [], "CSRFHeader": []} in logout_security

    # Safe cookie-auth operations do not need CSRF.
    queue_security = schema["paths"]["/api/v1/queue"]["get"]["security"]
    assert {"APIKeyCookie": []} in queue_security


async def test_openapi_declares_api_key_header_on_recovery_exchange(app: FastAPI) -> None:
    """The recovery-key exchange sources ``X-Api-Key`` via the shared
    ``APIKeyHeader`` dependency (issue #293 finding 5), so the endpoint advertises
    the requirement in OpenAPI — a raw ``Request.headers.get`` left the contract
    silent and generated clients would omit the key and hit an undocumented 401."""
    schema = app.openapi()
    security = schema["paths"]["/api/v1/auth/api-key"]["post"]["security"]
    assert {"APIKeyHeader": []} in security
