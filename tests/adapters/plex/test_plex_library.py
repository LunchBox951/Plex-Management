"""PlexLibrary adapter tests — recorded Plex shapes via ``httpx.MockTransport``.

No real network in the default run. The JSON shapes mirror Plex's ``MediaContainer``
envelope: ``/library/sections`` returns ``Directory[]`` rows (each with a
``Location[]`` of ``{"path": ...}``); ``/library/sections/{key}/all?includeGuids=1``
returns ``Metadata[]`` items carrying ids both on the scalar ``guid`` field (legacy
``themoviedb://`` agent) and on the ``Guid[]`` array (modern ``tmdb://`` agent).

Every handler asserts the token rides the ``X-Plex-Token`` header and NEVER the
URL. The module-level caches are cleared between tests by an autouse fixture.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from plex_manager.adapters.plex import PlexLibrary
from plex_manager.adapters.plex.library import (
    PlexAuthError,
    PlexLibraryError,
    reset_caches,
)
from plex_manager.ports.library import WatchState

PLEX_URL = "http://plex:32400"
TOKEN = "super-secret-plex-token"  # noqa: S105


@pytest.fixture(autouse=True)
def clear_caches() -> Iterator[None]:
    reset_caches()
    yield
    reset_caches()


# --------------------------------------------------------------------------- #
# Recorded shapes
# --------------------------------------------------------------------------- #
SECTIONS: dict[str, Any] = {
    "MediaContainer": {
        "size": 3,
        "Directory": [
            {
                "key": "1",
                "title": "Movies",
                "type": "movie",
                "Location": [
                    {"id": 1, "path": "/data/movies"},
                    {"id": 2, "path": "/mnt/movies2"},
                ],
            },
            {
                "key": "2",
                "title": "TV Shows",
                "type": "show",
                "Location": [{"id": 3, "path": "/data/tv"}],
            },
            # A non-media section (photos) must be dropped.
            {
                "key": "3",
                "title": "Photos",
                "type": "photo",
                "Location": [{"id": 4, "path": "/data/photos"}],
            },
        ],
    }
}

MOVIES_ALL: dict[str, Any] = {
    "MediaContainer": {
        "size": 2,
        "Metadata": [
            {
                "guid": "plex://movie/5d776b59ad5437001f79c6f8",
                "Guid": [
                    {"id": "imdb://tt1375666"},
                    {"id": "tmdb://27205"},
                    {"id": "tvdb://0"},
                ],
            },
            # Legacy "The Movie Database" agent: id only on the scalar guid field.
            {"guid": "com.plexapp.agents.themoviedb://129?lang=en", "Guid": []},
        ],
    }
}


def _main_handler(request: httpx.Request) -> httpx.Response:
    # The token must travel in the header and never appear in the URL.
    assert request.headers.get("X-Plex-Token") == TOKEN
    assert request.headers.get("Accept") == "application/json"
    assert TOKEN not in str(request.url)
    path = request.url.path
    if path == "/library/sections":
        return httpx.Response(200, json=SECTIONS)
    if path == "/library/sections/1/all":
        assert request.url.params.get("includeGuids") == "1"
        return httpx.Response(200, json=MOVIES_ALL)
    return httpx.Response(404, json={})


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response], base_url: str = PLEX_URL
) -> PlexLibrary:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PlexLibrary(client, base_url=base_url, token=TOKEN)


# --------------------------------------------------------------------------- #
# list_sections
# --------------------------------------------------------------------------- #
async def test_list_sections_maps_type_and_locations() -> None:
    sections = await _adapter(_main_handler).list_sections()
    assert len(sections) == 2  # photo section dropped
    movies, shows = sections
    assert movies.key == "1"
    assert movies.title == "Movies"
    assert movies.type == "movie"
    assert movies.locations == ("/data/movies", "/mnt/movies2")
    assert shows.type == "show"
    assert shows.locations == ("/data/tv",)


async def test_list_sections_is_cached_per_base_url() -> None:
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _main_handler(request)

    adapter = _adapter(counting, base_url="http://cached-plex:32400")
    first = await adapter.list_sections()
    second = await adapter.list_sections()
    assert first == second
    assert calls["n"] == 1  # second call served from the module-level cache


async def test_list_sections_cache_is_keyed_by_token_not_just_url() -> None:
    # A rotated or mistyped token for the SAME server must not read back the previous
    # token's cached sections; otherwise a bad credential could surface a stale
    # "Connected to Plex" and be saved. The cache key includes a hash of the token.
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.url.path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        return httpx.Response(404, json={})

    base = "http://rotated-plex:32400"
    token_a = "token-a-value"  # noqa: S105 — test fixture, not a real secret
    token_b = "token-b-value"  # noqa: S105 — test fixture, not a real secret
    client = httpx.AsyncClient(transport=httpx.MockTransport(counting))
    await PlexLibrary(client, base_url=base, token=token_a).list_sections()
    await PlexLibrary(client, base_url=base, token=token_b).list_sections()
    assert calls["n"] == 2  # token-B must NOT be served from token-A's cache entry
    await PlexLibrary(client, base_url=base, token=token_a).list_sections()
    assert calls["n"] == 2  # token-A is still served from its own cache entry


# --------------------------------------------------------------------------- #
# is_available — GUID parsing
# --------------------------------------------------------------------------- #
async def test_is_available_true_from_guid_array() -> None:
    # tmdb://27205 lives in the modern Guid[] array (includeGuids=1).
    assert await _adapter(_main_handler).is_available(27205, "movie") is True


async def test_is_available_true_from_legacy_scalar_guid() -> None:
    # themoviedb://129 lives in the legacy scalar guid field.
    assert await _adapter(_main_handler).is_available(129, "movie") is True


async def test_is_available_false_when_absent() -> None:
    assert await _adapter(_main_handler).is_available(55555, "movie") is False


async def test_is_available_caches_presence_but_repages_absence() -> None:
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _main_handler(request)

    adapter = _adapter(counting, base_url="http://cached-avail:32400")
    # First present lookup pages sections (1) + section/all (1) and caches the set.
    assert await adapter.is_available(27205, "movie") is True
    assert calls["n"] == 2
    # A repeated PRESENT lookup is served from the cache — no extra paging.
    assert await adapter.is_available(27205, "movie") is True
    assert calls["n"] == 2
    # An ABSENT answer is NEVER trusted from cache (a just-imported title may not be
    # indexed yet, which would otherwise strand it in Finalizing): it re-pages the
    # section contents (the section list stays cached).
    assert await adapter.is_available(55555, "movie") is False
    assert calls["n"] == 3


# --------------------------------------------------------------------------- #
# is_available — TV, per-season presence
# --------------------------------------------------------------------------- #
# One show (tmdb 1399, ratingKey "100") with three season rows: specials (index 0,
# present), season 1 (present), season 2 (announced but leafCount=0 -- NOT present,
# distinct from a season that Plex doesn't list at all).
SHOWS_ALL: dict[str, Any] = {
    "MediaContainer": {
        "size": 1,
        "Metadata": [
            {
                "ratingKey": "100",
                "guid": "plex://show/5d9c086c46115600020198a9",
                "Guid": [{"id": "tmdb://1399"}],
            },
        ],
    }
}

SEASONS_FOR_SHOW_100: dict[str, Any] = {
    "MediaContainer": {
        "size": 3,
        "Metadata": [
            {"index": 0, "leafCount": 3},  # specials, present
            {"index": 1, "leafCount": 10},  # season 1, present
            {"index": 2, "leafCount": 0},  # season 2 announced, no episodes yet
        ],
    }
}


def _tv_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("X-Plex-Token") == TOKEN
    assert TOKEN not in str(request.url)
    path = request.url.path
    if path == "/library/sections":
        return httpx.Response(200, json=SECTIONS)
    if path == "/library/sections/2/all":
        assert request.url.params.get("includeGuids") == "1"
        return httpx.Response(200, json=SHOWS_ALL)
    if path == "/library/metadata/100/children":
        return httpx.Response(200, json=SEASONS_FOR_SHOW_100)
    return httpx.Response(404, json={})


async def test_is_available_tv_season_present() -> None:
    assert await _adapter(_tv_handler).is_available(1399, "tv", season=1) is True


async def test_is_available_tv_season_zero_specials_present() -> None:
    assert await _adapter(_tv_handler).is_available(1399, "tv", season=0) is True


async def test_is_available_tv_season_absent_when_leaf_count_zero() -> None:
    # Season 2 IS listed on the show but has no episodes yet (leafCount=0) -- must
    # read as absent, not merely "unknown".
    assert await _adapter(_tv_handler).is_available(1399, "tv", season=2) is False


async def test_is_available_tv_season_absent_when_not_listed() -> None:
    # Season 5 never appears in the show's children response at all.
    assert await _adapter(_tv_handler).is_available(1399, "tv", season=5) is False


async def test_is_available_tv_whole_show_present_without_season_filter() -> None:
    assert await _adapter(_tv_handler).is_available(1399, "tv") is True


async def test_is_available_tv_absent_when_show_not_in_library() -> None:
    assert await _adapter(_tv_handler).is_available(9999, "tv") is False
    assert await _adapter(_tv_handler).is_available(9999, "tv", season=1) is False


async def test_present_seasons_returns_seasons_with_episodes() -> None:
    # Show 1399 has season 0 (specials) and season 1 with episodes, season 2 empty
    # (leafCount=0) -- present_seasons returns exactly the leafCount>0 seasons.
    assert await _adapter(_tv_handler).present_seasons(1399) == frozenset({0, 1})


async def test_present_seasons_empty_for_absent_show() -> None:
    assert await _adapter(_tv_handler).present_seasons(9999) == frozenset()


async def test_present_seasons_resolves_every_season_in_a_single_crawl() -> None:
    # The whole point of present_seasons: ONE library crawl answers for all of a
    # show's seasons, vs is_available(season=n) which re-pages per season.
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _tv_handler(request)

    adapter = _adapter(counting, base_url="http://present-seasons-once:32400")
    seasons = await adapter.present_seasons(1399)
    assert seasons == frozenset({0, 1})
    # sections (1) + the show section's /all (1) + the show's /children (1) = 3.
    assert calls["n"] == 3


async def test_is_available_tv_caches_presence_but_repages_absence() -> None:
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _tv_handler(request)

    adapter = _adapter(counting, base_url="http://cached-tv-avail:32400")
    # First lookup pages sections (1) + the show section's /all (1) + the show's
    # /children (1), then caches the per-show season map.
    assert await adapter.is_available(1399, "tv", season=1) is True
    assert calls["n"] == 3
    # A repeated PRESENT lookup (same show, same season) is served from cache.
    assert await adapter.is_available(1399, "tv", season=1) is True
    assert calls["n"] == 3
    # An ABSENT answer (season 2, leafCount=0) is NEVER trusted from cache -- it
    # re-pages (the section list itself stays cached, so only 2 more calls).
    assert await adapter.is_available(1399, "tv", season=2) is False
    assert calls["n"] == 5


# --------------------------------------------------------------------------- #
# is_available — pagination
# --------------------------------------------------------------------------- #
ONE_MOVIE_SECTION: dict[str, Any] = {
    "MediaContainer": {
        "Directory": [
            {"key": "1", "title": "Movies", "type": "movie", "Location": [{"path": "/m"}]}
        ]
    }
}


def _make_paged_handler(total: int) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=ONE_MOVIE_SECTION)
        if path == "/library/sections/1/all":
            start = int(request.headers["X-Plex-Container-Start"])
            size = int(request.headers["X-Plex-Container-Size"])
            chunk = [
                {"Guid": [{"id": f"tmdb://{1000 + i}"}]}
                for i in range(start, min(start + size, total))
            ]
            return httpx.Response(200, json={"MediaContainer": {"Metadata": chunk}})
        return httpx.Response(404, json={})

    return handler


async def test_is_available_pages_past_container_size() -> None:
    # 130 items -> a full first page (100) plus a short second page (30). An id on
    # the second page is only found if pagination advanced.
    adapter = _adapter(_make_paged_handler(130), base_url="http://paged-plex:32400")
    assert await adapter.is_available(1129, "movie") is True  # last item, page 2
    assert await adapter.is_available(1000, "movie") is True  # first item, page 1
    assert await adapter.is_available(2000, "movie") is False


# --------------------------------------------------------------------------- #
# trigger_scan
# --------------------------------------------------------------------------- #
SECTIONS_TWO_MOVIE: dict[str, Any] = {
    "MediaContainer": {
        "Directory": [
            {
                "key": "1",
                "title": "Movies",
                "type": "movie",
                "Location": [{"path": "/data/movies"}],
            },
            {"key": "4", "title": "Movies 4K", "type": "movie", "Location": [{"path": "/mnt/4k"}]},
            {"key": "2", "title": "TV", "type": "show", "Location": [{"path": "/data/tv"}]},
        ]
    }
}


def _make_trigger_handler(record: list[tuple[str, str | None]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS_TWO_MOVIE)
        match = re.fullmatch(r"/library/sections/(\d+)/refresh", path)
        if match:
            record.append((match.group(1), request.url.params.get("path")))
            return httpx.Response(200)  # empty body — still success
        return httpx.Response(404, json={})

    return handler


async def test_trigger_scan_refreshes_only_owning_section() -> None:
    record: list[tuple[str, str | None]] = []
    adapter = _adapter(_make_trigger_handler(record), base_url="http://scan-one:32400")
    scan_path = "/data/movies/Foo (2020)/foo.mkv"
    await adapter.trigger_scan(scan_path, "movie")
    # Only section 1 (whose /data/movies location is a parent) is refreshed, and the
    # path is round-tripped intact (single percent-encoding via httpx params).
    assert record == [("1", scan_path)]


async def test_trigger_scan_falls_back_to_all_movie_sections() -> None:
    record: list[tuple[str, str | None]] = []
    adapter = _adapter(_make_trigger_handler(record), base_url="http://scan-all:32400")
    await adapter.trigger_scan("/somewhere/unmapped/x.mkv", "movie")
    # No location matches -> every movie section (1 and 4, not the show section).
    assert {key for key, _ in record} == {"1", "4"}


async def test_trigger_scan_tv_refreshes_only_owning_show_section() -> None:
    record: list[tuple[str, str | None]] = []
    adapter = _adapter(_make_trigger_handler(record), base_url="http://scan-tv-one:32400")
    scan_path = "/data/tv/Some Show (2020)/Season 01"
    await adapter.trigger_scan(scan_path, "tv")
    # Only the show section (key 2) is refreshed -- never a movie section, even
    # though movie section 1's location does not prefix-match this path either.
    assert record == [("2", scan_path)]


async def test_trigger_scan_tv_falls_back_to_all_show_sections() -> None:
    record: list[tuple[str, str | None]] = []
    adapter = _adapter(_make_trigger_handler(record), base_url="http://scan-tv-all:32400")
    await adapter.trigger_scan("/somewhere/unmapped/show", "tv")
    # No location matches -> every show section (just key 2 here; movie sections
    # 1 and 4 are never touched by a tv-scoped scan).
    assert {key for key, _ in record} == {"2"}


async def test_trigger_scan_tv_raises_when_no_show_section_exists() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections":
            return httpx.Response(200, json=ONE_MOVIE_SECTION)
        return httpx.Response(404, json={})

    adapter = _adapter(handler, base_url="http://scan-no-show:32400")
    with pytest.raises(PlexLibraryError, match="no Plex show library section"):
        await adapter.trigger_scan("/data/tv/anything", "tv")


# --------------------------------------------------------------------------- #
# Error boundary — secrets never leak
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_status_raises_plex_auth_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert TOKEN not in str(request.url)
        return httpx.Response(status, json={"error": "unauthorized"})

    adapter = _adapter(handler, base_url=f"http://auth-{status}:32400")
    with pytest.raises(PlexAuthError) as exc_info:
        await adapter.list_sections()
    message = str(exc_info.value)
    assert TOKEN not in message
    assert "/library/sections" in message
    assert str(status) in message


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_server_error_raises_plex_library_error_without_token(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "boom"})

    adapter = _adapter(handler, base_url=f"http://err-{status}:32400")
    with pytest.raises(PlexLibraryError) as exc_info:
        await adapter.list_sections()
    message = str(exc_info.value)
    assert TOKEN not in message
    assert str(status) in message


async def test_transport_outage_raises_plex_library_error_without_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("name resolution failed", request=request)

    adapter = _adapter(handler, base_url="http://down-plex:32400")
    with pytest.raises(PlexLibraryError) as exc_info:
        await adapter.list_sections()
    message = str(exc_info.value)
    assert TOKEN not in message
    assert "/library/sections" in message


async def test_non_json_200_raises_plex_library_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>plex login</html>")

    adapter = _adapter(handler, base_url="http://html-plex:32400")
    with pytest.raises(PlexLibraryError) as exc_info:
        await adapter.list_sections()
    assert TOKEN not in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Token hygiene + repr + port conformance
# --------------------------------------------------------------------------- #
async def test_token_travels_in_header_never_in_url() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers.get("X-Plex-Token") == TOKEN
        return _main_handler(request)

    adapter = _adapter(handler, base_url="http://hygiene-plex:32400")
    await adapter.is_available(27205, "movie")
    assert seen  # at least /library/sections and /library/sections/1/all
    for url in seen:
        assert TOKEN not in url


def test_repr_redacts_token() -> None:
    rendered = repr(_adapter(_main_handler))
    assert TOKEN not in rendered
    assert "***" in rendered


def test_adapter_satisfies_library_port() -> None:
    from plex_manager.ports.library import LibraryPort

    assert isinstance(_adapter(_main_handler), LibraryPort)


SECTIONS_NO_MOVIE: dict[str, Any] = {
    "MediaContainer": {
        "Directory": [
            {"key": "2", "title": "TV Shows", "type": "show", "Location": [{"path": "/data/tv"}]},
        ]
    }
}


async def test_list_sections_does_not_cache_a_no_movie_result() -> None:
    # F10: a no-movie sections list is a self-healing negative -- validate_plex tells
    # the operator to add a Movie library and test again. If that empty/show-only
    # result were cached for the full TTL, the immediate re-test would read the stale
    # snapshot and stay wrongly blocked. So a no-movie result must NOT be cached;
    # adding a Movie library is seen on the very next call. A has-movie result still IS
    # cached.
    calls = {"n": 0}
    state = {"has_movie": False}

    def switching(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        if request.url.path == "/library/sections":
            calls["n"] += 1
            return httpx.Response(200, json=SECTIONS if state["has_movie"] else SECTIONS_NO_MOVIE)
        return httpx.Response(404, json={})

    adapter = _adapter(switching, base_url="http://no-movie-plex:32400")
    # First test: no Movie library -> result returned but NOT cached.
    first = await adapter.list_sections()
    assert all(s.type != "movie" for s in first)
    assert calls["n"] == 1
    # Operator adds a Movie library and tests again immediately: the next call re-hits
    # Plex (the no-movie result was never cached) and sees the new library at once.
    state["has_movie"] = True
    second = await adapter.list_sections()
    assert calls["n"] == 2  # re-fetched, not served from a stale no-movie cache
    assert any(s.type == "movie" for s in second)
    # Now that a movie section exists, the positive result IS cached.
    third = await adapter.list_sections()
    assert calls["n"] == 2  # served from cache; no extra fetch
    assert third == second


async def test_is_available_no_cache_repages_despite_cached_presence() -> None:
    """G7: use_cache=False bypasses the cached-PRESENT fast path so a removal is seen
    immediately. The default (cached) lookup still trusts a cached present answer; the
    dedup path passes use_cache=False so a removed-then-re-requested movie is not read
    as stale-True for the whole cache TTL."""
    present = {"there": True}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=ONE_MOVIE_SECTION)
        if path == "/library/sections/1/all":
            meta = [{"Guid": [{"id": "tmdb://4242"}]}] if present["there"] else []
            return httpx.Response(200, json={"MediaContainer": {"Metadata": meta}})
        return httpx.Response(404, json={})

    adapter = _adapter(handler, base_url="http://nocache-plex:32400")
    # Present -> caches the presence set {4242}.
    assert await adapter.is_available(4242, "movie") is True
    # Removed from Plex, but the cache still holds 4242: the DEFAULT lookup is stale-True.
    present["there"] = False
    assert await adapter.is_available(4242, "movie") is True
    # use_cache=False re-pages and sees the removal -> False (and refreshes the cache).
    assert await adapter.is_available(4242, "movie", use_cache=False) is False
    # The refreshed cache now reflects the removal for the default path too.
    assert await adapter.is_available(4242, "movie") is False


async def test_is_available_tv_no_cache_repages_despite_cached_presence() -> None:
    """G7, TV side: use_cache=False bypasses the cached-PRESENT fast path for a
    show/season answer too, so a season just REMOVED from Plex is seen as absent
    immediately instead of trusting the cached per-show season map for the whole
    cache TTL. Mirrors ``test_is_available_no_cache_repages_despite_cached_presence``
    above (movies)."""
    present = {"there": True}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        if path == "/library/sections/2/all":
            return httpx.Response(200, json=SHOWS_ALL)
        if path == "/library/metadata/100/children":
            leaf_count = 10 if present["there"] else 0
            meta = [{"index": 1, "leafCount": leaf_count}]
            return httpx.Response(200, json={"MediaContainer": {"Metadata": meta}})
        return httpx.Response(404, json={})

    adapter = _adapter(handler, base_url="http://nocache-tv-plex:32400")
    # Present -> caches the per-show season map with season 1 present.
    assert await adapter.is_available(1399, "tv", season=1) is True
    # Season 1 removed from Plex, but the cache still holds it: the DEFAULT lookup is
    # stale-True (same asymmetry as the movie case).
    present["there"] = False
    assert await adapter.is_available(1399, "tv", season=1) is True
    # use_cache=False re-pages and sees the removal -> False (and refreshes the cache).
    assert await adapter.is_available(1399, "tv", season=1, use_cache=False) is False
    # The refreshed cache now reflects the removal for the default path too.
    assert await adapter.is_available(1399, "tv", season=1) is False


# --------------------------------------------------------------------------- #
# watch_state — ADR-0012 disk-pressure eviction input
# --------------------------------------------------------------------------- #
_WATCHED_EPOCH = 1_700_000_000
_WATCHED_AT = datetime.fromtimestamp(_WATCHED_EPOCH, tz=UTC)
_PARTIAL_EPOCH = 1_650_000_000
_PARTIAL_AT = datetime.fromtimestamp(_PARTIAL_EPOCH, tz=UTC)

MOVIES_WATCH_ALL: dict[str, Any] = {
    "MediaContainer": {
        "size": 3,
        "Metadata": [
            # Watched: viewCount>0 with a recorded lastViewedAt.
            {"Guid": [{"id": "tmdb://27205"}], "viewCount": 3, "lastViewedAt": _WATCHED_EPOCH},
            # Never viewed: Plex omits viewCount/lastViewedAt entirely.
            {"Guid": [{"id": "tmdb://129"}]},
            # Defensive/malformed: a viewCount with no lastViewedAt must never
            # read as watched (WatchState's own consistency contract).
            {"Guid": [{"id": "tmdb://999"}], "viewCount": 1},
        ],
    }
}


def _movie_watch_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("X-Plex-Token") == TOKEN
    path = request.url.path
    if path == "/library/sections":
        return httpx.Response(200, json=SECTIONS)
    if path == "/library/sections/1/all":
        return httpx.Response(200, json=MOVIES_WATCH_ALL)
    return httpx.Response(404, json={})


async def test_watch_state_movie_watched() -> None:
    state = await _adapter(_movie_watch_handler).watch_state(27205, "movie")
    assert state == WatchState(watched=True, last_viewed_at=_WATCHED_AT)


async def test_watch_state_movie_never_viewed() -> None:
    state = await _adapter(_movie_watch_handler).watch_state(129, "movie")
    assert state == WatchState(watched=False, last_viewed_at=None)


async def test_watch_state_movie_view_count_without_timestamp_is_not_watched() -> None:
    # A stray viewCount with no lastViewedAt would be inconsistent; the adapter
    # must force watched=False rather than trust the count alone.
    state = await _adapter(_movie_watch_handler).watch_state(999, "movie")
    assert state == WatchState(watched=False, last_viewed_at=None)


async def test_watch_state_movie_absent_from_library() -> None:
    state = await _adapter(_movie_watch_handler).watch_state(55555, "movie")
    assert state == WatchState(watched=False, last_viewed_at=None)


async def test_watch_state_movie_is_not_cached_across_calls() -> None:
    # Unlike is_available, watch_state always re-pages the section's items -- a
    # stale "watched" could delete content the operator is actively rewatching, so
    # freshness wins over the extra request cost (the sweep runs on its own
    # infrequent interval). The section LIST itself still legitimately uses the
    # shared ``list_sections`` cache, so only the item-listing endpoint is counted.
    item_calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/sections/1/all":
            item_calls["n"] += 1
        return _movie_watch_handler(request)

    adapter = _adapter(counting, base_url="http://watch-uncached:32400")
    await adapter.watch_state(27205, "movie")
    await adapter.watch_state(27205, "movie")
    assert item_calls["n"] == 2  # every call re-pages the item listing


SEASONS_WATCH_FOR_SHOW_100: dict[str, Any] = {
    "MediaContainer": {
        "size": 3,
        "Metadata": [
            # Specials: fully viewed.
            {"index": 0, "leafCount": 3, "viewedLeafCount": 3, "lastViewedAt": _WATCHED_EPOCH},
            # Season 1: partially viewed.
            {"index": 1, "leafCount": 10, "viewedLeafCount": 4, "lastViewedAt": _PARTIAL_EPOCH},
            # Season 2: announced but empty (leafCount=0) -- never "watched" by
            # the vacuous 0 == 0 truth, even though viewedLeafCount also reads 0.
            {"index": 2, "leafCount": 0, "viewedLeafCount": 0},
        ],
    }
}


def _tv_watch_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("X-Plex-Token") == TOKEN
    path = request.url.path
    if path == "/library/sections":
        return httpx.Response(200, json=SECTIONS)
    if path == "/library/sections/2/all":
        return httpx.Response(200, json=SHOWS_ALL)
    if path == "/library/metadata/100/children":
        return httpx.Response(200, json=SEASONS_WATCH_FOR_SHOW_100)
    return httpx.Response(404, json={})


async def test_watch_state_tv_requires_season() -> None:
    with pytest.raises(ValueError, match="requires a season"):
        await _adapter(_tv_watch_handler).watch_state(1399, "tv")


async def test_watch_state_tv_season_fully_viewed() -> None:
    state = await _adapter(_tv_watch_handler).watch_state(1399, "tv", season=0)
    assert state == WatchState(watched=True, last_viewed_at=_WATCHED_AT)


async def test_watch_state_tv_season_partially_viewed() -> None:
    state = await _adapter(_tv_watch_handler).watch_state(1399, "tv", season=1)
    assert state == WatchState(watched=False, last_viewed_at=_PARTIAL_AT)


async def test_watch_state_tv_season_empty_leaf_count_never_watched() -> None:
    state = await _adapter(_tv_watch_handler).watch_state(1399, "tv", season=2)
    assert state == WatchState(watched=False, last_viewed_at=None)


async def test_watch_state_tv_season_not_listed() -> None:
    state = await _adapter(_tv_watch_handler).watch_state(1399, "tv", season=7)
    assert state == WatchState(watched=False, last_viewed_at=None)


async def test_watch_state_tv_show_absent_from_library() -> None:
    state = await _adapter(_tv_watch_handler).watch_state(9999, "tv", season=1)
    assert state == WatchState(watched=False, last_viewed_at=None)


async def test_watch_state_tv_is_not_cached_across_calls() -> None:
    # Same freshness argument as the movie case; the show section's item listing
    # AND the show's ``/children`` season listing must both re-fetch every call.
    children_calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/library/metadata/100/children":
            children_calls["n"] += 1
        return _tv_watch_handler(request)

    adapter = _adapter(counting, base_url="http://watch-tv-uncached:32400")
    await adapter.watch_state(1399, "tv", season=0)
    await adapter.watch_state(1399, "tv", season=0)
    assert children_calls["n"] == 2  # every call re-fetches the season listing


async def test_watch_state_adapter_conforms_to_library_port() -> None:
    from plex_manager.ports.library import LibraryPort

    assert isinstance(_adapter(_tv_watch_handler), LibraryPort)
