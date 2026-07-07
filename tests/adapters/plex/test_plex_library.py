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


async def test_list_sections_use_cache_false_bypasses_the_cache_read() -> None:
    # R5-4: the health/"Test connection" probe must always reflect reality, so
    # it needs a cache-BYPASS -- a fresh call with use_cache=False re-pages Plex
    # live even though a previous call already warmed the 300s cache.
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _main_handler(request)

    adapter = _adapter(counting, base_url="http://bypass-plex:32400")
    await adapter.list_sections()
    assert calls["n"] == 1  # warms the cache
    await adapter.list_sections(use_cache=False)
    assert calls["n"] == 2  # bypassed the cache, re-hit Plex live
    # A later default (cached) call is served from the cache the bypass call
    # refreshed -- the bypass SETS the cache, it doesn't just skip it forever.
    await adapter.list_sections()
    assert calls["n"] == 2


async def test_list_sections_use_cache_false_reflects_a_new_outage() -> None:
    # The scenario the health probe exists to catch: a healthy call warms the
    # cache, then Plex goes down (or the token is rejected) -- a use_cache=False
    # call must see the CURRENT failure, not a cached "ok" sections list.
    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    adapter = _adapter(_main_handler, base_url="http://outage-plex:32400")
    sections = await adapter.list_sections()
    assert len(sections) == 2  # cache is warm

    down_client = httpx.AsyncClient(transport=httpx.MockTransport(flaky))
    down_adapter = PlexLibrary(down_client, base_url="http://outage-plex:32400", token=TOKEN)
    with pytest.raises(PlexAuthError):
        await down_adapter.list_sections(use_cache=False)


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


@pytest.mark.parametrize("bad_token", ["tok\r\ninjected", "tok\x00nul", "tokén-nonascii"])
async def test_header_unsafe_token_raises_plex_auth_error_without_request(
    bad_token: str,
) -> None:
    """Defense-in-depth: a stored token that cannot ride the ``X-Plex-Token`` header
    (a CR/LF/NUL value would leak the RAW token via httpx's ``str(exc)``; a non-ASCII
    value an uncaught ``UnicodeEncodeError``/500) fails as a surfaced ``PlexAuthError``,
    WITHOUT the token ever being placed in a request. Mirrors the oauth adapter's own
    ``_require_header_safe_token`` guard for the one remaining plex header sink."""
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        nonlocal sent
        sent += 1
        return httpx.Response(200, json=SECTIONS)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = PlexLibrary(client, base_url=PLEX_URL, token=bad_token)
    try:
        with pytest.raises(PlexAuthError) as exc_info:
            await adapter.list_sections()
        assert bad_token not in str(exc_info.value)
        assert sent == 0
    finally:
        await client.aclose()


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


# --------------------------------------------------------------------------- #
# season_presence — BATCH targeted lookup, NOT a whole-library crawl (#136)
# --------------------------------------------------------------------------- #
# Three shows in the SAME show section (ratingKeys 100/200/300); the target
# (tmdb 2000) sits in the MIDDLE so a test proves the lookup actually matches by
# tmdb id rather than happening to grab the first/last item.
SHOWS_ALL_MULTI: dict[str, Any] = {
    "MediaContainer": {
        "size": 3,
        "Metadata": [
            {"ratingKey": "100", "guid": "plex://show/aaa", "Guid": [{"id": "tmdb://1000"}]},
            {"ratingKey": "200", "guid": "plex://show/bbb", "Guid": [{"id": "tmdb://2000"}]},
            {"ratingKey": "300", "guid": "plex://show/ccc", "Guid": [{"id": "tmdb://3000"}]},
        ],
    }
}

SEASONS_FOR_SHOW_100_MULTI: dict[str, Any] = {
    "MediaContainer": {"size": 1, "Metadata": [{"index": 1, "leafCount": 5}]}
}

SEASONS_FOR_SHOW_200: dict[str, Any] = {
    "MediaContainer": {
        "size": 2,
        "Metadata": [
            {"index": 1, "leafCount": 8},  # season 1, present
            {"index": 2, "leafCount": 0},  # season 2 announced, no episodes yet
        ],
    }
}

SEASONS_FOR_SHOW_300: dict[str, Any] = {
    "MediaContainer": {"size": 1, "Metadata": [{"index": 1, "leafCount": 4}]}
}


