"""TmdbMetadata adapter tests — recorded TMDB shapes via ``httpx.MockTransport``.

No real network in the default run. The JSON shapes mirror TMDB v3 responses
(field names verified against overseerr's themoviedb interfaces): ``/search/multi``
mixes movie/tv/person rows; movie details carry top-level ``imdb_id`` plus a
``keywords.keywords`` block; tv details carry ``external_ids`` and a
``keywords.results`` block plus ``number_of_seasons``.

An OPTIONAL live smoke test against the real API is guarded by env vars and is
skipped in CI.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError, TmdbMetadata

API_KEY = "test-key-never-logged"

SEARCH_MULTI: dict[str, Any] = {
    "page": 1,
    "results": [
        {
            "id": 27205,
            "media_type": "movie",
            "title": "Inception",
            "release_date": "2010-07-15",
            "overview": "A thief who steals corporate secrets...",
            "poster_path": "/inception.jpg",
        },
        {
            "id": 1399,
            "media_type": "tv",
            "name": "Game of Thrones",
            "first_air_date": "2011-04-17",
            "overview": "Seven noble families fight...",
            "poster_path": "/got.jpg",
        },
        {
            "id": 287,
            "media_type": "person",
            "name": "Brad Pitt",
        },
    ],
    "total_results": 3,
    "total_pages": 1,
}

MOVIE_DETAIL: dict[str, Any] = {
    "id": 27205,
    "imdb_id": "tt1375666",
    "title": "Inception",
    "release_date": "2010-07-15",
    "overview": "A thief who steals corporate secrets...",
    "poster_path": "/inception.jpg",
    "external_ids": {"imdb_id": "tt1375666"},
    "keywords": {"keywords": [{"id": 9826, "name": "dream"}]},
}

ANIME_MOVIE_DETAIL: dict[str, Any] = {
    "id": 129,
    "imdb_id": "tt0245429",
    "title": "Spirited Away",
    "release_date": "2001-07-20",
    "poster_path": "/spirited.jpg",
    "external_ids": {"imdb_id": "tt0245429"},
    "keywords": {"keywords": [{"id": 210024, "name": "anime"}]},
}

TV_DETAIL: dict[str, Any] = {
    "id": 1399,
    "name": "Game of Thrones",
    "first_air_date": "2011-04-17",
    "overview": "Seven noble families fight...",
    "poster_path": "/got.jpg",
    "number_of_seasons": 8,
    "external_ids": {"imdb_id": "tt0944947", "tvdb_id": 121361},
    "keywords": {"results": [{"id": 4152, "name": "kingdom"}]},
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    # The api key must travel as a query param, and never anywhere else.
    assert request.url.params.get("api_key") == API_KEY
    if path == "/3/search/multi":
        return httpx.Response(200, json=SEARCH_MULTI)
    if path == "/3/movie/27205":
        return httpx.Response(200, json=MOVIE_DETAIL)
    if path == "/3/movie/129":
        return httpx.Response(200, json=ANIME_MOVIE_DETAIL)
    if path == "/3/movie/999999":
        return httpx.Response(404, json={"status_code": 34, "status_message": "Not found"})
    if path == "/3/tv/1399":
        return httpx.Response(200, json=TV_DETAIL)
    if path == "/3/tv/401":
        return httpx.Response(401, json={"status_code": 7, "status_message": "Invalid API key"})
    return httpx.Response(404, json={"status_message": "unhandled"})


def _adapter() -> TmdbMetadata:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return TmdbMetadata(client, API_KEY)


async def test_search_returns_movie_and_tv_rows_only() -> None:
    results = await _adapter().search("inception")
    assert len(results) == 2  # person row dropped
    movie, tv = results
    assert movie.media_type == "movie"
    assert movie.tmdb_id == 27205
    assert movie.title == "Inception"
    assert movie.year == 2010
    assert movie.poster_url == "https://image.tmdb.org/t/p/w500/inception.jpg"
    assert tv.media_type == "tv"
    assert tv.title == "Game of Thrones"
    assert tv.year == 2011


async def test_get_movie_maps_imdb_and_year() -> None:
    movie = await _adapter().get_movie(27205)
    assert movie is not None
    assert movie.tmdb_id == 27205
    assert movie.imdb_id == "tt1375666"
    assert movie.year == 2010
    assert movie.poster_url == "https://image.tmdb.org/t/p/w500/inception.jpg"


async def test_get_movie_404_returns_none() -> None:
    assert await _adapter().get_movie(999999) is None


async def test_get_tv_maps_external_ids_and_season_count() -> None:
    show = await _adapter().get_tv_show(1399)
    assert show is not None
    assert show.tvdb_id == 121361
    assert show.imdb_id == "tt0944947"
    assert show.season_count == 8
    assert show.year == 2011


async def test_401_raises_tmdb_auth_error() -> None:
    with pytest.raises(TmdbAuthError):
        await _adapter().get_tv_show(401)


async def test_auth_error_message_excludes_api_key() -> None:
    try:
        await _adapter().get_tv_show(401)
    except TmdbAuthError as exc:
        assert API_KEY not in str(exc)
    else:  # pragma: no cover - guarded by the raises above
        pytest.fail("expected TmdbAuthError")


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_server_error_raises_redacted_without_api_key(status: int) -> None:
    # The api key travels in the query string, so httpx's HTTPStatusError (which
    # embeds the full URL) must NEVER escape — these statuses are reachable in
    # production (429 rate-limit, 5xx) and would otherwise leak the key into logs.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("api_key") == API_KEY  # key is in the URL
        return httpx.Response(status, json={"status_message": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TmdbMetadata(client, API_KEY)
    with pytest.raises(TmdbApiError) as exc_info:
        await adapter.get_movie(27205)
    message = str(exc_info.value)
    assert API_KEY not in message
    assert "/movie/27205" in message
    assert str(status) in message


async def test_transport_outage_raises_tmdb_api_error_without_url() -> None:
    """TMDB unreachable (DNS / connection / timeout): httpx raises BEFORE the
    status check, so without wrapping it propagates as an opaque 500. It must be
    converted to a retryable TmdbApiError naming the path only — never the url
    (which embeds the api key)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TmdbMetadata(client, API_KEY)
    with pytest.raises(TmdbApiError) as exc_info:
        await adapter.get_movie(27205)
    message = str(exc_info.value)
    assert API_KEY not in message
    assert "/movie/27205" in message


