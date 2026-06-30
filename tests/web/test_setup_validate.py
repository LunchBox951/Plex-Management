"""Setup ``validate/*`` endpoints — real adapter paths over a mock transport.

These prove the wiring (request body -> validator -> shared HTTP client) and that
auth failures surface honestly as ``ok=False`` without leaking secrets.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from fastapi import FastAPI

from plex_manager.web.setup_validation import validate_movies_root

Handler = Callable[[httpx.Request], httpx.Response]
SeedFn = Callable[..., Awaitable[None]]


def test_validate_movies_root_rejects_relative_and_traversal() -> None:
    assert validate_movies_root("relative/movies").ok is False
    assert validate_movies_root("/library/../etc").ok is False
    assert validate_movies_root("   ").ok is False


def test_validate_movies_root_accepts_a_writable_dir(tmp_path: Path) -> None:
    assert validate_movies_root(str(tmp_path)).ok is True


def test_validate_movies_root_reports_a_missing_dir() -> None:
    result = validate_movies_root("/definitely/not/a/real/path/xyz123")
    assert result.ok is False
    assert "does not exist" in result.message


async def _use_transport(app: FastAPI, handler: Handler) -> None:
    """Point the app's shared HTTP client at a mock transport for one test."""
    await app.state.http_client.aclose()
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_validate_tmdb_ok(client: httpx.AsyncClient, app: FastAPI) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/3/search/multi"
        return httpx.Response(200, json={"results": []})

    await _use_transport(app, handler)
    response = await client.post("/api/v1/setup/validate/tmdb", json={"api_key": "k"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_validate_tmdb_bad_key(client: httpx.AsyncClient, app: FastAPI) -> None:
    await _use_transport(app, lambda _r: httpx.Response(401, json={"status_message": "no"}))
    response = await client.post("/api/v1/setup/validate/tmdb", json={"api_key": "bad"})
    body = response.json()
    assert body["ok"] is False
    assert "bad" not in response.text  # the rejected key never echoes back


async def test_validate_prowlarr_ok(client: httpx.AsyncClient, app: FastAPI) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/system/status"
        assert request.headers["X-Api-Key"] == "pk"
        return httpx.Response(200, json={"version": "1.0"})

    await _use_transport(app, handler)
    response = await client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://prowlarr.local", "api_key": "pk"},
    )
    assert response.json()["ok"] is True


async def test_validate_prowlarr_bad_key(client: httpx.AsyncClient, app: FastAPI) -> None:
    await _use_transport(app, lambda _r: httpx.Response(401))
    response = await client.post(
        "/api/v1/setup/validate/prowlarr",
        json={"url": "http://prowlarr.local", "api_key": "bad"},
    )
    assert response.json()["ok"] is False


async def test_validate_plex_ok(client: httpx.AsyncClient, app: FastAPI) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/identity"
        assert request.headers["X-Plex-Token"] == "tok"
        return httpx.Response(200, json={"machineIdentifier": "abc"})

    await _use_transport(app, handler)
    response = await client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400", "token": "tok"},
    )
    assert response.json()["ok"] is True


async def test_validate_qbittorrent_ok(client: httpx.AsyncClient, app: FastAPI) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        assert request.url.path == "/api/v2/torrents/info"
        return httpx.Response(200, json=[])

    await _use_transport(app, handler)
    response = await client.post(
        "/api/v1/setup/validate/qbittorrent",
        json={"url": "http://qb.local", "username": "admin", "password": "pw"},
    )
    assert response.json()["ok"] is True


async def test_validate_qbittorrent_bad_creds(client: httpx.AsyncClient, app: FastAPI) -> None:
    await _use_transport(app, lambda _r: httpx.Response(200, text="Fails."))
    response = await client.post(
        "/api/v1/setup/validate/qbittorrent",
        json={"url": "http://qb.local", "username": "admin", "password": "bad"},
    )
    body = response.json()
    assert body["ok"] is False
    assert "bad" not in response.text


async def test_validate_requires_api_key_after_init(
    client: httpx.AsyncClient, app: FastAPI, seed: SeedFn
) -> None:
    # Pre-init the probes are open (no key exists yet); once initialized they must
    # require the api key so they can't be an anonymous SSRF / reachability oracle.
    await seed(initialized=True, app_api_key="setup-key")
    await _use_transport(app, lambda _r: httpx.Response(200, json={"results": []}))

    unauth = await client.post("/api/v1/setup/validate/tmdb", json={"api_key": "k"})
    assert unauth.status_code == 401

    ok = await client.post(
        "/api/v1/setup/validate/tmdb",
        json={"api_key": "k"},
        headers={"X-Api-Key": "setup-key"},
    )
    assert ok.status_code == 200
    assert ok.json()["ok"] is True