def _make_multi_show_handler(calls: dict[str, int]) -> Callable[[httpx.Request], httpx.Response]:
    """Serves 3 shows in one section, each with its OWN ``/children`` endpoint, so
    a test can prove ``season_presence`` fetches ONLY the requested shows'
    children -- never a show that was NOT part of the request -- unlike a
    whole-library season crawl. Only ratingKey 200 (tmdb 2000) has a real
    ``/children`` response wired here; 100/300's fetch hard-fails, so a test that
    requests ONLY {2000} proves those two are never touched."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        if path == "/library/sections/2/all":
            return httpx.Response(200, json=SHOWS_ALL_MULTI)
        if path == "/library/metadata/200/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_200)
        if path in ("/library/metadata/100/children", "/library/metadata/300/children"):
            pytest.fail(f"season_presence must not fetch children for a non-target show: {path}")
        return httpx.Response(404, json={})

    return handler


def _make_multi_show_handler_all_seasons(
    calls: dict[str, int],
) -> Callable[[httpx.Request], httpx.Response]:
    """Same 3-show section as :func:`_make_multi_show_handler`, but ALL THREE
    shows' ``/children`` are real (no hard-fail) -- used by tests that
    deliberately request all three ids in one batch call."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        if path == "/library/sections/2/all":
            return httpx.Response(200, json=SHOWS_ALL_MULTI)
        if path == "/library/metadata/100/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_100_MULTI)
        if path == "/library/metadata/200/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_200)
        if path == "/library/metadata/300/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_300)
        return httpx.Response(404, json={})

    return handler


# Two show sections ("TV Shows" + "Anime") each holding a DIFFERENT item that
# resolves to the SAME tmdb id (7000) -- a show catalogued in both a normal TV
# library and a separate Anime library, or duplicated within one section, is the
# exact scenario finding 1 (#136 review) requires a union over, not a first-match.
SECTIONS_TWO_SHOW: dict[str, Any] = {
    "MediaContainer": {
        "size": 3,
        "Directory": [
            {
                "key": "1",
                "title": "Movies",
                "type": "movie",
                "Location": [{"id": 1, "path": "/data/movies"}],
            },
            {
                "key": "2",
                "title": "TV Shows",
                "type": "show",
                "Location": [{"id": 3, "path": "/data/tv"}],
            },
            {
                "key": "5",
                "title": "Anime",
                "type": "show",
                "Location": [{"id": 6, "path": "/data/anime"}],
            },
        ],
    }
}

TV_SHOWS_WITH_DUP: dict[str, Any] = {
    "MediaContainer": {
        "size": 1,
        "Metadata": [
            {"ratingKey": "500", "guid": "plex://show/dup-tv", "Guid": [{"id": "tmdb://7000"}]},
        ],
    }
}

ANIME_SHOWS_WITH_DUP: dict[str, Any] = {
    "MediaContainer": {
        "size": 1,
        "Metadata": [
            {"ratingKey": "600", "guid": "plex://show/dup-anime", "Guid": [{"id": "tmdb://7000"}]},
        ],
    }
}

SEASONS_FOR_SHOW_500: dict[str, Any] = {
    "MediaContainer": {"size": 1, "Metadata": [{"index": 1, "leafCount": 3}]}
}

SEASONS_FOR_SHOW_600: dict[str, Any] = {
    "MediaContainer": {"size": 1, "Metadata": [{"index": 2, "leafCount": 5}]}
}


def _duplicate_show_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("X-Plex-Token") == TOKEN
    assert TOKEN not in str(request.url)
    path = request.url.path
    if path == "/library/sections":
        return httpx.Response(200, json=SECTIONS_TWO_SHOW)
    if path == "/library/sections/2/all":
        return httpx.Response(200, json=TV_SHOWS_WITH_DUP)
    if path == "/library/sections/5/all":
        return httpx.Response(200, json=ANIME_SHOWS_WITH_DUP)
    if path == "/library/metadata/500/children":
        return httpx.Response(200, json=SEASONS_FOR_SHOW_500)
    if path == "/library/metadata/600/children":
        return httpx.Response(200, json=SEASONS_FOR_SHOW_600)
    return httpx.Response(404, json={})


async def test_season_presence_returns_seasons_for_the_target_show() -> None:
    calls: dict[str, int] = {}
    adapter = _adapter(_make_multi_show_handler(calls), base_url="http://season-presence:32400")
    assert await adapter.season_presence({2000}) == {2000: frozenset({1})}


async def test_season_presence_empty_for_absent_show() -> None:
    calls: dict[str, int] = {}
    adapter = _adapter(
        _make_multi_show_handler(calls), base_url="http://season-presence-absent:32400"
    )
    assert await adapter.season_presence({9999}) == {9999: frozenset()}
    # No show matched -- ``/children`` is never touched at all.
    assert not any(path.startswith("/library/metadata/") for path in calls)


