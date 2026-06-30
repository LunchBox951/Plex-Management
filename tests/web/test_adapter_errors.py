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

from plex_manager.adapters.prowlarr import IndexerRateLimitError
from plex_manager.adapters.qbittorrent import QbittorrentAuthError
from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError
from plex_manager.domain.release import CandidateRelease, IndexerSearchRequest
from plex_manager.ports.download_client import DownloadStatus
from plex_manager.ports.metadata import MediaSearchResult
from tests.web.fakes import FakeProwlarr, FakeQbittorrent, FakeTmdb, override_adapters

SeedFn = Callable[..., Awaitable[None]]

_API_KEY = "adapter-err-key"
_HEADERS = {"X-Api-Key": _API_KEY}
_DESCRIPTOR = {"tmdb_id": 603, "media_type": "movie", "title": "Some Movie", "year": 2020}


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
    async def get_all_statuses(self, category: str | None = None) -> list[DownloadStatus]:
        raise QbittorrentAuthError("qBittorrent rejected the login")


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
    await seed(initialized=True, app_api_key=_API_KEY)
    override_adapters(app, qbt=_AuthFailQbt())
    response = await client.get("/api/v1/queue", headers=_HEADERS)
    assert response.status_code == 502
    assert response.json() == {"detail": "qbittorrent_auth_failed"}
