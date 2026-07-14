"""Plex-native artwork proxy (issue #66) — the backend image proxy that keeps the
Plex token server-side.

These tests pin the route contract: an authenticated request for an in-library
title streams the Plex image bytes with the upstream ``Content-Type``; every
not-available path (Plex unconfigured, not in library, no art, Plex down) degrades
to 404 so the browser falls back to TMDB, never a 500. Auth is required.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
import pytest
from fastapi import FastAPI

from plex_manager.adapters.plex.library import PlexLibraryError
from plex_manager.ports.library import ArtworkImage
from tests.web.fakes import FakeLibrary, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "artwork-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_POSTER = ArtworkImage(content=b"\x89PNG\r\n\x1a\nposter-bytes", content_type="image/png")


async def test_proxy_streams_plex_image_for_in_library_title(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    library = FakeLibrary(artwork={(603, "movie", "poster"): _POSTER})
    override_adapters(app, library=library)

    response = await client.get("/api/v1/artwork/plex/movie/603/poster", headers=_HEADERS)

    assert response.status_code == 200
    assert response.content == _POSTER.content
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=86400"
    # The proxy asked the port with exactly the client-named key — nothing else.
    assert library.fetch_artwork_calls == [(603, "movie", "poster")]


async def test_proxy_requires_authentication(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=FakeLibrary(artwork={(603, "movie", "poster"): _POSTER}))

    response = await client.get("/api/v1/artwork/plex/movie/603/poster")

    assert response.status_code == 401


async def test_proxy_404s_when_plex_unconfigured(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # No Plex creds and no library override -> get_library_optional is None. The
    # browser falls back to TMDB, so a 404 (not a 409/500) is the honest answer.
    await seed(initialized=True, app_api_key=_API_KEY)

    response = await client.get("/api/v1/artwork/plex/movie/603/poster", headers=_HEADERS)

    assert response.status_code == 404


async def test_proxy_404s_when_title_has_no_plex_artwork(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=FakeLibrary(artwork={}))

    response = await client.get("/api/v1/artwork/plex/movie/999/poster", headers=_HEADERS)

    assert response.status_code == 404


async def test_proxy_404s_when_plex_is_down(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # A Plex outage must degrade to TMDB (404), never surface a 500.
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(
        app, library=FakeLibrary(artwork_raises=PlexLibraryError("plex request to /x failed"))
    )

    response = await client.get("/api/v1/artwork/plex/movie/603/poster", headers=_HEADERS)

    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["thumbnail", "art", "", "poster/../.."])
async def test_proxy_rejects_unknown_kind(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn, bad: str
) -> None:
    # ``kind`` is a Literal path param: anything but poster/background is a 4xx
    # before the adapter is ever consulted (no arbitrary passthrough).
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=FakeLibrary(artwork={}))

    response = await client.get(f"/api/v1/artwork/plex/movie/603/{bad}", headers=_HEADERS)

    assert response.status_code in (404, 422)
