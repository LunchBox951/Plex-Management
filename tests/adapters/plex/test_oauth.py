"""Plex hosted sign-in adapter: PIN flow, account/resources and owner checks."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from plex_manager.adapters.plex.oauth import (
    PlexOAuthClient,
    PlexOAuthError,
    PlexOAuthPending,
    owner_has_server,
)

_CLIENT_ID = "plex-manager-test-client"
_USER_TOKEN = "plex-user-token"  # noqa: S105 - fake token used by MockTransport tests


def _json_response(payload: object) -> httpx.Response:
    return httpx.Response(200, json=payload)


@pytest.mark.asyncio
async def test_create_pin_posts_client_headers_and_builds_auth_url() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.host == "plex.tv"
        assert request.url.path == "/api/v2/pins"
        assert request.url.params["strong"] == "true"
        assert request.headers["X-Plex-Product"] == "Plex Manager"
        assert request.headers["X-Plex-Client-Identifier"] == _CLIENT_ID
        return _json_response({"id": 123, "code": "ABCD", "expiresIn": 600})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexOAuthClient(http, client_identifier=_CLIENT_ID)
        pin = await client.create_pin(return_url="http://test/auth/plex/callback")

    assert len(seen) == 1
    assert pin.pin_id == 123
    assert pin.code == "ABCD"
    assert pin.expires_in == 600
    assert "clientID=plex-manager-test-client" in pin.auth_url
    assert "code=ABCD" in pin.auth_url
    assert "forwardUrl=http%3A%2F%2Ftest%2Fauth%2Fplex%2Fcallback" in pin.auth_url


@pytest.mark.asyncio
async def test_poll_pin_without_token_raises_pending() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v2/pins/123"
        return _json_response({"id": 123, "code": "ABCD", "authToken": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexOAuthClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexOAuthPending):
            await client.poll_pin(123)


@pytest.mark.asyncio
async def test_poll_pin_returns_auth_token_without_logging_it() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/pins/123"
        return _json_response({"id": 123, "code": "ABCD", "authToken": _USER_TOKEN})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexOAuthClient(http, client_identifier=_CLIENT_ID)
        token = await client.poll_pin(123)

    assert token == _USER_TOKEN
    assert _USER_TOKEN not in repr(client)


@pytest.mark.asyncio
async def test_fetch_account_and_resources_parse_json_shapes() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Plex-Token"] == _USER_TOKEN
        if request.url.path == "/users/account.json":
            return _json_response(
                {
                    "user": {
                        "id": 42,
                        "username": "owner",
                        "email": "owner@example.test",
                        "thumb": "http://plex/avatar.jpg",
                    }
                }
            )
        if request.url.path == "/api/resources":
            return _json_response(
                [
                    {
                        "name": "Home Plex",
                        "clientIdentifier": "server-machine-id",
                        "owned": True,
                    }
                ]
            )
        raise AssertionError(request.url.path)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexOAuthClient(http, client_identifier=_CLIENT_ID)
        account = await client.fetch_account(_USER_TOKEN)
        resources = await client.fetch_resources(_USER_TOKEN)

    assert account.plex_id == 42
    assert account.username == "owner"
    assert account.email == "owner@example.test"
    assert account.avatar_url == "http://plex/avatar.jpg"
    assert resources[0].client_identifier == "server-machine-id"
    assert resources[0].owned is True


@pytest.mark.asyncio
async def test_fetch_server_identity_uses_service_token_header() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://plex.local:32400/identity"
        assert request.headers["X-Plex-Token"] == "service-token"
        return _json_response({"MediaContainer": {"machineIdentifier": "server-machine-id"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexOAuthClient(http, client_identifier=_CLIENT_ID)
        identity = await client.fetch_server_identity("http://plex.local:32400", "service-token")

    assert identity == "server-machine-id"


def test_owner_has_server_requires_owned_resource_with_matching_machine_id() -> None:
    resources = [
        {
            "name": "Shared Server",
            "clientIdentifier": "server-machine-id",
            "owned": False,
        },
        {
            "name": "Owned Server",
            "clientIdentifier": "server-machine-id",
            "owned": True,
        },
    ]

    parsed = PlexOAuthClient.parse_resources(resources)
    assert owner_has_server(parsed, "server-machine-id") is True
    assert owner_has_server(parsed, "other-machine-id") is False


@pytest.mark.parametrize(
    ("raw_owned", "expected"),
    [
        # Real booleans.
        (True, True),
        (False, False),
        # Ints (the XML-derived numeric shape).
        (1, True),
        (0, False),
        # Strings, in every casing plex.tv emits.
        ("1", True),
        ("0", False),
        ("true", True),
        ("false", False),
        ("True", True),
        ("TRUE", True),
        ("False", False),
        (" true ", True),
        (" 1 ", True),
        # Unexpected shapes fail CLOSED (never mis-grant ownership).
        (2, False),
        (-1, False),
        ("yes", False),
        ("owned", False),
        ("", False),
        (None, False),
    ],
)
def test_parse_resources_owned_accepts_every_plex_boolean_encoding(
    raw_owned: object, expected: bool
) -> None:
    """``owned`` arrives as a bool, an int (1/0), or a string ("1"/"true"/...),
    because plex.tv resource payloads are XML-derived. All truthy encodings must
    register as owned; anything unexpected fails CLOSED to False."""
    parsed = PlexOAuthClient.parse_resources(
        [{"name": "S", "clientIdentifier": "server-machine-id", "owned": raw_owned}]
    )
    assert parsed[0].owned is expected


def test_owner_has_server_accepts_numeric_owned_encoding() -> None:
    """E2E-critical: the REAL owner's resource commonly encodes ``owned`` as ``1``
    (or ``"1"``). It must still register as owning the configured server — else the
    owner signs in with permissions=0 and is locked out of every require_admin
    route."""
    resources = [
        {"name": "Shared", "clientIdentifier": "server-machine-id", "owned": "0"},
        {"name": "Owned Server", "clientIdentifier": "server-machine-id", "owned": 1},
    ]

    parsed = PlexOAuthClient.parse_resources(resources)
    assert owner_has_server(parsed, "server-machine-id") is True
    assert owner_has_server(parsed, "other-machine-id") is False


@pytest.mark.asyncio
async def test_auth_errors_exclude_secret_values() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexOAuthClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexOAuthError) as excinfo:
            await client.fetch_account(_USER_TOKEN)

    assert _USER_TOKEN not in str(excinfo.value)
    assert "401" in str(excinfo.value)


def test_pin_expiry_decodes_from_seconds() -> None:
    now = datetime(2026, 7, 4, tzinfo=UTC)
    pin = PlexOAuthClient.parse_pin({"id": 123, "code": "ABCD", "expiresIn": 120}, now=now)

    assert pin.expires_at.isoformat() == "2026-07-04T00:02:00+00:00"
