"""Plex.tv verification adapter: v2 JSON account/resources/identity + typed errors.

The fixtures below mirror the real plex.tv ``api/v2`` JSON payload shapes
(cross-checked against python-plexapi and Overseerr's parsers): ``GET
/api/v2/user`` returns a FLAT JSON object, and ``GET /api/v2/resources`` returns
a JSON ARRAY of devices with REAL booleans and a ``connections`` list. The
previous implementation failed in production because its mocks returned JSON
from an XML-only endpoint (``/api/resources`` / ``/users/account.json``), so the
parser was never exercised against the shapes plex.tv actually serves. These
fixtures pin the real shapes so that drift cannot ship again.
"""

from __future__ import annotations

import httpx
import pytest

from plex_manager.adapters.plex.oauth import (
    _ARRAY_BODY_KEY,  # pyright: ignore[reportPrivateUsage]
    PlexAccount,
    PlexResource,
    PlexTvClient,
    PlexVerifyError,
    account_server_resource,
    find_owned_server,
    owned_servers,
)

_CLIENT_ID = "plex-manager-test-client"
_USER_TOKEN = "plex-user-token"  # noqa: S105 - fake token used by MockTransport tests


# Real shape: GET https://plex.tv/api/v2/user returns a FLAT JSON object.
V2_USER = {
    "id": 173000000,
    "uuid": "8f21ac4c1e2c9a3b",
    "username": "lunchbox",
    "title": "lunchbox",
    "email": "owner@example.com",
    "thumb": "https://plex.tv/users/8f21ac4c1e2c9a3b/avatar?c=1751500000",
}

# Real shape: GET https://plex.tv/api/v2/resources?includeHttps=1 returns a JSON ARRAY
# of devices with REAL booleans and a connections list.
V2_RESOURCES = [
    {
        "name": "Apollo",
        "product": "Plex Media Server",
        "clientIdentifier": "abc123machine",
        "provides": "server",
        "owned": True,
        "connections": [
            {
                "protocol": "http",
                "address": "127.0.0.1",
                "port": 32400,
                "uri": "http://127.0.0.1:32400",
                "local": True,
                "relay": False,
                "IPv6": False,
            },
            {
                "protocol": "https",
                "address": "203.0.113.7",
                "port": 32400,
                "uri": "https://203-0-113-7.abc.plex.direct:32400",
                "local": False,
                "relay": False,
                "IPv6": False,
            },
        ],
    },
    {
        "name": "SomeoneElses",
        "clientIdentifier": "shared999",
        "provides": "server",
        "owned": False,
        "connections": [],
    },
    {
        "name": "A Player",
        "clientIdentifier": "player1",
        "provides": "client,player",
        "owned": True,
        "connections": [],
    },
]


