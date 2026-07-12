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
from typing import Any, Literal

import httpx
import pytest

from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError, TmdbMetadata
from plex_manager.ports.metadata import RecommendationFacet

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


TRENDING_MOVIES: dict[str, Any] = {
    "page": 1,
    "results": [
        {
            "id": 27205,
            "title": "Inception",
            "release_date": "2010-07-15",
            "overview": "A thief who steals corporate secrets...",
            "poster_path": "/inception.jpg",
            "backdrop_path": "/inception_bg.jpg",
        },
        {
            # A stray person row (no title) must still be dropped even though the
            # endpoint forces media_type='movie'.
            "id": 287,
            "name": "Brad Pitt",
        },
    ],
    "total_pages": 500,
    "total_results": 10000,
}

POPULAR_MOVIES: dict[str, Any] = {
    "page": 1,
    "results": [
        {
            "id": 155,
            "title": "The Dark Knight",
            "release_date": "2008-07-16",
            "poster_path": "/tdk.jpg",
            "backdrop_path": "/tdk_bg.jpg",
        }
    ],
    "total_pages": 42,
    "total_results": 840,
}

UPCOMING_MOVIES: dict[str, Any] = {
    "page": 2,
    "results": [
        {
            "id": 1234,
            "title": "Future Film",
            "release_date": "2026-12-25",
            "poster_path": "/future.jpg",
            "backdrop_path": "/future_bg.jpg",
        }
    ],
    "total_pages": 5,
    "total_results": 95,
}

TRENDING_TV: dict[str, Any] = {
    "page": 1,
    "results": [
        {
            # Same tmdb id as a trending MOVIE row, to prove the per-kind page cache
            # never conflates a tv page with a movie page of the same number.
            "id": 27205,
            "name": "Inception: The Series",
            "first_air_date": "2022-03-01",
            "poster_path": "/inception_tv.jpg",
            "backdrop_path": "/inception_tv_bg.jpg",
        },
        {
            # A stray person row (no name) must still be dropped even though the
            # endpoint forces media_type='tv'.
            "id": 287,
            "title": "Brad Pitt",
        },
    ],
    "total_pages": 500,
    "total_results": 9999,
}

