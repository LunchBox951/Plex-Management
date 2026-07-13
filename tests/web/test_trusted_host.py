"""TrustedHostMiddleware: reject an untrusted ``Host`` before any route runs.

The maintainer's acceptance criterion: a DNS-rebinding origin (an attacker
hostname resolved to loopback) must NOT be able to reach the pre-auth
first-owner setup claim just because loopback is otherwise trusted. These
tests pin the shipped trust policy (loopback/private/link-local IP literals +
``localhost`` + configured hosts trusted by default; everything else, INCLUDING
anything only claimed via ``X-Forwarded-Host``, rejected with a fixed
``400 invalid_host``), that the middleware wins ahead of ``SetupGuardMiddleware``,
and that it never disturbs the existing optional setup-token check.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.config import get_settings
from plex_manager.web.routers import auth as auth_module
from plex_manager.web.trusted_host import (
    _is_trusted,  # pyright: ignore[reportPrivateUsage]
    _split_host,  # pyright: ignore[reportPrivateUsage]
)

SeedFn = Callable[..., Awaitable[None]]

_TOKEN = "browser-obtained-plex-token"  # noqa: S105 - fake token for the MockTransport
_MACHINE_ID = "abc123machine"

_OWNER_USER: dict[str, object] = {
    "id": 42,
    "uuid": "owner-uuid",
    "username": "plex-owner",
    "title": "plex-owner",
    "email": "owner@example.test",
}


def _owned_server() -> dict[str, object]:
    return {
        "name": "Apollo",
        "product": "Plex Media Server",
        "clientIdentifier": _MACHINE_ID,
        "provides": "server",
        "owned": True,
        "connections": [],
    }


def _owner_transport() -> httpx.MockTransport:
    """A plex.tv v2 transport that verifies the token as a server-owning account."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/user":
            return httpx.Response(200, json=_OWNER_USER)
        if request.url.host == "plex.tv" and request.url.path == "/api/v2/resources":
            return httpx.Response(200, json=[_owned_server()])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


async def _use_transport(app: FastAPI, transport: httpx.MockTransport) -> None:
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=transport)


@pytest.fixture(autouse=True)
def reset_throttle() -> None:
    """Clear the in-process sign-in throttle so a prior file's attempts never leak."""
    auth_module.reset_sign_in_throttle()


# --- Loopback / private-range allowed by default -----------------------------


@pytest.mark.parametrize(
    "host", ["localhost", "127.0.0.1", "[::1]:8000", "LOCALHOST", "localhost."]
)
async def test_loopback_and_localhost_variants_allowed(
    client: httpx.AsyncClient, host: str
) -> None:
    response = await client.get("/health", headers={"host": host})
    assert response.status_code != 400


@pytest.mark.parametrize("host", ["192.168.1.10", "10.0.0.5:8000", "172.16.0.1", "[fe80::1]:8000"])
async def test_private_and_link_local_ip_allowed(client: httpx.AsyncClient, host: str) -> None:
    response = await client.get("/health", headers={"host": host})
    assert response.status_code != 400


# --- Core / adversarial: DNS-rebinding rejected before the setup claim --------


