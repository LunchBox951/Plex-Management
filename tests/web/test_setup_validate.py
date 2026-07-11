"""Setup ``validate/*`` endpoints — real adapter paths over a mock transport.

These prove the wiring (request body -> validator -> shared HTTP client), that the
#53 URL-shape validation still rejects a malformed base URL BEFORE any outbound
probe, and that ``validate/plex`` now asserts server OWNERSHIP against the signed-in
admin's plex.tv resources. Every probe is driven through the new admin-session auth
(``require_setup_admin``); the credential model itself is covered in
``test_setup_flow.py`` and ``test_deps_setup_admin.py``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plex_manager.adapters.plex.library import reset_caches
from plex_manager.models import AuthSession, SystemSettings, User
from plex_manager.ports.library import LibrarySection
from plex_manager.services import path_visibility
from plex_manager.web import setup_validation
from plex_manager.web.deps import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    hash_session_token,
)
from plex_manager.web.setup_validation import library_options, validate_plex
from tests.web.fakes import FakeLibrary, override_adapters

Handler = Callable[[httpx.Request], httpx.Response]
SeedFn = Callable[..., Awaitable[None]]
SessionMaker = async_sessionmaker[AsyncSession]

_API_KEY = "setup-validate-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_SESSION_TOKEN = "validate-session-token"  # noqa: S105 - a test cookie value, not a credential
_CSRF_TOKEN = "validate-csrf-token"  # noqa: S105 - a test CSRF value, not a credential
_CSRF_HEADERS = {CSRF_HEADER_NAME: _CSRF_TOKEN}
_ADMIN_OAUTH_TOKEN = "admin-oauth-token"  # noqa: S105 - fake token used by MockTransport tests
_PLEX_MACHINE_ID = "apollo-machine-id"


@pytest.fixture(autouse=True)
def reset_plex_caches() -> Iterator[None]:
    # The Plex adapter caches sections by base_url at module level; isolate tests.
    reset_caches()
    yield
    reset_caches()


@pytest.fixture
async def admin_client(
    client: httpx.AsyncClient, sessionmaker_: SessionMaker
) -> AsyncIterator[httpx.AsyncClient]:
    """A client authenticated as a pre-init admin (a Plex owner with an OAuth token).

    ``validate/plex`` needs the admin's stored OAuth token to assert ownership, and
    every ``validate/*`` endpoint requires an admin, so one seeded owner session
    drives them all.
    """
    async with sessionmaker_() as session:
        session.add(SystemSettings(initialized=False, setup_started_at=datetime.now(UTC)))
        user = User(
            plex_id=42, username="owner", permissions=1, encrypted_plex_token=_ADMIN_OAUTH_TOKEN
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
    client.cookies.set(SESSION_COOKIE_NAME, _SESSION_TOKEN)
    client.cookies.set(CSRF_COOKIE_NAME, _CSRF_TOKEN)
    yield client


# --------------------------------------------------------------------------- #
# Pure unit tests of library_options (no endpoint / auth)
# --------------------------------------------------------------------------- #
def test_library_options_includes_both_kinds_tagged_by_type(tmp_path: Path) -> None:
    tv_path = tmp_path / "tv"
    tv_path.mkdir()
    sections = [
        LibrarySection(
            key="1", title="Movies", type="movie", locations=(str(tmp_path), "/no/such/dir")
        ),
        LibrarySection(key="2", title="Shows", type="show", locations=(str(tv_path),)),
    ]
    options = library_options(sections)
    # BOTH movie and show sections are returned, one option per location, each tagged
    # with the app's own section_type ("show" -> "tv"); writability is per-path.
    assert [(o.title, o.path, o.section_type, o.writable) for o in options] == [
        ("Movies", str(tmp_path), "movie", True),
        ("Movies", "/no/such/dir", "movie", False),
        ("Shows", str(tv_path), "tv", True),
    ]
    assert options[0].section_key == "1"
    assert options[2].section_key == "2"


def test_library_options_probe_flag(tmp_path: Path) -> None:
    writable = LibrarySection(key="1", title="Movies", type="movie", locations=(str(tmp_path),))
    missing = LibrarySection(key="2", title="More", type="movie", locations=("/no/such/dir",))

    # Default (what the authenticated Settings picker uses): the filesystem IS probed.
    probed = library_options([writable, missing])
    assert [o.writable for o in probed] == [True, False]

    # probe_writable=False (the pre-init validate path): NOT probed -> UNKNOWN (None),
    # never a fabricated bool — even for a path that does not exist.
    unprobed = library_options([writable, missing], probe_writable=False)
    assert [o.writable for o in unprobed] == [None, None]


def test_library_options_suggests_a_container_remap_for_a_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    # tmp dirs are never mount points: relax the live-mount gate (the test seam).
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    host_section = LibrarySection(
        key="1", title="Movies", type="movie", locations=("/host/Media/Movies",)
    )
    options = library_options([host_section], suggest_mounts=(str(mount),))
    assert options[0].path == "/host/Media/Movies"  # the RAW Plex-reported path
    assert options[0].suggested_path == str(mount / "Movies")


def test_library_options_no_suggestion_when_the_path_already_resolves(tmp_path: Path) -> None:
    already_visible = LibrarySection(
        key="1", title="Movies", type="movie", locations=(str(tmp_path),)
    )
    options = library_options([already_visible], suggest_mounts=("/media",))
    assert options[0].suggested_path is None


def test_library_options_no_guess_for_an_unresolvable_bind_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 regression (maintainer decision): an unresolvable Plex location
    gets NO suggestion of any kind -- the short-lived low-confidence mount-root
    guess was removed because a child section like ``/srv/plex-data/Movies``
    would misroute to the bare mount root. Both the whole-bind-root and the
    child-section shapes stay raw; the operator types a container path manually
    (plus the wizard's visibility hint) for exotic bind topologies."""
    mount = tmp_path / "media"
    mount.mkdir()
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)  # tmp-dir mount seam
    sections = [
        LibrarySection(key="1", title="Movies", type="movie", locations=("/srv/plex-data",)),
        LibrarySection(key="2", title="Kids", type="movie", locations=("/srv/plex-data/Movies",)),
    ]
    options = library_options(sections, probe_writable=False, suggest_mounts=(str(mount),))
    assert [o.suggested_path for o in options] == [None, None]
    # And the schema carries no other suggestion field to smuggle a guess through.
    assert "low_confidence_suggested_path" not in type(options[0]).model_fields


def test_library_options_under_mount_location_is_never_suffix_probed_deeper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 regression: a Plex location ALREADY under the app's own mount
    (Docker setups where Plex sees the container paths) must be kept as-is --
    never suffix-remapped DEEPER onto a nested twin like ``/media/media/Movies``
    (which Plex does not watch), even pre-init where the raw-path probe is off
    (probing our OWN mounts is not a remote-server oracle)."""
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    # The nesting trap the longest-suffix-first search would otherwise pick.
    (mount / "media" / "Movies").mkdir(parents=True)
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)  # tmp-dir mount seam
    section = LibrarySection(
        key="1", title="Movies", type="movie", locations=(str(mount / "Movies"),)
    )
    options = library_options([section], probe_writable=False, suggest_mounts=(str(mount),))
    # As-is: no suggestion needed, and emphatically not the nested trap.
    assert options[0].suggested_path is None


def test_library_options_prefers_the_mounted_twin_over_a_phantom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-3 regression: a pre-fix install can have a PHANTOM host-shaped tree
    inside this container (the old importer ``os.makedirs``-ed e.g.
    ``/home/Media/Movies``) beside the real bind at ``/media/Movies``. The picker
    must suggest the MOUNTED twin, not treat the phantom as authoritative."""
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    phantom = tmp_path / "phantom" / "Media" / "Movies"
    phantom.mkdir(parents=True)
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)  # tmp-dir mount seam
    section = LibrarySection(key="1", title="Movies", type="movie", locations=(str(phantom),))
    # probe_writable=True (the authenticated Settings picker): the raw path IS
    # probed and exists -- exactly the shape where the phantom used to win.
    options = library_options([section], probe_writable=True, suggest_mounts=(str(mount),))
    assert options[0].suggested_path == str(mount / "Movies")


def test_library_options_suggestion_probe_original_mirrors_probe_writable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-init (probe_writable=False) must never stat the RAW, caller-supplied
    path even to compute a suggestion -- the same pre-auth-oracle guard
    ``probe_writable`` already enforces for writability. Ties the wiring
    (``probe_original=probe_writable``, and ``allow_mount_root`` always on for a
    library location), not ``remap_to_visible``'s own behavior (covered by
    ``tests/services/test_path_visibility.py``)."""
    seen: list[bool] = []
    mount_root_flags: list[bool] = []

    def spy(  # type: ignore[no-untyped-def]
        path: str,
        mounts: object,
        *,
        predicate: object = None,
        probe_original: bool = True,
        allow_mount_root: bool = False,
    ):
        seen.append(probe_original)
        mount_root_flags.append(allow_mount_root)
        return None

    monkeypatch.setattr(setup_validation, "remap_to_visible", spy)
    section = LibrarySection(key="1", title="Movies", type="movie", locations=("/some/path",))

    library_options([section], probe_writable=False, suggest_mounts=("/media",))
    library_options([section], probe_writable=True, suggest_mounts=("/media",))

    assert seen == [False, True]
    assert mount_root_flags == [True, True]  # a whole-media-root library can map to the mount root


# --------------------------------------------------------------------------- #
# Transport helpers
# --------------------------------------------------------------------------- #
async def _use_transport(app: FastAPI, handler: Handler) -> None:
    """Point the app's shared HTTP client at a mock transport for one test."""
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _owned_server(
    machine_id: str = _PLEX_MACHINE_ID, *, uri: str = "http://plex.local:32400"
) -> dict[str, object]:
    return {
        "name": "Apollo",
        "product": "Plex Media Server",
        "clientIdentifier": machine_id,
        "provides": "server",
        "owned": True,
        "connections": [
            {
                "uri": uri,
                "address": "plex.local",
                "port": 32400,
                "local": True,
                "relay": False,
                "protocol": "http",
            }
        ],
    }


_MOVIE_SECTION: dict[str, object] = {
    "key": "1",
    "title": "Movies",
    "type": "movie",
    "Location": [{"path": "/movies"}],
}
_TV_SECTION: dict[str, object] = {
    "key": "2",
    "title": "Shows",
    "type": "show",
    "Location": [{"path": "/tv"}],
}


def _plex_probe_handler(
    *,
    sections: list[dict[str, object]],
    sections_status: int = 200,
    identity: str = _PLEX_MACHINE_ID,
    resources: list[dict[str, object]] | None = None,
    expect_token: str | None = None,
) -> Handler:
    """A handler answering the Plex server (/identity, /library/sections) AND
    plex.tv (/api/v2/resources) for the ownership-verified validate/plex flow.

    Plex's /identity is unauthenticated, so a bad token still falls through to the
    section list's 401 -- the honest "Plex rejected the token" path.
    """
    server_resources = resources if resources is not None else [_owned_server()]

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        if host == "plex.tv" and path == "/api/v2/resources":
            assert request.headers.get("X-Plex-Token")
            return httpx.Response(200, json=server_resources)
        if path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": identity}})
        if path == "/library/sections":
            if expect_token is not None:
                assert request.headers["X-Plex-Token"] == expect_token
            if sections_status != 200:
                return httpx.Response(sections_status, json={})
            return httpx.Response(200, json={"MediaContainer": {"Directory": sections}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler


# --------------------------------------------------------------------------- #
# validate/tmdb
# --------------------------------------------------------------------------- #
async def test_validate_tmdb_ok(admin_client: httpx.AsyncClient, app: FastAPI) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/3/search/multi"
        return httpx.Response(200, json={"results": []})

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/tmdb", json={"api_key": "k"}, headers=_CSRF_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_validate_tmdb_bad_key(admin_client: httpx.AsyncClient, app: FastAPI) -> None:
    await _use_transport(app, lambda _r: httpx.Response(401, json={"status_message": "no"}))
    response = await admin_client.post(
        "/api/v1/setup/validate/tmdb", json={"api_key": "bad"}, headers=_CSRF_HEADERS
    )
    body = response.json()
    assert body["ok"] is False
    assert "bad" not in response.text  # the rejected key never echoes back


# --------------------------------------------------------------------------- #
# Auth: admin required, envelope-documented
# --------------------------------------------------------------------------- #
async def test_validate_requires_an_admin(client: httpx.AsyncClient, app: FastAPI) -> None:
    # No credential at all -> the envelope 401, and NO outbound request is made.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request without auth")

    await _use_transport(app, handler)

    denied = await client.post("/api/v1/setup/validate/tmdb", json={"api_key": "k"})
    assert denied.status_code == 401
    assert denied.json()["detail"] == "session_required"


async def test_validate_requires_admin_after_init(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    # Once initialized the probes require the api key (or a session) so they can't be
    # an anonymous SSRF / reachability oracle.
    await seed(initialized=True, app_api_key=_API_KEY)
    await _use_transport(app, lambda _r: httpx.Response(200, json={"results": []}))

    unauth = await client.post("/api/v1/setup/validate/tmdb", json={"api_key": "k"})
    assert unauth.status_code == 401

    ok = await client.post("/api/v1/setup/validate/tmdb", json={"api_key": "k"}, headers=_HEADERS)
    assert ok.status_code == 200
    assert ok.json()["ok"] is True


def test_validate_contract_documents_envelope_401(app: FastAPI) -> None:
    operation = app.openapi()["paths"]["/api/v1/setup/validate/tmdb"]["post"]

    # The legacy X-Setup-Token / X-Api-Key header params are gone; auth failures are
    # the structured envelope now.
    header_params = [
        parameter for parameter in operation.get("parameters", []) if parameter["in"] == "header"
    ]
    assert header_params == []
    assert operation["responses"]["401"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorEnvelope"
    )


# --------------------------------------------------------------------------- #
# validate/prowlarr
# --------------------------------------------------------------------------- #
async def test_validate_prowlarr_ok(admin_client: httpx.AsyncClient, app: FastAPI) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/system/status"
        assert request.headers["X-Api-Key"] == "pk"
        return httpx.Response(200, json={"version": "1.0"})

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://prowlarr.local", "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    assert response.json()["ok"] is True


async def test_validate_prowlarr_bad_key(admin_client: httpx.AsyncClient, app: FastAPI) -> None:
    await _use_transport(app, lambda _r: httpx.Response(401))
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://prowlarr.local", "api_key": "bad"},
        headers=_CSRF_HEADERS,
    )
    assert response.json()["ok"] is False


async def test_validate_prowlarr_rejects_non_json_status_200(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    await _use_transport(app, lambda _r: httpx.Response(200, text="<h1>not prowlarr</h1>"))
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://prowlarr.local", "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "Unexpected response from Prowlarr."


async def test_validate_prowlarr_rejects_status_200_without_version(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    await _use_transport(app, lambda _r: httpx.Response(200, json={"appName": "not-prowlarr"}))
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://prowlarr.local", "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "Unexpected response from Prowlarr."


@pytest.mark.parametrize("bad_key", ["pk\r\ninject", "pk\x00", "kéy"])
async def test_validate_prowlarr_rejects_unsafe_api_key(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_key: str
) -> None:
    # A header-unsafe api key (GHSA-qv47) is rejected BEFORE the outbound probe --
    # httpx would otherwise either echo the raw key via str(exc) (CR/LF/NUL) or
    # crash with an uncaught UnicodeEncodeError (non-ASCII).
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for an unsafe api key")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://prowlarr.local", "api_key": bad_key},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert "Prowlarr API key" in body["message"]
    assert "inject" not in response.text
    assert "kéy" not in response.text


async def test_validate_prowlarr_transport_error_never_leaks_the_key(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    # Regression pin for GHSA-qv47: a header-unsafe key used to reach httpx, which
    # raised LocalProtocolError with str(exc) == "Illegal header value b'...'" --
    # echoing the raw credential into the response. The pre-check above must stop
    # it, and this asserts the leaked fragment never appears even if it regresses.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for an unsafe api key")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://prowlarr.local", "api_key": "pk\r\ninjected"},
        headers=_CSRF_HEADERS,
    )
    assert "Illegal header value" not in response.text
    assert "injected" not in response.text


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "prowlarr.local",  # no scheme
        "http://",  # empty host
        "not a url at all",
        "ftp://prowlarr.local",
        "http://[::1",  # unterminated IPv6 literal
        "http://[vG.x]",  # invalid IPvFuture (non-hex version)
        "http://prowlarr.local:bad",  # non-numeric port -> httpx.InvalidURL
        "http://prowlarr.local:99999",  # out-of-range port
        "http://\nprowlarr.local",  # embedded newline (CR/LF log-forging shape)
        "http://prowlarr.local/\x01",  # control char in path
        "http://prowlarr local",  # space in the authority
        "http://prowlarr.local/base path",  # space anywhere (here in the path)
    ],
)
async def test_validate_prowlarr_rejects_non_http_url(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_url: str
) -> None:
    # A malformed/non-http(s) url never reaches httpx -- it gets a clear, retryable
    # rejection instead of an opaque transport error. Prove no outbound request.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for a rejected url")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": bad_url, "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "Enter a valid http(s) URL."


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://prowlarr.local?x=1",
        "http://prowlarr.local#frag",
        "http://prowlarr.local?",
        "http://prowlarr.local#",
    ],
)
async def test_validate_prowlarr_rejects_query_or_fragment(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_url: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for a rejected url")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": bad_url, "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "Base URL must not contain a query or fragment."


@pytest.mark.parametrize("bad_url", ["http://999.999.999.999", "http://01.02.03.04"])
async def test_validate_prowlarr_rejects_invalid_ipv4_shaped_host(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_url: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for a rejected url")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": bad_url, "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "Invalid IPv4 address in host."


async def test_validate_prowlarr_accepts_dotted_quad_ipv4_host(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/system/status"
        return httpx.Response(200, json={"version": "1.0"})

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://192.168.1.10:9696", "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    assert response.json()["ok"] is True


@pytest.mark.parametrize(
    ("bad_url", "message"),
    [
        ("http://[v7.abc]", "Invalid IPv6 address in host."),
        ("http://[fe80::1%eth0]", "IPv6 zone ids are not supported in a base URL."),
        ("http://[fe80::1%25eth0]:9696", "IPv6 zone ids are not supported in a base URL."),
    ],
)
async def test_validate_prowlarr_rejects_bad_bracketed_ipv6_host(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_url: str, message: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for a rejected url")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": bad_url, "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == message


async def test_validate_prowlarr_accepts_valid_ipv6_literal_host(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/system/status"
        return httpx.Response(200, json={"version": "1.0"})

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://[9999::1]:9696", "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    assert response.json()["ok"] is True


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://\N{PILE OF POO}.local",
        "http://xn--zzzzzz",
        "http://xn--ls8h.local",
    ],
)
async def test_validate_prowlarr_rejects_urls_the_http_client_cannot_parse(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_url: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for a rejected url")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": bad_url, "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "URL is not parseable by the HTTP client."


@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_validate_prowlarr_accepts_valid_http_and_https(
    admin_client: httpx.AsyncClient, app: FastAPI, scheme: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/system/status"
        return httpx.Response(200, json={"version": "1.0"})

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": f"{scheme}://prowlarr.local", "api_key": "pk"},
        headers=_CSRF_HEADERS,
    )
    assert response.json()["ok"] is True


# --------------------------------------------------------------------------- #
# validate/qbittorrent
# --------------------------------------------------------------------------- #
async def test_validate_qbittorrent_ok(
    admin_client: httpx.AsyncClient, app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No live mount in this test process at all: the download-path probe treats
    # this as "bare metal, no Docker split" (today's every non-Docker install) --
    # the client-reported default is genuinely visible here (a real directory on
    # THIS machine), so the probe stays quiet (no note).
    def _never_a_mount(_path: str) -> bool:
        return False

    monkeypatch.setattr(path_visibility, "is_live_mount", _never_a_mount)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/app/preferences":
            return httpx.Response(200, json={"save_path": str(tmp_path)})
        assert request.url.path == "/api/v2/torrents/info"
        return httpx.Response(200, json=[])

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/qbittorrent",
        json={"url": "http://qb.local", "username": "admin", "password": "pw"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is True
    assert body["download_path_note"] is None


async def test_validate_qbittorrent_notes_an_invisible_default_save_path(
    admin_client: httpx.AsyncClient, app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issues #133/#157: a client-reported default save path that is NOT visible
    inside this container surfaces a NON-blocking, informational note -- ``ok``
    stays ``True`` (Plex Manager directs every grab's save path explicitly, so
    the mismatch can no longer strand an import)."""
    downloads_mount = tmp_path / "downloads"
    downloads_mount.mkdir()
    downloads_mount_str = str(downloads_mount)

    def _only_the_downloads_mount(path: str) -> bool:
        return path == downloads_mount_str

    monkeypatch.setattr(path_visibility, "is_live_mount", _only_the_downloads_mount)
    monkeypatch.setattr(path_visibility, "KNOWN_DOWNLOAD_MOUNTS", (downloads_mount_str,))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/app/preferences":
            # A HOST-namespace path guaranteed not to exist on the box running
            # this test, and not a suffix of the container's download mount.
            return httpx.Response(
                200, json={"save_path": "/definitely-not-a-real-host-path/Downloads"}
            )
        assert request.url.path == "/api/v2/torrents/info"
        return httpx.Response(200, json=[])

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/qbittorrent",
        json={"url": "http://qb.local", "username": "admin", "password": "pw"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is True
    assert body["download_path_note"] is not None
    assert "/definitely-not-a-real-host-path/Downloads" in body["download_path_note"]


async def test_validate_qbittorrent_swallows_a_preferences_probe_failure(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    """The connectivity check already succeeded; a failure of the SECOND,
    best-effort save-path probe must never flip ``ok`` to ``False``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/app/preferences":
            return httpx.Response(500, text="boom")
        assert request.url.path == "/api/v2/torrents/info"
        return httpx.Response(200, json=[])

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/qbittorrent",
        json={"url": "http://qb.local", "username": "admin", "password": "pw"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is True
    assert body["download_path_note"] is None


async def test_validate_qbittorrent_bad_creds(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    await _use_transport(app, lambda _r: httpx.Response(200, text="Fails."))
    response = await admin_client.post(
        "/api/v1/setup/validate/qbittorrent",
        json={"url": "http://qb.local", "username": "admin", "password": "bad"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert "bad" not in response.text


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "qb.local",
        "http://",
        "not a url at all",
        "http://[::1",
        "http://[vG.x]",
        "http://qb.local:bad",
        "http://qb.local:99999",
        "http://\nqb.local",
        "http://qb.local/\x01",
        "http://qb local",
        "http://qb.local/base path",
    ],
)
async def test_validate_qbittorrent_rejects_non_http_url(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_url: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for a rejected url")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/qbittorrent",
        json={"url": bad_url, "username": "admin", "password": "pw"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "Enter a valid http(s) URL."


# --------------------------------------------------------------------------- #
# validate/plex — URL shape
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "plex.local",
        "http://",
        "not a url at all",
        "http://[::1",
        "http://[vG.x]",
        "http://plex.local:bad",
        "http://plex.local:0",
        "http://plex.local:99999",
        "http://\nplex.local",
        "http://plex.local/\x01",
        "http://plex local:32400",
        "http://plex.local/base path",
    ],
)
async def test_validate_plex_rejects_non_http_url(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_url: str
) -> None:
    # A bad URL is rejected by shape BEFORE the identity probe or the ownership
    # fetch, so no outbound request is ever attempted.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for a rejected url")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/plex", json={"url": bad_url, "token": "tok"}, headers=_CSRF_HEADERS
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "Enter a valid http(s) URL."


@pytest.mark.parametrize("bad_token", ["tok\r\ninject", "tök"])
async def test_validate_plex_rejects_unsafe_token(
    admin_client: httpx.AsyncClient, app: FastAPI, bad_token: str
) -> None:
    # A header-unsafe token (GHSA-qv47) is rejected BEFORE the identity probe or
    # section list -- no outbound request is ever attempted.
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not issue an outbound request for an unsafe token")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400", "token": bad_token},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert "Plex token" in body["message"]
    assert "inject" not in response.text
    assert "tök" not in response.text


async def test_validate_plex_empty_token_still_probes() -> None:
    # An empty token is a LEGITIMATE value the header-safety pre-check must NOT
    # reject (e.g. the health-card path calling with no stored token yet). Drives
    # ``validate_plex`` directly -- through the endpoint, an empty string falls
    # back to the admin's stored token before it ever reaches this function
    # (``body.token or admin_token``), which would not actually exercise the
    # empty-string pre-check.
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Plex-Token"] == ""
        return httpx.Response(200, json={"MediaContainer": {"Directory": [_MOVIE_SECTION]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await validate_plex(http, "http://plex.local:32400", "")

    assert result.ok is True


# --------------------------------------------------------------------------- #
# validate/plex — library listing + ownership
# --------------------------------------------------------------------------- #
async def test_validate_plex_ok_returns_movie_and_tv_libraries(
    admin_client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    movie_section = {**_MOVIE_SECTION, "Location": [{"path": str(tmp_path)}]}
    await _use_transport(
        app,
        _plex_probe_handler(
            sections=[movie_section, _TV_SECTION],
            expect_token="tok",  # noqa: S106 - a fake token value asserted, not a secret
        ),
    )
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400", "token": "tok"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is True
    assert body["machine_identifier"] == _PLEX_MACHINE_ID
    # BOTH the movie and tv libraries are offered, each tagged by section_type.
    # Writability is UNKNOWN (None) pre-init -- no filesystem probe of a
    # caller-supplied server.
    assert [(lib["title"], lib["path"], lib["section_type"]) for lib in body["libraries"]] == [
        ("Movies", str(tmp_path), "movie"),
        ("Shows", "/tv", "tv"),
    ]
    assert all(lib["writable"] is None for lib in body["libraries"])


async def test_validate_plex_movie_only_is_legit(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    await _use_transport(app, _plex_probe_handler(sections=[_MOVIE_SECTION]))
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is True
    assert [lib["section_type"] for lib in body["libraries"]] == ["movie"]


async def test_validate_plex_tv_only_is_legit(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    await _use_transport(app, _plex_probe_handler(sections=[_TV_SECTION]))
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is True
    assert [lib["section_type"] for lib in body["libraries"]] == ["tv"]


async def test_validate_plex_no_library_at_all_blocks_setup(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    # Reachable + valid token, but NEITHER a Movie NOR a TV library: an install that
    # cannot import anything is reported not-ok so the wizard stops here.
    await _use_transport(app, _plex_probe_handler(sections=[]))
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["libraries"] == []
    assert "Movie or TV library" in body["message"]


async def test_validate_plex_bad_token(admin_client: httpx.AsyncClient, app: FastAPI) -> None:
    # /identity is unauthenticated (still answers), but the section list 401s -> the
    # honest "Plex rejected the token" path; the rejected token never echoes back.
    await _use_transport(app, _plex_probe_handler(sections=[], sections_status=401))
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400", "token": "nope-secret"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is False
    assert body["message"] == "Plex rejected the token."
    assert "nope-secret" not in response.text


async def test_validate_plex_bypasses_the_sections_cache_on_a_later_outage(
    admin_client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    # list_sections' module-level cache has a 300s TTL. A healthy probe populates it;
    # validate_plex must use_cache=False so a LATER outage isn't masked as a stale
    # "ok" for up to 300s.
    movie_section = {**_MOVIE_SECTION, "Location": [{"path": str(tmp_path)}]}
    await _use_transport(app, _plex_probe_handler(sections=[movie_section]))
    first = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )
    assert first.json()["ok"] is True  # warms the 300s module-level sections cache

    await _use_transport(app, _plex_probe_handler(sections=[], sections_status=401))
    second = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )
    assert second.json()["ok"] is False  # NOT a stale "ok" served from the 300s cache


async def test_validate_plex_foreign_server_is_not_owned(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    # The probed server's machine id is one the admin only SHARES, never owns.
    await _use_transport(
        app,
        _plex_probe_handler(
            sections=[_MOVIE_SECTION], identity="shared999", resources=[_owned_server()]
        ),
    )
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "server_not_owned"


async def test_validate_plex_rejects_unadvertised_origin_before_sending_token(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "plex.tv":
            return httpx.Response(200, json=[_owned_server()])
        raise AssertionError("an unadvertised origin must not receive the stored Plex token")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://169.254.169.254:80"},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "server_not_owned"
    assert [request.url.host for request in calls] == ["plex.tv"]


async def test_validate_plex_allows_custom_origin_with_explicit_token(
    admin_client: httpx.AsyncClient, app: FastAPI
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "plex.tv":
            return httpx.Response(200, json=[_owned_server()])
        if request.url.path == "/identity":
            return httpx.Response(
                200,
                json={"MediaContainer": {"machineIdentifier": _PLEX_MACHINE_ID}},
            )
        if request.url.path == "/library/sections":
            assert request.headers["X-Plex-Token"] == "custom-token"
            return httpx.Response(
                200,
                json={"MediaContainer": {"Directory": [_MOVIE_SECTION]}},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    await _use_transport(app, handler)
    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://custom-plex.local:32400", "token": "custom-token"},
        headers=_CSRF_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert [request.url.host for request in calls] == [
        "plex.tv",
        "custom-plex.local",
        "custom-plex.local",
    ]


async def test_validate_plex_does_not_probe_filesystem(
    admin_client: httpx.AsyncClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The validate/plex endpoint checks a caller-supplied Plex server. It must NEVER
    # stat / os.access the locations that server reports. Prove _is_writable is never
    # called and writability is reported UNKNOWN.
    probed: list[str] = []

    def spy(path: str) -> bool:
        probed.append(path)
        return True

    monkeypatch.setattr(setup_validation, "_is_writable", spy)
    attacker_section = {**_MOVIE_SECTION, "Location": [{"path": "/etc"}, {"path": "/root/secret"}]}
    await _use_transport(
        app,
        _plex_probe_handler(
            sections=[attacker_section],
            resources=[_owned_server(uri="http://attacker.plex:32400")],
        ),
    )

    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://attacker.plex:32400"},
        headers=_CSRF_HEADERS,
    )
    body = response.json()
    assert body["ok"] is True
    assert probed == []  # no filesystem probe of attacker-supplied paths
    assert [lib["writable"] for lib in body["libraries"]] == [None, None]


async def test_validate_plex_attaches_a_container_suggestion_for_a_host_location(
    admin_client: httpx.AsyncClient,
    app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Plex section reporting a HOST-namespace location gets a container-visible
    ``suggested_path`` (issue #132), computed WITHOUT ever stat-ing the raw,
    caller-supplied path -- pre-init stays a non-oracle (writable stays UNKNOWN)."""
    mount = tmp_path / "media"
    (mount / "Movies").mkdir(parents=True)
    # Plex library locations are remapped under the LIBRARY mounts only. tmp dirs
    # are never mount points, so relax the live-mount gate (the test seam).
    monkeypatch.setattr(path_visibility, "KNOWN_LIBRARY_MOUNTS", (str(mount),))
    monkeypatch.setattr(path_visibility, "is_live_mount", os.path.isdir)
    probed: list[str] = []

    def spy(path: str) -> bool:
        probed.append(path)
        return True

    monkeypatch.setattr(setup_validation, "_is_writable", spy)
    host_section = {**_MOVIE_SECTION, "Location": [{"path": "/home/Media/Movies"}]}
    await _use_transport(app, _plex_probe_handler(sections=[host_section]))

    response = await admin_client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400"},
        headers=_CSRF_HEADERS,
    )

    body = response.json()
    assert body["ok"] is True
    library = body["libraries"][0]
    assert library["path"] == "/home/Media/Movies"  # the raw Plex-reported path
    assert library["suggested_path"] == str(mount / "Movies")
    assert library["writable"] is None  # pre-init never probes writability
    assert probed == []  # nor was the raw host path ever stat-ed for the option


# --------------------------------------------------------------------------- #
# Settings picker (authenticated) — probes writability, unlike the pre-init step
# --------------------------------------------------------------------------- #
async def test_plex_libraries_picker_probes_writability(
    app: FastAPI,
    client: httpx.AsyncClient,
    seed: SeedFn,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The AUTHENTICATED Settings picker uses the operator's OWN stored Plex creds, so
    # the real writability signal is legitimate there and must still be probed — the
    # opposite of the pre-init validate/plex step, which must not touch the filesystem.
    #
    # No library mounts: this test asserts the EXACT response JSON, so the remap/
    # suggestion machinery must be inert regardless of the host — the default
    # ("/media",) made the expected low_confidence field depend on whether the test
    # host really has a /media mount (the tests-py314 CI split; a run inside the
    # actual container would differ too).
    monkeypatch.setattr(path_visibility, "KNOWN_LIBRARY_MOUNTS", ())
    await seed(initialized=True, app_api_key=_API_KEY)
    movies_section = LibrarySection(
        key="1", title="Movies", type="movie", locations=(str(tmp_path),)
    )
    shows_section = LibrarySection(key="2", title="Shows", type="show", locations=("/no/tv",))
    override_adapters(app, library=FakeLibrary(sections=[movies_section, shows_section]))

    response = await client.get("/api/v1/settings/plex-libraries", headers=_HEADERS)

    assert response.status_code == 200
    assert response.json() == [
        {
            "section_key": "1",
            "title": "Movies",
            "path": str(tmp_path),
            "section_type": "movie",
            "writable": True,
            "suggested_path": None,
        },
        {
            "section_key": "2",
            "title": "Shows",
            "path": "/no/tv",
            "section_type": "tv",
            "writable": False,
            "suggested_path": None,
        },
    ]
