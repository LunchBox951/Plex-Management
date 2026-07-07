"""The Plex-first setup wizard API — the full owner-driven flow.

Exercises the new setup backend end to end against the real app + middleware:

* ``GET /api/v1/setup/plex/servers`` enumerates the signed-in admin's OWNED
  servers, each connection probed at ``{uri}/identity`` (reachable or not) — never
  failing the whole listing on one dead connection.
* ``POST /api/v1/setup/validate/plex`` proves the candidate server AND asserts the
  probed ``machineIdentifier`` is among the admin's owned resources (else 403).
* ``POST /api/v1/setup/complete`` is keyless: it CAS-claims ``initialized``, stores
  the chosen ``plex_machine_identifier``, defaults ``plex_token`` to the admin's
  stored OAuth token, preserves the sign-in claim's ``setup_started_at``, and never
  mints or discloses an app key.

The plex.tv fixtures mirror the real ``api/v2`` JSON shapes (a FLAT ``/user``
object, a ``/resources`` ARRAY), matching ``tests/web/test_auth_plex.py`` and
``tests/adapters/plex/test_oauth.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.config import get_settings
from plex_manager.models import AuthSession, SystemSettings, User
from plex_manager.services import path_visibility
from plex_manager.web.deps import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    PLEX_MACHINE_ID_SETTING,
    SESSION_COOKIE_NAME,
    SettingsStore,
    hash_session_token,
)
from plex_manager.web.routers import auth as auth_module

SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_MACHINE_ID = "apollo-machine-id"
_TOKEN = "browser-oauth-token"  # noqa: S105 - fake token used by MockTransport tests
_SESSION_TOKEN = "flow-session-token"  # noqa: S105 - a test cookie value, not a credential
_CSRF_TOKEN = "flow-csrf-token"  # noqa: S105 - a test CSRF value, not a credential
_API_KEY = "flow-app-api-key"


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

# A local (LAN) connection AND a remote plex.direct one — the two shapes plex.tv
# advertises for an owned server; localhost:32400 is the local connection.
_LOCAL_CONN: dict[str, object] = {
    "protocol": "http",
    "address": "127.0.0.1",
    "port": 32400,
    "uri": "http://localhost:32400",
    "local": True,
    "relay": False,
}
_REMOTE_CONN: dict[str, object] = {
    "protocol": "https",
    "address": "203.0.113.7",
    "port": 32400,
    "uri": "https://apollo.plex.direct:32400",
    "local": False,
    "relay": False,
}

_A_PLAYER: dict[str, object] = {
    "name": "A Player",
    "clientIdentifier": "player-1",
    "provides": "client,player",
    "owned": True,
    "connections": [],
}


def _owned_server(
    machine_id: str = _MACHINE_ID, *, connections: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "name": "Apollo",
        "product": "Plex Media Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [_LOCAL_CONN, _REMOTE_CONN] if connections is None else connections,
    }


def _shared_server(machine_id: str) -> dict[str, object]:
    return {
        "name": "SomeoneElses",
        "product": "Plex Media Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": False,
        "connections": [],
    }


_MOVIE_SECTION: dict[str, object] = {
    "key": "1",
    "title": "Movies",
    "type": "movie",
    "Location": [{"path": "/library/movies"}],
}


async def _use_transport(app: FastAPI, transport: httpx.MockTransport) -> None:
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=transport)


async def _seed_admin_session(
    sessionmaker_: SessionMaker,
    *,
    initialized: bool = False,
    plex_token: str | None = _TOKEN,
    setup_started_at: datetime | None = None,
) -> None:
    """Seed the singleton install row + an admin (owner) with a live session cookie."""
    async with sessionmaker_() as session:
        session.add(
            SystemSettings(
                initialized=initialized,
                setup_started_at=setup_started_at or datetime.now(UTC),
            )
        )
        user = User(
            plex_id=42, username="plex-owner", permissions=1, encrypted_plex_token=plex_token
        )
        session.add(user)
        await session.flush()
        session.add(
            AuthSession(
                user_id=user.id,
                token_hash=hash_session_token(_SESSION_TOKEN),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        await session.commit()


def _authenticate(client: httpx.AsyncClient) -> None:
    """Attach the seeded admin's session + CSRF cookies to the client jar."""
    client.cookies.set(SESSION_COOKIE_NAME, _SESSION_TOKEN)
    client.cookies.set(CSRF_COOKIE_NAME, _CSRF_TOKEN)