async def test_season_presence_evicts_a_stale_cache_entry_on_a_no_match_read() -> None:
    """Regression (symmetric to the no-poison case): a show that WAS cached as
    present but has since been REMOVED from Plex must be evicted from the TV
    seasons snapshot by a fresh no-match read — otherwise a season-scoped
    ``is_available`` inside the TTL keeps answering True from the stale entry."""
    calls: dict[str, int] = {}
    removed = False

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        if path == "/library/sections/2/all":
            if removed:
                return httpx.Response(200, json={"MediaContainer": {"size": 0, "Metadata": []}})
            return httpx.Response(200, json=SHOWS_ALL_MULTI)
        if path == "/library/metadata/200/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_200)
        return httpx.Response(404, json={})

    adapter = _adapter(handler, base_url="http://season-presence-evict-stale:32400")
    # Warm the snapshot: the show is present with season 1.
    assert await adapter.season_presence({2000}) == {2000: frozenset({1})}
    # The show vanishes from Plex; a fresh read returns empty AND must evict
    # the stale cache entry...
    removed = True
    assert await adapter.season_presence({2000}) == {2000: frozenset()}
    # ...so a season-scoped availability check inside the TTL answers False
    # instead of True-from-the-stale-snapshot.
    assert await adapter.is_available(2000, "tv", season=1) is False


async def test_season_presence_never_caches_a_no_match_id_as_present() -> None:
    """Regression: a requested id with NO matching item must not be written to the
    TV seasons cache. ``_is_tv_available`` treats a cached KEY as "show present"
    for whole-show checks, so caching the miss's empty set would flip a later
    ``is_available(tmdb_id, "tv")`` inside the TTL to True for a show Plex has
    never indexed."""
    calls: dict[str, int] = {}
    # The permissive handler: the later ``is_available`` legitimately performs a
    # full crawl on its cache miss (that miss IS the point of the test).
    adapter = _adapter(
        _make_multi_show_handler_all_seasons(calls),
        base_url="http://season-presence-no-poison:32400",
    )
    assert await adapter.season_presence({9999}) == {9999: frozenset()}
    # Within the cache TTL: the whole-show availability check must still answer
    # False (fresh re-crawl finding nothing), not True from a poisoned snapshot.
    assert await adapter.is_available(9999, "tv") is False


async def test_season_presence_empty_batch_returns_empty_mapping_without_any_request() -> None:
    """An empty ``tmdb_ids`` collection must short-circuit -- no reason to walk any
    section when nothing was asked for."""
    calls: dict[str, int] = {}
    adapter = _adapter(
        _make_multi_show_handler(calls), base_url="http://season-presence-empty:32400"
    )
    assert await adapter.season_presence([]) == {}
    assert calls == {}


async def test_season_presence_does_not_crawl_the_whole_library() -> None:
    """The proof this exists for (#136): resolving a small requested subset of
    shows' seasons must cost O(1) HTTP calls (sections + the owning show
    section's listing, walked ONCE, + only the REQUESTED shows' OWN
    ``/children``) -- never one ``/children`` fetch per show in the library
    (which is what ``present_seasons``/``is_available`` pay to answer for ANY
    show), and never a fetch for a show that was not part of the request."""
    calls: dict[str, int] = {}
    adapter = _adapter(_make_multi_show_handler(calls), base_url="http://season-presence-o1:32400")
    seasons = await adapter.season_presence({2000})
    assert seasons == {2000: frozenset({1})}
    assert calls["/library/sections"] == 1
    assert calls["/library/sections/2/all"] == 1
    assert calls["/library/metadata/200/children"] == 1
    # The OTHER two shows' children were never fetched (enforced by the handler's
    # own pytest.fail above; re-asserted here for a clear failure message too).
    assert "/library/metadata/100/children" not in calls
    assert "/library/metadata/300/children" not in calls
    # Movie section untouched -- this is a TV-only targeted lookup.
    assert "/library/sections/1/all" not in calls


async def test_season_presence_one_page_walk_for_n_shows() -> None:
    """(#136 review finding 2) A batch call naming N distinct target shows must
    still walk the show section's ``/all`` listing EXACTLY ONCE -- never once per
    requested id -- regardless of N. All three shows in the section are
    requested here (N=3) and the page-walk count must stay 1, not 3."""
    calls: dict[str, int] = {}
    adapter = _adapter(
        _make_multi_show_handler_all_seasons(calls), base_url="http://season-presence-batch:32400"
    )
    result = await adapter.season_presence({1000, 2000, 3000})
    assert result == {
        1000: frozenset({1}),
        2000: frozenset({1}),
        3000: frozenset({1}),
    }
    assert calls["/library/sections/2/all"] == 1
    # One /children fetch per MATCHED show -- three requested ids, three matches.
    assert calls["/library/metadata/100/children"] == 1
    assert calls["/library/metadata/200/children"] == 1
    assert calls["/library/metadata/300/children"] == 1


