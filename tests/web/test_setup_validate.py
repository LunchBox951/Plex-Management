"""Setup ``validate/*`` endpoints — real adapter paths over a mock transport.

These prove the wiring (request body -> validator -> shared HTTP client) and that
auth failures surface honestly as ``ok=False`` without leaking secrets.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.adapters.plex.library import reset_caches
from plex_manager.ports.library import LibrarySection
from plex_manager.web.setup_validation import movie_library_options

Handler = Callable[[httpx.Request], httpx.Response]
SeedFn = Callable[..., Awaitable[None]]


@pytest.fixture(autouse=True)
def reset_plex_caches() -> Iterator[None]:
    # The Plex adapter caches sections by base_url at module level; isolate tests.
    reset_caches()
    yield
    reset_caches()


def test_movie_library_options_filters_movies_and_flags_writable(tmp_path: Path) -> None:
    sections = [
        LibrarySection(
            key="1", title="Movies", type="movie", locations=(str(tmp_path), "/no/such/dir")
        ),
        LibrarySection(key="2", title="Shows", type="show", locations=("/tv",)),
    ]
    options = movie_library_options(sections)
    # Only movie sections; one option per location; writability is per-path.
    assert [(o.title, o.path, o.writable) for o in options] == [
        ("Movies", str(tmp_path), True),
        ("Movies", "/no/such/dir", False),
    ]
    assert all(o.section_key == "1" for o in options)


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


async def test_validate_plex_ok_returns_movie_libraries(
    client: httpx.AsyncClient, app: FastAPI, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/library/sections"
        assert request.headers["X-Plex-Token"] == "tok"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Directory": [
                        {
                            "key": "1",
                            "title": "Movies",
                            "type": "movie",
                            "Location": [{"path": str(tmp_path)}],
                        },
                        {
                            "key": "2",
                            "title": "Shows",
                            "type": "show",
                            "Location": [{"path": "/tv"}],
                        },
                    ]
                }
            },
        )

    await _use_transport(app, handler)
    response = await client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400", "token": "tok"},
    )
    body = response.json()
    assert body["ok"] is True
    # Only the movie library is offered, with its writability flagged.
    assert len(body["libraries"]) == 1
    assert body["libraries"][0]["title"] == "Movies"
    assert body["libraries"][0]["path"] == str(tmp_path)
    assert body["libraries"][0]["writable"] is True


async def test_validate_plex_no_movie_library_blocks_setup(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    # Plex is reachable and the token is valid, but there is no Movie library: an
    # install that cannot import anything must be reported as not-ok so the wizard
    # stops here instead of finishing into a configured-but-unusable state.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/library/sections"
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "Directory": [
                        {
                            "key": "2",
                            "title": "Shows",
                            "type": "show",
                            "Location": [{"path": "/tv"}],
                        }
                    ]
                }
            },
        )

    await _use_transport(app, handler)
    response = await client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400", "token": "tok"},
    )
    body = response.json()
    assert body["ok"] is False
    assert body["libraries"] == []
    assert "Movie library" in body["message"]


async def test_validate_plex_bad_token(client: httpx.AsyncClient, app: FastAPI) -> None:
    await _use_transport(app, lambda _r: httpx.Response(401, json={}))
    response = await client.post(
        "/api/v1/setup/validate/plex",
        json={"url": "http://plex.local:32400", "token": "nope-secret"},
    )
    body = response.json()
    assert body["ok"] is False
    assert "nope-secret" not in response.text  # the rejected token never echoes back


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