_CSRF_HEADERS = {CSRF_HEADER_NAME: _CSRF_TOKEN}


def _complete_body(movies_root: str = "/library/movies") -> dict[str, object]:
    # ``plex_token`` deliberately omitted: complete defaults it to the admin's
    # stored OAuth token. ``plex_machine_identifier`` is the wizard's chosen server.
    return {
        "plex_url": "http://plex.local:32400",
        "plex_machine_identifier": _MACHINE_ID,
        "prowlarr_url": "http://prowlarr.local:9696",
        "prowlarr_api_key": "prowlarr-key-xyz",
        "qbittorrent_url": "http://qb.local:8080",
        "qbittorrent_username": "admin",
        "qbittorrent_password": "qb-pass-xyz",
        "tmdb_api_key": "tmdb-key-xyz",
        "movies_root": movies_root,
    }


# --------------------------------------------------------------------------- #
# GET /setup/plex/servers — owned-server discovery + per-connection probe
# --------------------------------------------------------------------------- #
async def test_servers_lists_owned_server_with_probed_connections(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "plex.tv" and path == "/api/v2/resources":
            assert request.headers.get("X-Plex-Token")
            return httpx.Response(
                200, json=[_owned_server(), _shared_server("shared-x"), _A_PLAYER]
            )
        if path == "/identity":
            if host == "localhost":
                return httpx.Response(
                    200, json={"MediaContainer": {"machineIdentifier": _MACHINE_ID}}
                )
            raise httpx.ConnectError("connection refused", request=request)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.get("/api/v1/setup/plex/servers")

    assert response.status_code == 200
    servers = response.json()["servers"]
    # Only the OWNED server surfaces — the shared server and the player are filtered.
    assert [s["machine_identifier"] for s in servers] == [_MACHINE_ID]
    apollo = servers[0]
    assert apollo["name"] == "Apollo"
    # Connections carry the probe verdict: the LAN connection is reachable, the
    # remote plex.direct one is not — one dead connection never fails the listing.
    assert apollo["connections"] == [
        {
            "uri": "http://localhost:32400",
            "local": True,
            "relay": False,
            "status": "ok",
            "error_code": None,
        },
        {
            "uri": "https://apollo.plex.direct:32400",
            "local": False,
            "relay": False,
            "status": "unreachable",
            "error_code": "server_unreachable_from_backend",
        },
    ]


async def test_servers_marks_a_malformed_connection_uri_unreachable(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    # plex.tv can advertise a malformed connection uri (here an invalid port).
    # httpx raises ``InvalidURL`` — which is NOT an ``httpx.HTTPError`` — while
    # building the probe request, so one bad connection must be caught and marked
    # unreachable, never blow up the whole owned-server listing.
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)

    bad_conn: dict[str, object] = {
        "protocol": "http",
        "address": "apollo.plex.direct",
        "port": 32400,
        "uri": "http://apollo.plex.direct:notaport",
        "local": False,
        "relay": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_owned_server(connections=[_LOCAL_CONN, bad_conn])])
        if request.url.path == "/identity" and request.url.host == "localhost":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": _MACHINE_ID}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.get("/api/v1/setup/plex/servers")

    assert response.status_code == 200
    connections = response.json()["servers"][0]["connections"]
    assert connections[1] == {
        "uri": "http://apollo.plex.direct:notaport",
        "local": False,
        "relay": False,
        "status": "unreachable",
        "error_code": "server_unreachable_from_backend",
    }