async def _fetch_resources(payload: object) -> list[PlexResource]:
    """Drive ``fetch_resources`` against a mock transport returning ``payload``."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        return await client.fetch_resources(_USER_TOKEN)


async def test_fetch_account_parses_v2_user() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.url.host == "plex.tv"
        assert request.url.path == "/api/v2/user"
        return httpx.Response(200, json=V2_USER)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        account = await client.fetch_account(_USER_TOKEN)

    assert account == PlexAccount(
        plex_id=173000000,
        username="lunchbox",
        email="owner@example.com",
        avatar_url="https://plex.tv/users/8f21ac4c1e2c9a3b/avatar?c=1751500000",
    )
    assert seen[0].headers["X-Plex-Token"] == _USER_TOKEN
    assert seen[0].headers["X-Plex-Client-Identifier"] == _CLIENT_ID
    assert seen[0].headers["Accept"] == "application/json"


async def test_fetch_account_401_maps_to_token_invalid() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_account(_USER_TOKEN)

    assert excinfo.value.code == "plex_token_invalid"


async def test_fetch_resources_parses_v2_array() -> None:
    resources = await _fetch_resources(V2_RESOURCES)

    assert len(resources) == 3
    apollo = resources[0]
    assert apollo.name == "Apollo"
    assert apollo.owned is True
    assert apollo.provides == ("server",)
    assert len(apollo.connections) == 2
    assert [conn.local for conn in apollo.connections] == [True, False]
    assert all(conn.port == 32400 for conn in apollo.connections)
    assert apollo.connections[0].uri == "http://127.0.0.1:32400"


async def test_fetch_resources_hits_v2_not_v1() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=V2_RESOURCES)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        await client.fetch_resources(_USER_TOKEN)

    # Regression pin for the shipped bug: the old code hit /api/resources (v1, XML).
    assert seen[0].url.host == "plex.tv"
    assert seen[0].url.path == "/api/v2/resources"


async def test_fetch_resources_valid_empty_array_returns_empty_list() -> None:
    # A genuine empty array is a VALID authorization signal (the account has zero
    # server resources): it must parse to [] cleanly, not raise. Callers read [] as
    # "not authorized for the configured server" (#296).
    resources = await _fetch_resources([])
    assert resources == []


@pytest.mark.parametrize("body", [{}, {"error": "nope"}, {"resources": []}, {"items": []}])
async def test_fetch_resources_non_array_shape_is_bad_response(body: object) -> None:
    # NOTE the {"items": []} case: an OBJECT body carrying a public "items" list
    # must not impersonate the array wrapper (parse_resources keys off the private
    # _ARRAY_BODY_KEY sentinel only _request_json can synthesize).
    # A 2xx body that is NOT the expected JSON array (an error object, a wrapped
    # shape, HTML that still parsed) is MALFORMED, not "zero resources". It must
    # raise bad_response rather than silently collapse to [] -- otherwise watchlist
    # revalidation would read it as STALE and DELETE the user's snapshot on a
    # transient plex.tv hiccup (#296).
    with pytest.raises(PlexVerifyError) as excinfo:
        await _fetch_resources(body)
    assert excinfo.value.code == "plex_tv_bad_response"


async def test_owned_servers_filters_provides_and_owned() -> None:
    resources = await _fetch_resources(V2_RESOURCES)

    owned = owned_servers(resources)

    # Only Apollo qualifies: SomeoneElses is not owned; A Player provides no server.
    assert [server.name for server in owned] == ["Apollo"]


async def test_find_owned_server_and_account_server_resource() -> None:
    resources = await _fetch_resources(V2_RESOURCES)

    apollo = find_owned_server(resources, "abc123machine")
    assert apollo is not None
    assert apollo.name == "Apollo"

    # The shared server is not owned, so find_owned_server rejects it...
    assert find_owned_server(resources, "shared999") is None
    # ...but account_server_resource still surfaces shared access.
    shared = account_server_resource(resources, "shared999")
    assert shared is not None
    assert shared.name == "SomeoneElses"


async def test_connect_error_maps_to_unreachable() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_account(_USER_TOKEN)

    assert excinfo.value.code == "plex_tv_unreachable_server"
    assert excinfo.value.diagnostics["host"] == "plex.tv"


async def test_fetch_account_http_error_maps_to_bad_response() -> None:
    """A plex.tv HTTP error (non-401/403) ARRIVED, so it is a bad response — not
    an unreachable host. Diagnostics carry the reachable host and the status."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_account(_USER_TOKEN)

    assert excinfo.value.code == "plex_tv_bad_response"
    assert excinfo.value.diagnostics["host"] == "plex.tv"
    assert excinfo.value.diagnostics["status"] == "500"


async def test_fetch_account_non_json_body_maps_to_bad_response() -> None:
    """A 200 whose body is not JSON reached us but is unusable — bad response,
    not unreachable. There is no HTTP error, so no ``status`` is reported."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>definitely not json</html>",
            headers={"content-type": "text/html"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_account(_USER_TOKEN)

    assert excinfo.value.code == "plex_tv_bad_response"
    assert excinfo.value.diagnostics["host"] == "plex.tv"
    assert "status" not in excinfo.value.diagnostics


async def test_fetch_account_redirect_with_json_body_is_never_returned_as_data() -> None:
    """A 3xx (e.g. a proxy/auth redirect in front of plex.tv) must be rejected like
    any other non-2xx (issue #87) — ``httpx.Response.is_error`` excludes 3xx, so the
    prior check would have read a redirect carrying a JSON body as legitimate
    identity data even though it never actually reached plex.tv. #122 fixed the
    other four adapter wrappers but deferred this fifth ``oauth.py`` site because it
    lived only on the then-unmerged PR #45 branch."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, json=V2_USER, headers={"Location": "/web/login"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_account(_USER_TOKEN)

    assert excinfo.value.code == "plex_tv_bad_response"
    assert excinfo.value.diagnostics["host"] == "plex.tv"
    assert excinfo.value.diagnostics["status"] == "302"


async def test_fetch_account_redirect_with_empty_body_is_status_aware_not_non_json() -> None:
    """A 3xx with an empty body must raise the SAME status-aware ``bad_response``
    error as a 3xx with a JSON body — not fall through to the JSON-decode failure
    path, which would report a misleading "non-JSON response" diagnosis (no
    ``status``) for what is actually a redirect being rejected."""

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/web/login"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_account(_USER_TOKEN)

    assert excinfo.value.code == "plex_tv_bad_response"
    assert excinfo.value.diagnostics["host"] == "plex.tv"
    assert excinfo.value.diagnostics["status"] == "302"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"id": 173000000},  # username/title missing
        {"username": "lunchbox"},  # id missing
    ],
)
def test_parse_account_missing_fields_maps_to_bad_response(payload: dict[str, object]) -> None:
    """A well-formed 200 whose JSON lacks id/username reached us but is unusable:
    plex.tv answered, just not with what we need — a bad response, not unreachable."""
    with pytest.raises(PlexVerifyError) as excinfo:
        PlexTvClient.parse_account(payload)

    assert excinfo.value.code == "plex_tv_bad_response"
    assert excinfo.value.diagnostics["host"] == "plex.tv"