async def test_rebinding_host_rejected_before_first_owner_claim(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    """An untrusted Host must be rejected BEFORE it can win the first-owner CAS,
    and the real operator's later claim over a trusted Host must still succeed --
    proving the attacker's request never touched the claim at all."""
    await seed(initialized=False)
    await _use_transport(app, _owner_transport())

    attacker = await client.post(
        "/api/v1/auth/plex",
        json={"auth_token": _TOKEN},
        headers={"host": "evil.example.com"},
    )
    assert attacker.status_code == 400
    assert attacker.json()["detail"] == "invalid_host"

    real = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
    assert real.status_code == 200
    assert real.json()["user"]["is_admin"] is True


async def test_untrusted_host_rejected_on_setup_complete_path(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=False)
    response = await client.post("/api/v1/setup/complete", headers={"host": "evil.example.com"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_host"


# --- X-Forwarded-Host cannot bypass the real-Host boundary --------------------


async def test_forwarded_host_header_cannot_launder_untrusted_host(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/health",
        headers={"host": "evil.example.com", "x-forwarded-host": "localhost"},
    )
    assert response.status_code == 400


async def test_forwarded_host_header_cannot_poison_trusted_host(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get(
        "/health",
        headers={"host": "localhost", "x-forwarded-host": "evil.example.com"},
    )
    assert response.status_code != 400


# --- Configured hostname trusted ----------------------------------------------


async def test_configured_allowed_host_reaches_app(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_ALLOWED_HOSTS", "plexmgr.example.com,media.lan")
    get_settings.cache_clear()
    try:
        response = await client.get("/health", headers={"host": "plexmgr.example.com"})
        assert response.status_code != 400
        response = await client.get("/health", headers={"host": "media.lan"})
        assert response.status_code != 400
    finally:
        get_settings.cache_clear()


async def test_wildcard_escape_hatch_allows_arbitrary_host(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLEX_MANAGER_ALLOWED_HOSTS", "*")
    get_settings.cache_clear()
    try:
        response = await client.get("/health", headers={"host": "anything.example.com"})
        assert response.status_code != 400
    finally:
        get_settings.cache_clear()


# --- Fail closed on missing/empty Host ----------------------------------------


async def test_missing_host_header_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"host": ""})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_host"


async def test_unknown_public_host_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"host": "attacker.example.com"})
    assert response.status_code == 400


# --- Middleware ordering: host check wins ahead of SetupGuard's allowlist ----


async def test_untrusted_host_beats_setup_guard_allowlist(
    client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """``/api/v1/auth`` is allowlisted by SetupGuardMiddleware pre-init, but an
    untrusted Host must still be rejected -- proving TrustedHostMiddleware is
    the outermost layer and runs first."""
    await seed(initialized=False)
    response = await client.get("/api/v1/auth/me", headers={"host": "evil.example.com"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_host"


# --- Setup-token enforcement is untouched by this middleware ------------------


async def test_setup_token_still_enforced_over_trusted_host(
    client: httpx.AsyncClient, seed: SeedFn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trusted Host does not bypass the OPTIONAL setup-token check -- host
    validation and the setup token are independent, additive gates."""
    monkeypatch.setenv("PLEX_MANAGER_SETUP_TOKEN", "expected-token")
    get_settings.cache_clear()
    try:
        await seed(initialized=False)
        response = await client.post("/api/v1/auth/plex", json={"auth_token": _TOKEN})
        assert response.status_code != 400  # trusted Host: not blocked here
        assert response.status_code == 401
        assert response.json()["detail"] == "invalid_setup_token"
    finally:
        get_settings.cache_clear()


# --- Unit-level coverage of the parsing/trust helpers -------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("localhost", "localhost"),
        ("localhost:8000", "localhost"),
        ("[::1]:8000", "::1"),
        ("[::1]", "::1"),
        ("::1", "::1"),
        ("", None),
        ("   ", None),
        # --- Malformed bracketed syntax must fail closed, not return the
        #     bracketed part while silently dropping attacker-chosen trailing
        #     text (the DNS-rebinding bypass the P2 finding flagged).
        ("[127.0.0.1].attacker.example", None),
        ("[::1]evil.example", None),
        ("[::1]:8080.attacker.example", None),
        ("[::1].", None),
        ("[]", None),  # empty brackets
        ("[]:8080", None),  # empty brackets with a port
        ("[::1", None),  # unterminated bracket
        ("[[::1]]", None),  # nested brackets: trailing "]" is not a valid port
        ("[::1]:", None),  # empty port
        ("[::1]:abc", None),  # non-numeric port
        ("[::1]:80:80", None),  # doubled port
        ("[::1]:99999", None),  # out-of-range port
        ("[::1]:²", None),  # unicode "superscript two": isdigit but not ASCII
        ("[::1]:0", "::1"),  # min valid port
        ("[::1]:65535", "::1"),  # max valid port
        # --- Brackets are only valid around IPv6 literals (RFC 3986): a name
        #     or IPv4 literal must not gain trust by being wrapped in brackets.
        ("[localhost]", None),
        ("[localhost]:8000", None),
        ("[127.0.0.1]", None),
        ("[127.0.0.1]:8000", None),
        ("[evil.example.com]", None),
        ("[::ffff:127.0.0.1]", "::ffff:127.0.0.1"),  # IPv4-mapped IPv6 IS IPv6
        # --- Single-colon suffixes must be REAL ports too: a malformed tail
        #     must not be silently normalized down to a trusted prefix.
        ("127.0.0.1:evil.example", None),
        ("localhost:notaport", None),
        ("localhost:", None),  # empty port
        ("localhost:²", None),  # unicode digit port
        ("localhost:99999", None),  # out-of-range port
        ("localhost:0", "localhost"),  # min valid port
        ("localhost:65535", "localhost"),  # max valid port
        # --- Digit strings past CPython's int-conversion limit must be
        #     rejected by the length bound BEFORE int() runs (which would
        #     raise ValueError -> a 500 instead of the fixed 400).
        ("[::1]:" + "1" * 5000, None),
        ("127.0.0.1:" + "9" * 5000, None),
    ],
)
def test_split_host(raw: str, expected: str | None) -> None:
    assert _split_host(raw) == expected


@pytest.mark.parametrize(
    "host",
    [
        "[127.0.0.1].attacker.example",
        "[::1]evil.example",
        "[::1]:8080.attacker.example",
        "[]",
        "[::1",
        "127.0.0.1:evil.example",
        "localhost:notaport",
        "[::1]:" + "1" * 5000,
        "[localhost]:8000",
        "[127.0.0.1]",
    ],
)
async def test_malformed_host_rejected_end_to_end(client: httpx.AsyncClient, host: str) -> None:
    """A trusted literal smuggled inside a malformed Host (bracketed suffix
    tricks, non-IPv6 bracket contents, bogus single-colon "ports",
    int-limit-length digit strings) must not reach the app -- the parser fails
    closed, so the middleware returns the fixed 400 rather than trusting a
    partial parse or raising a 500."""
    response = await client.get("/health", headers={"host": host})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid_host"


@pytest.mark.parametrize(
    ("host", "configured", "expected"),
    [
        ("localhost", frozenset(), True),
        ("LOCALHOST", frozenset(), True),
        ("localhost.", frozenset(), True),
        ("127.0.0.1", frozenset(), True),
        ("::1", frozenset(), True),
        ("192.168.1.1", frozenset(), True),
        ("169.254.1.1", frozenset(), True),
        ("8.8.8.8", frozenset(), False),
        ("evil.example.com", frozenset(), False),
        ("evil.example.com", frozenset({"evil.example.com"}), True),
        ("anything", frozenset({"*"}), True),
        ("", frozenset(), False),
    ],
)
def test_is_trusted(host: str, configured: frozenset[str], expected: bool) -> None:
    assert _is_trusted(host, configured) is expected