POPULAR_TV: dict[str, Any] = {
    "page": 1,
    "results": [
        {
            "id": 1399,
            "name": "Game of Thrones",
            "first_air_date": "2011-04-17",
            "poster_path": "/got.jpg",
            "backdrop_path": "/got_bg.jpg",
        }
    ],
    "total_pages": 12,
    "total_results": 240,
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    # The api key must travel as a query param, and never anywhere else.
    assert request.url.params.get("api_key") == API_KEY
    if path == "/3/search/multi":
        return httpx.Response(200, json=SEARCH_MULTI)
    if path == "/3/trending/movie/week":
        return httpx.Response(200, json=TRENDING_MOVIES)
    if path == "/3/movie/popular":
        return httpx.Response(200, json=POPULAR_MOVIES)
    if path == "/3/movie/upcoming":
        return httpx.Response(200, json=UPCOMING_MOVIES)
    if path == "/3/trending/tv/week":
        return httpx.Response(200, json=TRENDING_TV)
    if path == "/3/tv/popular":
        return httpx.Response(200, json=POPULAR_TV)
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


async def test_search_year_filters_mixed_year_results() -> None:
    results = await _adapter().search("inception", year=2010)
    assert [(r.tmdb_id, r.media_type, r.year) for r in results] == [(27205, "movie", 2010)]


async def test_search_cache_hit_returns_a_copy_never_the_cached_reference() -> None:
    """Issue #106: ``_TtlCache.get`` used to hand back the SAME list object on
    every hit within the TTL -- a caller mutating its own result (append/sort/
    clear) would silently corrupt what every LATER cache hit returns. The cache
    entry must now be immutable and every ``search`` call must get its own list."""
    adapter = _adapter()
    first = await adapter.search("inception")
    assert len(first) == 2
    first.append(first[0])  # a careless caller mutates the returned list in place
    first.clear()  # ...and even empties it

    second = await adapter.search("inception")  # served from the (still warm) cache
    assert len(second) == 2  # completely unaffected by the first call's mutation
    assert second is not first


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


async def test_non_json_200_raises_tmdb_api_error_without_url() -> None:
    """A 200 whose body is NOT JSON (a reverse-proxy / auth HTML page) would make
    response.json() raise a raw JSONDecodeError that bypasses the TmdbApiError
    handler -> opaque 500. It must be converted to a retryable TmdbApiError naming
    the path only — never the url (which embeds the api key)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("api_key") == API_KEY  # key is in the URL
        return httpx.Response(200, text="<html>login</html>")

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


async def test_trending_movies_maps_page_and_backdrop() -> None:
    page = await _adapter().trending_movies()
    assert page.page == 1
    assert page.total_pages == 500
    assert page.total_results == 10000
    assert len(page.results) == 1  # the person row (no title) is dropped
    movie = page.results[0]
    assert movie.media_type == "movie"
    assert movie.tmdb_id == 27205
    assert movie.title == "Inception"
    assert movie.year == 2010
    assert movie.poster_url == "https://image.tmdb.org/t/p/w500/inception.jpg"
    assert movie.backdrop_url == "https://image.tmdb.org/t/p/w780/inception_bg.jpg"


async def test_popular_movies_maps_envelope() -> None:
    page = await _adapter().popular_movies()
    assert page.total_pages == 42
    assert page.total_results == 840
    assert len(page.results) == 1
    assert page.results[0].title == "The Dark Knight"
    assert page.results[0].backdrop_url == "https://image.tmdb.org/t/p/w780/tdk_bg.jpg"


async def test_upcoming_movies_maps_envelope() -> None:
    page = await _adapter().upcoming_movies(page=2)
    assert page.page == 2
    assert page.total_pages == 5
    assert len(page.results) == 1
    assert page.results[0].media_type == "movie"
    assert page.results[0].title == "Future Film"


async def test_trending_tv_maps_envelope_and_kind() -> None:
    page = await _adapter().trending_tv()
    assert page.page == 1
    assert page.total_pages == 500
    assert page.total_results == 9999
    assert len(page.results) == 1  # the person row (no name) is dropped
    show = page.results[0]
    assert show.media_type == "tv"
    assert show.tmdb_id == 27205
    assert show.title == "Inception: The Series"
    assert show.year == 2022
    assert show.poster_url == "https://image.tmdb.org/t/p/w500/inception_tv.jpg"
    assert show.backdrop_url == "https://image.tmdb.org/t/p/w780/inception_tv_bg.jpg"


async def test_popular_tv_maps_envelope() -> None:
    page = await _adapter().popular_tv()
    assert page.total_pages == 12
    assert page.total_results == 240
    assert len(page.results) == 1
    assert page.results[0].media_type == "tv"
    assert page.results[0].title == "Game of Thrones"
    assert page.results[0].backdrop_url == "https://image.tmdb.org/t/p/w780/got_bg.jpg"


async def test_tv_and_movie_page_caches_are_isolated() -> None:
    # Both trending_movies() and trending_tv() are page 1 of a "trending:*:week"
    # cache prefix; a row with the SAME tmdb id (27205) sits on both. If the cache
    # key omitted the kind, one lookup would wrongly serve the other's cached page.
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting))
    adapter = TmdbMetadata(client, API_KEY)

    movies_page = await adapter.trending_movies()
    tv_page = await adapter.trending_tv()
    assert calls["n"] == 2  # each kind fetched once, neither served the other's data
    assert movies_page.results[0].media_type == "movie"
    assert movies_page.results[0].title == "Inception"
    assert tv_page.results[0].media_type == "tv"
    assert tv_page.results[0].title == "Inception: The Series"

    # Repeating both lookups is served entirely from cache -- no extra network.
    assert await adapter.trending_movies() == movies_page
    assert await adapter.trending_tv() == tv_page
    assert calls["n"] == 2


async def test_discover_page_clamped_to_valid_window() -> None:
    requested_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/3/trending/movie/week":
            requested_pages.append(request.url.params.get("page"))
            return httpx.Response(200, json=TRENDING_MOVIES)
        return _handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TmdbMetadata(client, API_KEY)
    await adapter.trending_movies(page=0)
    await adapter.trending_movies(page=9999)
    assert requested_pages == ["1", "500"]  # clamped to 1..500


async def test_discover_error_excludes_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("api_key") == API_KEY  # key is in the URL
        return httpx.Response(500, json={"status_message": "boom"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TmdbMetadata(client, API_KEY)
    with pytest.raises(TmdbApiError) as exc_info:
        await adapter.popular_movies()
    message = str(exc_info.value)
    assert API_KEY not in message
    assert "/movie/popular" in message


async def test_search_404_raises_tmdb_api_error() -> None:
    """A 404 on /search/multi means the route is wrong, NOT "no results" (issue
    #89) — it must raise TmdbApiError rather than silently mapping to an empty
    list, which would look identical to a legitimate no-match search."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status_message": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TmdbMetadata(client, API_KEY)
    with pytest.raises(TmdbApiError) as exc_info:
        await adapter.search("inception")
    assert "/search/multi" in str(exc_info.value)


async def test_list_endpoint_404_raises_tmdb_api_error() -> None:
    """A 404 on a discover/list endpoint (e.g. /movie/popular) must raise, not
    silently degrade to an empty page (issue #89) — same rationale as search."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"status_message": "not found"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TmdbMetadata(client, API_KEY)
    with pytest.raises(TmdbApiError) as exc_info:
        await adapter.popular_movies()
    assert "/movie/popular" in str(exc_info.value)


async def test_detail_lookup_404_still_returns_none() -> None:
    """Detail lookups (get_movie/get_tv_show) keep the 404 -> None contract
    (issue #89): absence really is a valid answer for a single title lookup,
    unlike search/list 404s which mean a broken route."""
    assert await _adapter().get_movie(999999) is None


async def test_movie_recommendation_profile_parses_genres_directors_and_top_ten_cast() -> None:
    calls: list[httpx.Request] = []
    cast = [{"id": 1000 + index, "name": f"Actor {index}"} for index in range(12)]
    detail = {
        **MOVIE_DETAIL,
        "genres": [
            {"id": 27, "name": "Horror"},
            {"id": -1, "name": "Invalid"},
            {"id": 28, "name": ""},
        ],
        "credits": {
            "crew": [
                {"id": 525, "name": "Christopher Nolan", "job": "Director"},
                {"id": 777, "name": "Not a Director", "job": "Producer"},
                {"id": 0, "name": "Invalid Director", "job": "Director"},
            ],
            "cast": cast,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=detail)

    adapter = TmdbMetadata(httpx.AsyncClient(transport=httpx.MockTransport(handler)), API_KEY)
    first = await adapter.recommendation_profile(27205, "movie")
    second = await adapter.recommendation_profile(27205, "movie")

    assert first is not None
    assert second is first  # immutable cached profile is safe to share
    assert len(calls) == 1
    assert calls[0].url.path == "/3/movie/27205"
    assert calls[0].url.params["append_to_response"] == "external_ids,keywords,credits"
    assert [(facet.metric, facet.value_id, facet.label) for facet in first.facets] == [
        ("genre", 27, "Horror"),
        ("director", 525, "Christopher Nolan"),
        *(("cast", 1000 + index, f"Actor {index}") for index in range(10)),
    ]
    assert isinstance(first.facets, tuple)


async def test_tv_profile_exposes_genre_and_anime_but_never_people_facets() -> None:
    detail = {
        **TV_DETAIL,
        "genres": [{"id": 16, "name": "Animation"}],
        "keywords": {"results": [{"id": 210024, "name": "anime"}]},
        # Even a malformed/unexpected credits append must not leak unsupported
        # people filters into the TV profile.
        "credits": {
            "crew": [{"id": 1, "name": "Director", "job": "Director"}],
            "cast": [{"id": 2, "name": "Actor"}],
        },
    }
    seen_append: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_append.append(request.url.params.get("append_to_response"))
        return httpx.Response(200, json=detail)

    adapter = TmdbMetadata(httpx.AsyncClient(transport=httpx.MockTransport(handler)), API_KEY)
    profile = await adapter.recommendation_profile(1399, "tv")

    assert profile is not None
    assert [(facet.metric, facet.value_id, facet.label) for facet in profile.facets] == [
        ("genre", 16, "Animation"),
        ("anime", None, "anime"),
    ]
    assert seen_append == ["external_ids,keywords"]


@pytest.mark.parametrize(
    ("media_type", "facet", "expected_path", "facet_param"),
    [
        (
            "movie",
            RecommendationFacet(metric="genre", value_id=27, label="Horror"),
            "/3/discover/movie",
            ("with_genres", "27"),
        ),
        (
            "tv",
            RecommendationFacet(metric="genre", value_id=18, label="Drama"),
            "/3/discover/tv",
            ("with_genres", "18"),
        ),
        (
            "movie",
            RecommendationFacet(metric="director", value_id=525, label="Nolan"),
            "/3/discover/movie",
            ("with_crew", "525"),
        ),
        (
            "movie",
            RecommendationFacet(metric="cast", value_id=287, label="Actor"),
            "/3/discover/movie",
            ("with_cast", "287"),
        ),
        (
            "tv",
            RecommendationFacet(metric="anime", value_id=None, label="anime"),
            "/3/discover/tv",
            ("with_keywords", "210024"),
        ),
    ],
)
async def test_discover_recommendations_maps_exact_typed_query(
    media_type: Literal["movie", "tv"],
    facet: RecommendationFacet,
    expected_path: str,
    facet_param: tuple[str, str],
) -> None:
    calls: list[httpx.Request] = []
    payload = TRENDING_MOVIES if media_type == "movie" else TRENDING_TV

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    adapter = TmdbMetadata(httpx.AsyncClient(transport=httpx.MockTransport(handler)), API_KEY)
    first = await adapter.discover_recommendations(media_type, facet)
    second = await adapter.discover_recommendations(media_type, facet)

    assert second is first
    assert len(calls) == 1
    request = calls[0]
    assert request.url.path == expected_path
    assert request.url.params[facet_param[0]] == facet_param[1]
    assert request.url.params["sort_by"] == "popularity.desc"
    assert request.url.params["include_adult"] == "false"
    assert request.url.params["page"] == "1"
    assert all(item.media_type == media_type for item in first.results)


@pytest.mark.parametrize("metric", ["director", "cast"])
async def test_tv_people_recommendations_fail_before_network(
    metric: Literal["director", "cast"],
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=TRENDING_TV)

    adapter = TmdbMetadata(httpx.AsyncClient(transport=httpx.MockTransport(handler)), API_KEY)
    facet = RecommendationFacet(metric=metric, value_id=1, label="Person")
    with pytest.raises(ValueError, match="unsupported for TV"):
        await adapter.discover_recommendations("tv", facet)
    assert calls == 0


async def test_value_bearing_anime_recommendation_fails_before_network() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=TRENDING_MOVIES)

    adapter = TmdbMetadata(httpx.AsyncClient(transport=httpx.MockTransport(handler)), API_KEY)
    facet = RecommendationFacet(metric="anime", value_id=210024, label="anime")
    with pytest.raises(ValueError, match="do not accept a facet id"):
        await adapter.discover_recommendations("movie", facet)
    assert calls == 0


@pytest.mark.parametrize("status", [401, 404, 429, 500])
async def test_recommendation_errors_are_surfaced_and_api_key_redacted(status: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"status_message": "nope"})

    adapter = TmdbMetadata(httpx.AsyncClient(transport=httpx.MockTransport(handler)), API_KEY)
    facet = RecommendationFacet(metric="genre", value_id=27, label="Horror")
    expected = TmdbAuthError if status == 401 else TmdbApiError
    with pytest.raises(expected) as exc_info:
        await adapter.discover_recommendations("movie", facet)
    assert API_KEY not in str(exc_info.value)
    assert "/discover/movie" in str(exc_info.value)


@pytest.mark.parametrize("status", [301, 302, 307])
async def test_redirect_status_raises_tmdb_api_error(status: int) -> None:
    """A 3xx (e.g. a proxy/auth redirect in front of TMDB) must be rejected like
    any other non-2xx (issue #87) — httpx.Response.is_error excludes 3xx, so the
    prior check would have read a redirect as success."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"status_message": "redirected"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TmdbMetadata(client, API_KEY)
    with pytest.raises(TmdbApiError) as exc_info:
        await adapter.get_movie(27205)
    assert str(status) in str(exc_info.value)


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
