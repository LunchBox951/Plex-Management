"""Typed adapter errors -> honest HTTP statuses (honesty over silence).

The adapters raise typed, actionable errors instead of swallowing a failure into
an empty result. These tests prove the app-level handlers convert each to a
meaningful status + ``detail`` (so the UI can offer 'retry later' / 're-check
credentials'), never an opaque 500, and never leaking the secret in the body.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx
from fastapi import FastAPI

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.adapters.prowlarr import IndexerError, IndexerRateLimitError
from plex_manager.adapters.qbittorrent import QbittorrentAuthError, QbittorrentError
from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError
from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest
from plex_manager.ports.library import LibrarySection
from plex_manager.ports.metadata import MediaSearchResult, MovieMetadata
from tests.web.fakes import (
    FakeLibrary,
    FakeProwlarr,
    FakeQbittorrent,
    FakeTmdb,
    candidate,
    override_adapters,
)

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "adapter-err-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_DESCRIPTOR = {"tmdb_id": 603, "media_type": "movie", "title": "Some Movie", "year": 2020}
_GOOD = "Some.Movie.2020.1080p.WEB-DL.x264-GROUP"
_GOOD_HASH = "3" * 40


async def _create_request(app: FastAPI, client: httpx.AsyncClient) -> int:
    """Create a fresh (non-terminal) movie request so a grab can reach qbt.add."""
    override_adapters(
        app, tmdb=FakeTmdb(movies={603: MovieMetadata(tmdb_id=603, title="Some Movie", year=2020)})
    )
    created = await client.post(
        "/api/v1/requests", json={"tmdb_id": 603, "media_type": "movie"}, headers=_HEADERS
    )
    assert created.status_code == 201
    return int(created.json()["id"])


class _AuthFailTmdb(FakeTmdb):
    async def search(self, query: str, year: int | None = None) -> list[MediaSearchResult]:
        raise TmdbAuthError("TMDB rejected the api key (HTTP 401)")


class _UnavailableTmdb(FakeTmdb):
    async def search(self, query: str, year: int | None = None) -> list[MediaSearchResult]:
        raise TmdbApiError("TMDB request to /search/multi failed (HTTP 429)")


class _RateLimitedProwlarr(FakeProwlarr):
    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        raise IndexerRateLimitError("all indexers rate-limited (HTTP 400)")


class _AuthFailQbt(FakeQbittorrent):
    async def add(self, magnet_or_url: str, save_path: str, category: str) -> str:
        raise QbittorrentAuthError("qBittorrent rejected the login")


class _OutageQbt(FakeQbittorrent):
    async def add(self, magnet_or_url: str, save_path: str, category: str) -> str:
        raise QbittorrentError("qBittorrent request failed")


class _OutageProwlarr(FakeProwlarr):
    async def search(self, request: IndexerSearchRequest) -> list[CandidateRelease]:
        raise IndexerError("Prowlarr search request failed")


class _AuthFailLibrary(FakeLibrary):
    async def list_sections(self) -> list[LibrarySection]:
        raise PlexAuthError("Plex rejected the request (HTTP 401)")


class _OutageLibrary(FakeLibrary):
    async def list_sections(self) -> list[LibrarySection]:
        raise PlexLibraryError("Plex request to /library/sections failed")


async def test_tmdb_auth_error_maps_to_502(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_AuthFailTmdb())
    response = await client.get(
        "/api/v1/discover/search", params={"query": "dune"}, headers=_HEADERS
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "tmdb_auth_failed"}


async def test_tmdb_api_error_maps_to_502_unavailable(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, tmdb=_UnavailableTmdb())
    response = await client.get(
        "/api/v1/discover/search", params={"query": "dune"}, headers=_HEADERS
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "tmdb_unavailable"}


async def test_indexer_rate_limit_maps_to_503(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, prowlarr=_RateLimitedProwlarr())
    response = await client.post("/api/v1/search-preview", json=_DESCRIPTOR, headers=_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": "indexer_rate_limited"}


async def test_qbittorrent_auth_error_maps_to_502(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    # GET /queue is now a passive DB read; the grab path still drives qBittorrent
    # (qbt.add), so a rejected login there surfaces as the honest 502 (not a 500).
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=_AuthFailQbt(),
    )
    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "qbittorrent_auth_failed"}


async def test_qbittorrent_outage_maps_to_502_unavailable(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """A qBittorrent outage (base QbittorrentError) on the grab path maps to a
    distinct honest detail from the auth subclass — not an opaque 500."""
    await seed(initialized=True, app_api_key=_API_KEY)
    request_id = await _create_request(app, client)
    override_adapters(
        app,
        prowlarr=FakeProwlarr([candidate(_GOOD, info_hash=_GOOD_HASH, seeders=42)]),
        qbt=_OutageQbt(),
    )
    response = await client.post(
        "/api/v1/queue/grab", json={"request_id": request_id}, headers=_HEADERS
    )
    assert response.status_code == 502
    assert response.json() == {"detail": "qbittorrent_unavailable"}


async def test_indexer_outage_maps_to_503_unavailable(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """A Prowlarr outage (base IndexerError) maps to 503 indexer_unavailable,
    distinct from the rate-limit subclass detail."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, prowlarr=_OutageProwlarr())
    response = await client.post("/api/v1/search-preview", json=_DESCRIPTOR, headers=_HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": "indexer_unavailable"}


async def test_plex_auth_error_maps_to_502(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """A revoked Plex token surfaced from the library picker maps to an honest 502,
    not an opaque 500 (the endpoint was added without a registered Plex handler)."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=_AuthFailLibrary())
    response = await client.get("/api/v1/settings/plex-libraries", headers=_HEADERS)
    assert response.status_code == 502
    assert response.json() == {"detail": "plex_auth_failed"}


async def test_plex_library_error_maps_to_502_unavailable(
    app: FastAPI, client: httpx.AsyncClient, seed: SeedFn
) -> None:
    """A Plex outage (PlexLibraryError) from the library picker maps to a distinct
    honest detail from the auth subclass — never an opaque 500."""
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, library=_OutageLibrary())
    response = await client.get("/api/v1/settings/plex-libraries", headers=_HEADERS)
    assert response.status_code == 502
    assert response.json() == {"detail": "plex_unavailable"}