async def test_season_presence_unions_seasons_across_duplicate_show_entries() -> None:
    """(#136 review finding 1) The same tmdb id catalogued in TWO show sections
    (a separate 'TV Shows' and 'Anime' library is a real deployment shape) must
    have its present seasons UNIONED across every matching item. Returning only
    the first match's seasons would under-report a season present only on the
    OTHER duplicate, stalling that season at 'Finalizing' forever."""
    adapter = _adapter(_duplicate_show_handler, base_url="http://season-presence-dup:32400")
    result = await adapter.season_presence({7000})
    # Season 1 comes from the "TV Shows" entry (ratingKey 500), season 2 from the
    # "Anime" entry (ratingKey 600) -- the union of both, not just one.
    assert result == {7000: frozenset({1, 2})}


async def test_season_presence_returns_partial_union_when_one_duplicate_fails() -> None:
    """(round 6, #136 review) When duplicates exist and one entry's ``/children``
    fails while another CONFIRMS seasons, the confirmed partial union must be
    RETURNED (positive evidence is sound to promote on — omitting the id would
    strand a Plex-confirmed season at 'Finalizing' behind a broken duplicate)
    but must NOT be written through to the cache: the union may be missing
    seasons that only live on the failed duplicate, so a later season-scoped
    check re-crawls fresh instead of trusting an incomplete snapshot."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS_TWO_SHOW)
        if path == "/library/sections/2/all":
            return httpx.Response(200, json=TV_SHOWS_WITH_DUP)
        if path == "/library/sections/5/all":
            return httpx.Response(200, json=ANIME_SHOWS_WITH_DUP)
        if path == "/library/metadata/500/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_500)
        if path == "/library/metadata/600/children":
            return httpx.Response(500, json={})  # the broken duplicate
        return httpx.Response(404, json={})

    adapter = _adapter(handler, base_url="http://season-presence-partial-dup:32400")
    # The healthy "TV Shows" duplicate confirmed season 1 — returned despite the
    # broken "Anime" duplicate erroring.
    assert await adapter.season_presence({7000}) == {7000: frozenset({1})}
    # The incomplete union was NOT cached: a season-scoped check for season 2
    # (which lives only on the failed duplicate) re-crawls fresh rather than
    # answering False from a partial snapshot. The fresh crawl in this handler
    # errors on 600's children too, so the honest outcome is the adapter's own
    # error — NOT a confident False produced by an incomplete cache entry.
    with pytest.raises(PlexLibraryError):
        await adapter.is_available(7000, "tv", season=2)


async def test_season_presence_isolates_a_single_show_failure_in_the_same_batch() -> None:
    """(round 4, #136 review) One show's ``/children`` fetch returning a 500 inside
    an otherwise-successful batch call must not abort the OTHER show's lookup.
    The failed show is OMITTED from the returned mapping entirely (never mapped
    to an empty frozenset -- that would dishonestly claim "no seasons present"
    for a show whose presence is actually unknown); the healthy show's entry is
    still cached from this same call; and a subsequent check for the FAILED show
    still re-crawls fresh once the underlying fault clears -- it was poisoned
    neither as present nor as absent by the earlier failure."""
    calls: dict[str, int] = {}
    show_100_should_fail = True

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        if path == "/library/sections/2/all":
            return httpx.Response(200, json=SHOWS_ALL_MULTI)
        if path == "/library/metadata/100/children":
            if show_100_should_fail:
                return httpx.Response(500, json={})
            return httpx.Response(200, json=SEASONS_FOR_SHOW_100_MULTI)
        if path == "/library/metadata/200/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_200)
        if path == "/library/metadata/300/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_300)
        return httpx.Response(404, json={})

    adapter = _adapter(handler, base_url="http://season-presence-isolate:32400")
    result = await adapter.season_presence({1000, 2000})
    # The healthy show (2000) resolves; the failing show (1000) is omitted
    # entirely -- never present as a key with a dishonest empty frozenset.
    assert result == {2000: frozenset({1})}
    assert 1000 not in result

    # The healthy show's entry was written through to the cache by that same
    # call: a season-scoped availability check must not need another
    # ``/children`` fetch.
    children_200_calls = calls["/library/metadata/200/children"]
    assert await adapter.is_available(2000, "tv", season=1) is True
    assert calls["/library/metadata/200/children"] == children_200_calls

    # The failed show (1000) was cached in NEITHER direction. Once the
    # underlying fault clears, a fresh check must see it as present -- not
    # stuck absent from a poisoned miss...
    show_100_should_fail = False
    assert await adapter.is_available(1000, "tv", season=1) is True
    # ...and re-running the batch lookup for it must actually re-crawl (not
    # trust a stale cached absence either).
    assert await adapter.season_presence({1000}) == {1000: frozenset({1})}


async def test_season_presence_is_never_cached_absence() -> None:
    """Mirrors ``present_seasons``'/``is_available``'s "never trust a cached
    absence" contract: a season that just finished indexing must be seen on the
    very next call, even if an earlier snapshot is warm in the shared cache."""
    calls = {"n": 0}

    def counting(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _tv_handler(request)

    adapter = _adapter(counting, base_url="http://season-presence-fresh:32400")
    # Warm the shared snapshot cache via present_seasons (season 2 reads absent).
    assert await adapter.present_seasons(1399) == frozenset({0, 1})
    warmed_calls = calls["n"]
    # season_presence re-pages fresh rather than trusting the warm (absent) cache.
    assert await adapter.season_presence({1399}) == {1399: frozenset({0, 1})}
    assert calls["n"] > warmed_calls


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
# present_ids — batched tile decoration (issue #29)
# --------------------------------------------------------------------------- #
def _make_counting_grid_handler(calls: dict[str, int]) -> Callable[[httpx.Request], httpx.Response]:
    """A handler serving BOTH the movie (section 1) and show (section 2) crawls, the
    show ``/children`` (which present_ids must NEVER hit), and section ``/refresh``.
    Records per-path call counts so a test can prove the crawl fan-out.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        calls["_total"] = calls.get("_total", 0) + 1
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        if path == "/library/sections/1/all":
            return httpx.Response(200, json=MOVIES_ALL)
        if path == "/library/sections/2/all":
            return httpx.Response(200, json=SHOWS_ALL)
        if path == "/library/metadata/100/children":
            return httpx.Response(200, json=SEASONS_FOR_SHOW_100)
        if re.fullmatch(r"/library/sections/\d+/refresh", path):
            return httpx.Response(200)
        return httpx.Response(404, json={})

    return handler


async def test_present_ids_movie_subset_in_a_single_crawl() -> None:
    calls: dict[str, int] = {}
    adapter = _adapter(_make_counting_grid_handler(calls), base_url="http://present-movie:32400")
    present = await adapter.present_ids([(27205, "movie"), (129, "movie"), (55555, "movie")])
    assert present == frozenset({(27205, "movie"), (129, "movie")})
    # ONE crawl: the sections list + the single movie section's /all. A movie-only
    # page never touches the show sections (no per-title fan-out).
    assert calls["/library/sections"] == 1
    assert calls["/library/sections/1/all"] == 1
    assert "/library/sections/2/all" not in calls


async def test_present_ids_tv_subset_without_a_children_crawl() -> None:
    calls: dict[str, int] = {}
    adapter = _adapter(_make_counting_grid_handler(calls), base_url="http://present-tv:32400")
    present = await adapter.present_ids([(1399, "tv"), (9999, "tv")])
    assert present == frozenset({(1399, "tv")})
    # Show-level presence is a single guid crawl of the show section -- NEVER the
    # per-show /children fetch (that is _TV_SEASONS_CACHE's expensive job, not a tile's).
    assert calls["/library/sections/2/all"] == 1
    assert "/library/metadata/100/children" not in calls
    # A tv-only page never crawls the movie sections either.
    assert "/library/sections/1/all" not in calls


async def test_present_ids_mixed_movie_and_tv() -> None:
    calls: dict[str, int] = {}
    adapter = _adapter(_make_counting_grid_handler(calls), base_url="http://present-mixed:32400")
    present = await adapter.present_ids(
        [(27205, "movie"), (55555, "movie"), (1399, "tv"), (9999, "tv")]
    )
    assert present == frozenset({(27205, "movie"), (1399, "tv")})
    # One movie crawl + one show crawl (+ the shared sections list) -- and still no
    # /children fan-out for the show.
    assert calls["/library/sections/1/all"] == 1
    assert calls["/library/sections/2/all"] == 1
    assert "/library/metadata/100/children" not in calls


async def test_present_ids_second_call_is_served_from_cache() -> None:
    calls: dict[str, int] = {}
    adapter = _adapter(_make_counting_grid_handler(calls), base_url="http://present-cache:32400")
    await adapter.present_ids([(27205, "movie"), (1399, "tv")])
    warmed = calls["_total"]
    # A second identical page load re-uses the warmed movie + show snapshots: zero
    # new HTTP (tiles tolerate the short TTL staleness -- use_cache=True semantics).
    again = await adapter.present_ids([(27205, "movie"), (1399, "tv")])
    assert again == frozenset({(27205, "movie"), (1399, "tv")})
    assert calls["_total"] == warmed


async def test_present_ids_empty_keys_touches_no_network() -> None:
    calls: dict[str, int] = {}
    adapter = _adapter(_make_counting_grid_handler(calls), base_url="http://present-empty:32400")
    assert await adapter.present_ids([]) == frozenset()
    assert calls == {}


async def test_trigger_scan_tv_invalidates_the_show_presence_cache() -> None:
    calls: dict[str, int] = {}
    adapter = _adapter(
        _make_counting_grid_handler(calls), base_url="http://present-invalidate:32400"
    )
    # Warm the show-presence snapshot.
    await adapter.present_ids([(1399, "tv")])
    assert calls["/library/sections/2/all"] == 1
    # A tv scan (a just-imported show) must drop the snapshot so the show promotes to
    # "available" on tiles without waiting the full TTL: the next present_ids re-crawls.
    await adapter.trigger_scan("/data/tv/New Show (2024)", "tv")
    await adapter.present_ids([(1399, "tv")])
    assert calls["/library/sections/2/all"] == 2  # re-crawled, not served stale


async def test_present_ids_refresh_absent_repages_a_still_pending_movie() -> None:
    """P2 (#136 review): the availability reconcile cycle passes
    ``refresh_absent=True`` so a movie that is still indexing when the FIRST crawl
    of a tick runs (caching that absence) is NOT held absent for the rest of the
    300s TTL -- the very next tick must see it once Plex catches up, exactly like
    the old per-row ``is_available`` did."""
    state = {"indexed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        if path == "/library/sections/1/all":
            metadata = list(MOVIES_ALL["MediaContainer"]["Metadata"])
            if state["indexed"]:
                metadata = [
                    *metadata,
                    {"guid": "plex://movie/new", "Guid": [{"id": "tmdb://999999"}]},
                ]
            return httpx.Response(
                200,
                json={"MediaContainer": {"size": len(metadata), "Metadata": metadata}},
            )
        return httpx.Response(404, json={})

    adapter = _adapter(handler, base_url="http://present-refresh-absent:32400")
    # Tick 1: the new movie hasn't finished indexing yet -- the crawl (correctly)
    # reads it absent, and that snapshot is cached for the TTL.
    present = await adapter.present_ids([(999999, "movie")], refresh_absent=True)
    assert present == frozenset()

    # Plex finishes indexing between ticks; nothing invalidates the cache (this is
    # exactly the race the finding describes -- the warm snapshot is now WRONG).
    state["indexed"] = True

    # Tick 2: refresh_absent=True must not trust that warm-but-wrong snapshot --
    # 999999 is still not confirmed present in it, so one fresh crawl runs.
    present = await adapter.present_ids([(999999, "movie")], refresh_absent=True)
    assert present == frozenset({(999999, "movie")})


async def test_present_ids_refresh_absent_still_trusts_a_confirmed_presence() -> None:
    """``refresh_absent=True`` must not force a crawl on EVERY call -- only when
    the snapshot fails to confirm a queried key. A snapshot that already contains
    every queried movie as present is trusted with zero extra HTTP calls."""
    calls: dict[str, int] = {}
    adapter = _adapter(
        _make_counting_grid_handler(calls), base_url="http://present-refresh-ok:32400"
    )
    await adapter.present_ids([(27205, "movie")], refresh_absent=True)
    warmed = calls["_total"]
    # 27205 is already confirmed present in the warm snapshot -- no re-crawl.
    present = await adapter.present_ids([(27205, "movie")], refresh_absent=True)
    assert present == frozenset({(27205, "movie")})
    assert calls["_total"] == warmed


async def test_present_ids_default_still_trusts_a_warm_cache_when_a_key_is_absent() -> None:
    """The tile-decoration default (``refresh_absent=False``) is UNCHANGED: a
    stale absence is harmless for a page-load hint and must not force an extra
    crawl on every page load."""
    calls: dict[str, int] = {}
    adapter = _adapter(_make_counting_grid_handler(calls), base_url="http://present-default:32400")
    await adapter.present_ids([(27205, "movie")])
    warmed = calls["_total"]
    # 55555 is not in the warmed snapshot -- default behavior trusts the cache
    # anyway and does NOT re-page.
    present = await adapter.present_ids([(55555, "movie")])
    assert present == frozenset()
    assert calls["_total"] == warmed


async def test_present_ids_refresh_absent_repages_a_still_pending_show() -> None:
    """Same never-trust-a-cached-absence contract, TV side: a show whose presence
    hasn't been crawled fresh since it finished indexing must not be held absent
    for the rest of the TTL when ``refresh_absent=True``."""
    state = {"indexed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS)
        if path == "/library/sections/2/all":
            metadata = list(SHOWS_ALL["MediaContainer"]["Metadata"])
            if state["indexed"]:
                metadata = [
                    *metadata,
                    {
                        "ratingKey": "200",
                        "guid": "plex://show/new",
                        "Guid": [{"id": "tmdb://8888"}],
                    },
                ]
            return httpx.Response(
                200,
                json={"MediaContainer": {"size": len(metadata), "Metadata": metadata}},
            )
        return httpx.Response(404, json={})

    adapter = _adapter(handler, base_url="http://present-refresh-absent-tv:32400")
    present = await adapter.present_ids([(8888, "tv")], refresh_absent=True)
    assert present == frozenset()

    state["indexed"] = True
    present = await adapter.present_ids([(8888, "tv")], refresh_absent=True)
    assert present == frozenset({(8888, "tv")})


async def test_present_ids_propagates_auth_error() -> None:
    # Honesty: a 401 must PROPAGATE (the router degrades to no-badge on it), never be
    # swallowed into an empty "nothing present" set (the prototype's swallowed-False bug).
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    adapter = _adapter(handler, base_url="http://present-401:32400")
    with pytest.raises(PlexAuthError):
        await adapter.present_ids([(27205, "movie")])


async def test_present_ids_propagates_library_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    adapter = _adapter(handler, base_url="http://present-500:32400")
    with pytest.raises(PlexLibraryError):
        await adapter.present_ids([(1399, "tv")])


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


# Plex reports its section locations in the HOST namespace (``/srv/media/...``),
# while the importer places into the CONTAINER-visible remap (``/media/...``).
SECTIONS_HOST_NAMESPACE: dict[str, Any] = {
    "MediaContainer": {
        "Directory": [
            {
                "key": "1",
                "title": "Movies",
                "type": "movie",
                "Location": [{"path": "/srv/media/Movies"}],
            },
            {
                "key": "4",
                "title": "Movies 4K",
                "type": "movie",
                "Location": [{"path": "/mnt/other/Films"}],
            },
            {
                "key": "2",
                "title": "TV",
                "type": "show",
                "Location": [{"path": "/srv/media/TV"}],
            },
        ]
    }
}


def _make_host_namespace_handler(record: list[tuple[str, str | None]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        path = request.url.path
        if path == "/library/sections":
            return httpx.Response(200, json=SECTIONS_HOST_NAMESPACE)
        match = re.fullmatch(r"/library/sections/(\d+)/refresh", path)
        if match:
            record.append((match.group(1), request.url.params.get("path")))
            return httpx.Response(200)
        return httpx.Response(404, json={})

    return handler


async def test_trigger_scan_reverse_maps_a_remapped_container_path() -> None:
    # The container path ``/media/Movies/Title (2020)`` sits under the container
    # remap (``/media``) of the section's HOST location ``/srv/media/Movies``. A
    # plain prefix check would miss and full-refresh every movie section; the
    # reverse map re-anchors it as the HOST path ``/srv/media/Movies/Title (2020)``
    # Plex actually knows, so ONLY the owning section gets a targeted partial scan.
    record: list[tuple[str, str | None]] = []
    adapter = _adapter(_make_host_namespace_handler(record), base_url="http://scan-remap:32400")
    await adapter.trigger_scan("/media/Movies/Title (2020)", "movie")
    assert record == [("1", "/srv/media/Movies/Title (2020)")]


async def test_trigger_scan_reverse_maps_a_remapped_tv_season_path() -> None:
    record: list[tuple[str, str | None]] = []
    adapter = _adapter(_make_host_namespace_handler(record), base_url="http://scan-remap-tv:32400")
    await adapter.trigger_scan("/media/TV/Some Show (2019)/Season 02", "tv")
    assert record == [("2", "/srv/media/TV/Some Show (2019)/Season 02")]


async def test_trigger_scan_full_refresh_when_container_path_shares_no_directory() -> None:
    # A container path under a mount but sharing NO directory with any section
    # location (e.g. a mount-root remap) has nothing to anchor on: honest full
    # refresh of every movie section, never a wrong-path targeted no-op.
    record: list[tuple[str, str | None]] = []
    adapter = _adapter(_make_host_namespace_handler(record), base_url="http://scan-noanchor:32400")
    await adapter.trigger_scan("/media/Unknown (2021)", "movie")
    assert {key for key, _ in record} == {"1", "4"}
    assert all(scan_path is None for _key, scan_path in record)  # full refresh: no path param


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


@pytest.mark.parametrize("status", [301, 302, 307])
async def test_redirect_status_raises_plex_library_error(status: int) -> None:
    """A 3xx (e.g. a proxy/auth redirect in front of Plex) must be rejected like
    any other non-2xx (issue #87) — ``httpx.Response.is_error`` excludes 3xx, so
    the prior check would have read a redirect as a successful scan/query even
    though it never actually reached Plex."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"Location": "/web/login"})

    adapter = _adapter(handler, base_url=f"http://redirect-{status}:32400")
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


def test_package_root_re_exports_typed_errors() -> None:
    """The Plex adapter package root must re-export ``PlexAuthError`` and
    ``PlexLibraryError`` (issue #113), matching every sibling adapter package
    (``prowlarr``, ``tmdb``, ``qbittorrent``) which all expose their public
    typed errors at ``__init__``, not just their implementation submodule — a
    caller following that established contract must not hit an ``ImportError``
    for the Plex adapter alone."""
    from plex_manager.adapters.plex import PlexAuthError as RootAuthError
    from plex_manager.adapters.plex import PlexLibrary as RootLibrary
    from plex_manager.adapters.plex import PlexLibraryError as RootLibraryError

    assert RootAuthError is PlexAuthError
    assert RootLibraryError is PlexLibraryError
    assert RootLibrary is PlexLibrary


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


async def test_list_sections_invalidates_a_stale_movie_cache_when_the_library_is_removed() -> None:
    # R6-E: list_sections only ever SET the cache on a movie-bearing result,
    # never CLEARED it -- so once a movie-bearing snapshot was cached, a LATER
    # re-page (esp. a live use_cache=False probe) that finds no movie section
    # left the OLD positive sitting in the cache, untouched. Default
    # (use_cache=True) callers -- the Settings folder picker, the scan path --
    # kept being handed the removed movie location for up to the ~300s TTL. A
    # no-movie page must INVALIDATE the cache key instead of merely skipping
    # the ``set``, so the very next default call re-pages rather than serving
    # the stale positive.
    calls = {"n": 0}
    state = {"has_movie": True}

    def switching(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        if request.url.path == "/library/sections":
            calls["n"] += 1
            return httpx.Response(200, json=SECTIONS if state["has_movie"] else SECTIONS_NO_MOVIE)
        return httpx.Response(404, json={})

    adapter = _adapter(switching, base_url="http://movie-removed-plex:32400")
    # Warm the cache with a movie-bearing result.
    first = await adapter.list_sections()
    assert any(s.type == "movie" for s in first)
    assert calls["n"] == 1

    # The operator removes the Movie library from Plex; a live use_cache=False
    # probe (e.g. "Test connection") re-pages and sees it gone.
    state["has_movie"] = False
    second = await adapter.list_sections(use_cache=False)
    assert all(s.type != "movie" for s in second)
    assert calls["n"] == 2

    # A DEFAULT (use_cache=True) call must re-page too -- the stale movie
    # positive from the FIRST call must not still be sitting in the cache.
    third = await adapter.list_sections()
    assert calls["n"] == 3  # re-fetched -- never served from the stale cache
    assert all(s.type != "movie" for s in third)


async def test_list_sections_use_cache_false_sees_a_newly_added_second_movie_section() -> None:
    # Issue #15: warm the cache with 1 movie section (an already-cached
    # movie-bearing snapshot), then the operator adds a 2nd movie library in
    # Plex. A default (use_cache=True) call would still serve the OLD,
    # shorter, cached list for up to the remaining TTL -- but a caller that
    # needs the picker to be current RIGHT NOW (settings.plex_libraries_endpoint)
    # passes use_cache=False and must see both sections immediately.
    calls = {"n": 0}
    state = {"two_movie": False}

    def switching(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Plex-Token") == TOKEN
        assert TOKEN not in str(request.url)
        if request.url.path == "/library/sections":
            calls["n"] += 1
            return httpx.Response(200, json=SECTIONS_TWO_MOVIE if state["two_movie"] else SECTIONS)
        return httpx.Response(404, json={})

    adapter = _adapter(switching, base_url="http://second-movie-lib-plex:32400")
    # Warm the cache with 1 movie section.
    first = await adapter.list_sections()
    assert len([s for s in first if s.type == "movie"]) == 1
    assert calls["n"] == 1

    # Operator adds a 2nd movie library in Plex.
    state["two_movie"] = True
    second = await adapter.list_sections(use_cache=False)
    assert calls["n"] == 2  # bypassed the stale 1-movie-section cache
    assert len([s for s in second if s.type == "movie"]) == 2


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