async def test_fetch_server_identity_ok() -> None:
    async def ok_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/identity"
        assert request.headers["X-Plex-Token"] == "service-token"
        return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "abc123machine"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(ok_handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        identity = await client.fetch_server_identity("http://plex", "service-token")
    assert identity == "abc123machine"


async def test_fetch_server_identity_connect_error_is_unreachable() -> None:
    async def connect_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect_error_handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_server_identity("http://plex", "service-token")
    assert excinfo.value.code == "server_unreachable_from_backend"


async def test_fetch_server_identity_non_json_is_identity_failed() -> None:
    async def html_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html>not a plex server</html>", headers={"content-type": "text/html"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(html_handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_server_identity("http://plex", "service-token")
    assert excinfo.value.code == "server_identity_failed"


@pytest.mark.parametrize("bad_token", ["tok\r\nx", "tök"])
async def test_fetch_account_rejects_unsafe_token(bad_token: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent for a header-unsafe token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_account(bad_token)

    exc = excinfo.value
    assert exc.code == "plex_token_invalid"
    assert bad_token not in str(exc)
    assert all(bad_token not in value for value in exc.diagnostics.values())


@pytest.mark.parametrize("bad_token", ["tok\r\nx", "tök"])
async def test_fetch_resources_rejects_unsafe_token(bad_token: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent for a header-unsafe token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_resources(bad_token)

    exc = excinfo.value
    assert exc.code == "plex_token_invalid"
    assert bad_token not in str(exc)
    assert all(bad_token not in value for value in exc.diagnostics.values())


@pytest.mark.parametrize("bad_token", ["tok\r\nx", "tök"])
async def test_fetch_server_identity_rejects_unsafe_token(bad_token: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent for a header-unsafe token")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_server_identity("http://plex", bad_token)

    exc = excinfo.value
    assert exc.code == "plex_token_invalid"
    assert bad_token not in str(exc)
    assert all(bad_token not in value for value in exc.diagnostics.values())


async def test_diagnostics_never_contain_token() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = PlexTvClient(http, client_identifier=_CLIENT_ID)
        with pytest.raises(PlexVerifyError) as excinfo:
            await client.fetch_account(_USER_TOKEN)

    exc = excinfo.value
    assert _USER_TOKEN not in str(exc)
    assert all(_USER_TOKEN not in value for value in exc.diagnostics.values())


@pytest.mark.parametrize(
    ("raw_owned", "expected"),
    [
        # Known truthy encodings across the bool/int/str shapes plex.tv has used.
        (True, True),
        (1, True),
        ("1", True),
        ("true", True),
        # Whitespace-tolerant truthy strings (matches _get_bool's strip()).
        (" true ", True),
        (" 1 ", True),
        # Known falsey encodings.
        (False, False),
        (0, False),
        ("false", False),
        (None, False),
        # Fail CLOSED on anything unexpected: ints other than 1, unknown/empty
        # strings must never mis-grant ownership.
        (2, False),
        (-1, False),
        ("yes", False),
        ("owned", False),
        ("", False),
    ],
)
def test_parse_resources_owned_boolean_tolerance(raw_owned: object, expected: bool) -> None:
    """``owned`` is a real bool in v2 JSON, but keep tolerating the historical XML
    encodings (int/str) and fail CLOSED on anything unexpected — the owner check
    must never mis-grant ownership."""
    payload = {
        _ARRAY_BODY_KEY: [
            {"name": "S", "clientIdentifier": "id", "provides": "server", "owned": raw_owned}
        ]
    }
    parsed = PlexTvClient.parse_resources(payload)
    assert parsed[0].owned is expected


def test_parse_resources_owned_missing_defaults_false() -> None:
    payload = {_ARRAY_BODY_KEY: [{"name": "S", "clientIdentifier": "id", "provides": "server"}]}
    parsed = PlexTvClient.parse_resources(payload)
    assert parsed[0].owned is False


def test_parse_connections_skips_entries_without_uri() -> None:
    """Connections must carry a ``uri`` to be usable; entries lacking one are dropped."""
    payload = {
        _ARRAY_BODY_KEY: [
            {
                "name": "S",
                "clientIdentifier": "id",
                "provides": "server",
                "owned": True,
                "connections": [
                    {"protocol": "http", "address": "10.0.0.1", "port": 32400},  # no uri
                    {"uri": "http://10.0.0.1:32400", "local": True, "port": 32400},
                ],
            }
        ]
    }
    parsed = PlexTvClient.parse_resources(payload)
    assert [conn.uri for conn in parsed[0].connections] == ["http://10.0.0.1:32400"]