async def test_servers_requires_a_plex_signed_in_admin(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    # An API-key admin has no Plex user row, so there is no OAuth token to enumerate
    # servers with — an honest 409, never an opaque 500 or an empty list.
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/setup/plex/servers", headers={"X-Api-Key": _API_KEY})

    assert response.status_code == 409
    body = response.json()
    assert body["detail"] == "plex_account_required"
    assert body["message"] == "Server discovery needs a Plex-signed-in admin."
    assert body["hint"] == "Sign in with Plex first."


# --------------------------------------------------------------------------- #
# POST /setup/validate/plex — ownership-verified probe
# --------------------------------------------------------------------------- #
def _validate_transport(
    *,
    identity: str,
    resources: list[dict[str, object]],
    seen: list[str] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        host = request.url.host
        path = request.url.path
        if host == "plex.tv" and path == "/api/v2/resources":
            assert request.headers.get("X-Plex-Token")
            return httpx.Response(200, json=resources)
        if path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": identity}})
        if path == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [_MOVIE_SECTION]}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def test_validate_plex_owned_server_returns_machine_identifier(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    response = await client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["machine_identifier"] == _MACHINE_ID
    assert [lib["section_type"] for lib in body["libraries"]] == ["movie"]


async def test_validate_plex_foreign_server_is_not_owned(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    # The probed server reports a machine id the admin does NOT own (only shares).
    await _use_transport(
        app,
        _validate_transport(
            identity="shared999",
            resources=[_owned_server(), _shared_server("shared999")],
        ),
    )

    response = await client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "server_not_owned"


async def test_validate_plex_unreachable_server_is_502(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv":
            return httpx.Response(200, json=[_owned_server()])
        raise httpx.ConnectError("connection refused", request=request)

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "server_unreachable_from_backend"


async def test_validate_plex_requires_a_plex_signed_in_admin(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers={"X-Api-Key": _API_KEY},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "plex_account_required"


# --------------------------------------------------------------------------- #
# POST /setup/complete — keyless, machine-id storing, claim-preserving
# --------------------------------------------------------------------------- #
async def test_complete_requires_a_session(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=False)

    response = await client.post("/api/v1/setup/complete", json=_complete_body())

    assert response.status_code == 401
    assert response.json()["detail"] == "session_required"


async def test_complete_is_keyless_and_stores_the_machine_id(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    # complete re-derives the machine id from the submitted server's /identity and
    # re-asserts ownership against the admin's plex.tv resources (the honest path).
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    response = await client.post(
        "/api/v1/setup/complete", json=_complete_body(str(tmp_path)), headers=_CSRF_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is True
    # Keyless: no app key is minted or disclosed anywhere in the response.
    assert "app_api_key" not in body

    async with sessionmaker_() as session:
        store = SettingsStore(session)
        # The wizard's chosen server is persisted under the shared settings key.
        assert await store.get(PLEX_MACHINE_ID_SETTING) == _MACHINE_ID
        # ``plex_token`` was omitted from the body, so it defaults to the admin's
        # stored OAuth token — never a blank or a placeholder.
        assert await store.get("plex_token") == _TOKEN
        assert await store.get("plex_url") == "http://plex.local:32400"


async def test_complete_preserves_the_sign_in_claim_timestamp(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    # The pre-init sign-in stamped ``setup_started_at``; complete only sets
    # ``setup_completed_at`` and must never overwrite the claim.
    claimed_at = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    await _seed_admin_session(sessionmaker_, setup_started_at=claimed_at)
    _authenticate(client)
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    response = await client.post(
        "/api/v1/setup/complete", json=_complete_body(str(tmp_path)), headers=_CSRF_HEADERS
    )
    assert response.status_code == 200

    async with sessionmaker_() as session:
        row = await session.get(SystemSettings, 1)
        assert row is not None
        assert row.initialized is True
        assert row.setup_completed_at is not None
        started = row.setup_started_at
        assert started is not None
        assert started.replace(tzinfo=UTC) == claimed_at


async def test_complete_is_rejected_after_init(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    first = await client.post(
        "/api/v1/setup/complete", json=_complete_body(str(tmp_path)), headers=_CSRF_HEADERS
    )
    assert first.status_code == 200
    second = await client.post(
        "/api/v1/setup/complete", json=_complete_body(str(tmp_path)), headers=_CSRF_HEADERS
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "already_initialized"


# --------------------------------------------------------------------------- #
# POST /setup/complete never trusts the caller's machine id (it re-derives it)
# --------------------------------------------------------------------------- #
async def test_complete_ignores_a_forged_machine_id_and_stores_the_derived_one(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    """A direct API caller pairing server-X creds with server-Y's machine id must
    not poison the stored identity: /complete probes the SUBMITTED server's
    /identity itself and persists that derived id — the body's id is advisory."""
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    body = {**_complete_body(str(tmp_path)), "plex_machine_identifier": "forged-other-server-id"}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_CSRF_HEADERS)

    assert response.status_code == 200
    async with sessionmaker_() as session:
        # The DERIVED id won; the forged claim was never persisted.
        assert await SettingsStore(session).get(PLEX_MACHINE_ID_SETTING) == _MACHINE_ID


async def test_complete_rejects_a_server_the_admin_does_not_own(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    """The submitted server derives to a machine id the admin only has SHARED
    access to: the same 403 ``server_not_owned`` as validate/plex — and, because
    the check runs BEFORE the claim, the install is left fully unclaimed (no
    half-initialized row, no stored creds)."""
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    await _use_transport(
        app,
        _validate_transport(
            identity="shared999",
            resources=[_owned_server(), _shared_server("shared999")],
        ),
    )

    response = await client.post(
        "/api/v1/setup/complete", json=_complete_body(), headers=_CSRF_HEADERS
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "server_not_owned"
    async with sessionmaker_() as session:
        row = await session.get(SystemSettings, 1)
        assert row is not None
        assert row.initialized is False  # the one-shot claim was never consumed
        assert await SettingsStore(session).get(PLEX_MACHINE_ID_SETTING) is None


async def test_complete_rejects_a_reachable_server_that_rejects_the_token(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    """/identity is UNAUTHENTICATED, so the machine-id derivation alone cannot
    prove a direct API caller's explicit ``plex_token`` override: a reachable
    server that REJECTS the resolved token must fail /complete with the 422
    ``plex_token_invalid`` envelope — never a flipped-``initialized`` install
    whose every later library call fails. The one-shot claim is untouched, so
    the operator retries with the right token."""
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "plex.tv" and path == "/api/v2/resources":
            return httpx.Response(200, json=[_owned_server()])
        if path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": _MACHINE_ID}})
        if path == "/library/sections":
            return httpx.Response(401)  # reachable, but the token is refused
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    await _use_transport(app, httpx.MockTransport(handler))

    body = {**_complete_body(), "plex_token": "wrong-service-token"}
    response = await client.post("/api/v1/setup/complete", json=body, headers=_CSRF_HEADERS)

    assert response.status_code == 422
    assert response.json()["detail"] == "plex_token_invalid"
    async with sessionmaker_() as session:
        row = await session.get(SystemSettings, 1)
        assert row is not None
        assert row.initialized is False  # the one-shot claim was never consumed
        assert await SettingsStore(session).get(PLEX_MACHINE_ID_SETTING) is None


async def test_complete_unreachable_submitted_server_is_502_and_unclaimed(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    """The /identity re-derivation uses the same honest 502 envelope as
    validate/plex when the submitted server is unreachable — and never consumes
    the one-shot claim, so the operator can retry after fixing the URL."""
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv":
            return httpx.Response(200, json=[_owned_server()])
        raise httpx.ConnectError("connection refused", request=request)

    await _use_transport(app, httpx.MockTransport(handler))

    response = await client.post(
        "/api/v1/setup/complete", json=_complete_body(), headers=_CSRF_HEADERS
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "server_unreachable_from_backend"
    async with sessionmaker_() as session:
        row = await session.get(SystemSettings, 1)
        assert row is not None
        assert row.initialized is False


# --------------------------------------------------------------------------- #
# POST /setup/complete — every submitted library root must be visible to THIS
# container (issue #132), gated BEFORE the one-shot claim
# --------------------------------------------------------------------------- #
async def test_complete_422_when_a_root_is_not_visible(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker
) -> None:
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    response = await client.post(
        "/api/v1/setup/complete",
        json=_complete_body("/nope/does/not/exist"),
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "library_root_unreachable"
    async with sessionmaker_() as session:
        row = await session.get(SystemSettings, 1)
        assert row is not None
        assert row.initialized is False  # the one-shot claim was never consumed
        assert await SettingsStore(session).get("movies_root") is None


async def test_complete_remaps_a_host_root_to_the_container_path(
    client: httpx.AsyncClient,
    app: FastAPI,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    # Library roots are remapped under the LIBRARY mounts only (never /downloads).
    monkeypatch.setattr(path_visibility, "KNOWN_LIBRARY_MOUNTS", (str(mount),))
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    response = await client.post(
        "/api/v1/setup/complete",
        json=_complete_body("/definitely-not-a-real-host-path/Media/Movies"),
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 200
    async with sessionmaker_() as session:
        assert await SettingsStore(session).get("movies_root") == str(mount / "Movies")


# --------------------------------------------------------------------------- #
# GET /setup/status — install flag only, never an app key
# --------------------------------------------------------------------------- #
async def test_status_has_no_app_api_key_field(client: httpx.AsyncClient, seed: SeedFn) -> None:
    await seed(initialized=False)

    response = await client.get("/api/v1/setup/status")

    assert response.status_code == 200
    body = response.json()
    assert body["initialized"] is False
    assert body["setup_token_required"] is False
    # The one-time-key model is gone: status never carried an app key and the field
    # is dropped entirely.
    assert "app_api_key" not in body


# --------------------------------------------------------------------------- #
# Optional hardening token — still enforced pre-init when configured
# --------------------------------------------------------------------------- #
async def test_configured_setup_token_still_gates_pre_init(
    client: httpx.AsyncClient,
    app: FastAPI,
    sessionmaker_: SessionMaker,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "boot-token")
    get_settings.cache_clear()
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    # A configured hardening token is required BEFORE the session check pre-init.
    missing = await client.post(
        "/api/v1/setup/complete", json=_complete_body(str(tmp_path)), headers=_CSRF_HEADERS
    )
    assert missing.status_code == 401
    assert missing.json()["detail"] == "invalid_setup_token"

    ok = await client.post(
        "/api/v1/setup/complete",
        json=_complete_body(str(tmp_path)),
        headers={**_CSRF_HEADERS, "X-Setup-Token": "boot-token"},
    )
    assert ok.status_code == 200


# --------------------------------------------------------------------------- #
# Post-init sign-in reads the stored machine id (no /identity re-probe)
# --------------------------------------------------------------------------- #
async def test_post_init_sign_in_uses_the_stored_machine_id(
    client: httpx.AsyncClient, app: FastAPI, sessionmaker_: SessionMaker, tmp_path: Path
) -> None:
    await _seed_admin_session(sessionmaker_)
    _authenticate(client)
    # complete itself probes /identity once (deriving the id it stores) — the
    # assertion below is that the POST-INIT SIGN-IN never re-probes it.
    await _use_transport(
        app, _validate_transport(identity=_MACHINE_ID, resources=[_owned_server()])
    )

    complete = await client.post(
        "/api/v1/setup/complete", json=_complete_body(str(tmp_path)), headers=_CSRF_HEADERS
    )
    assert complete.status_code == 200

    # A fresh sign-in now resolves admin from the STORED machine id — the backend
    # never re-probes the Plex server's /identity.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        host = request.url.host
        path = request.url.path
        if host == "plex.tv" and path == "/api/v2/user":
            return httpx.Response(200, json=_OWNER_USER)
        if host == "plex.tv" and path == "/api/v2/resources":
            return httpx.Response(200, json=[_owned_server()])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    await _use_transport(app, httpx.MockTransport(handler))
    client.cookies.clear()  # sign in fresh, not on the seeded setup session

    signin = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})

    assert signin.status_code == 200
    assert signin.json()["user"]["is_admin"] is True
    assert "/identity" not in seen