async def test_cache_serves_second_lookup_without_network() -> None:
    calls = {"n": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting_handler))
    adapter = TmdbMetadata(client, API_KEY)
    first = await adapter.get_movie(27205)
    second = await adapter.get_movie(27205)
    assert first == second
    assert calls["n"] == 1  # second call served from cache


async def test_anime_keyword_sets_is_anime() -> None:
    # TMDB keyword 210024 surfaces as MovieMetadata.is_anime, which the request
    # flow persists to media_requests.is_anime (drives the JP/dual-audio default).
    anime = await _adapter().get_movie(129)
    assert anime is not None
    assert anime.title == "Spirited Away"
    assert anime.is_anime is True

    non_anime = await _adapter().get_movie(27205)
    assert non_anime is not None
    assert non_anime.is_anime is False


def test_adapter_satisfies_metadata_port() -> None:
    from plex_manager.ports.metadata import MetadataPort

    assert isinstance(_adapter(), MetadataPort)


@pytest.mark.skipif(
    not os.getenv("PLEX_MANAGER_LIVE_TESTS"),
    reason="live TMDB smoke test; set PLEX_MANAGER_LIVE_TESTS and TMDB_API_KEY",
)
async def test_live_smoke_search_and_resolve() -> None:  # pragma: no cover - live only
    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        pytest.skip("TMDB_API_KEY not set")
    async with httpx.AsyncClient(timeout=30.0) as client:
        adapter = TmdbMetadata(client, api_key)
        results = await adapter.search("inception", year=2010)
        assert results
        movie = await adapter.get_movie(27205)
        assert movie is not None
        assert movie.imdb_id == "tt1375666"
