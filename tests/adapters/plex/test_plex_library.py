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
from typing import Any

import httpx
import pytest

from plex_manager.adapters.plex import PlexLibrary
from plex_manager.adapters.plex.library import (
    PlexAuthError,
    PlexLibraryError,
    reset_caches,
)

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


async def test_is_available_tv_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError, match="tv availability deferred"):
        await _adapter(_main_handler).is_available(1399, "tv")


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
    await adapter.trigger_scan(scan_path)
    # Only section 1 (whose /data/movies location is a parent) is refreshed, and the
    # path is round-tripped intact (single percent-encoding via httpx params).
    assert record == [("1", scan_path)]


async def test_trigger_scan_falls_back_to_all_movie_sections() -> None:
    record: list[tuple[str, str | None]] = []
    adapter = _adapter(_make_trigger_handler(record), base_url="http://scan-all:32400")
    await adapter.trigger_scan("/somewhere/unmapped/x.mkv")
    # No location matches -> every movie section (1 and 4, not the show section).
    assert {key for key, _ in record} == {"1", "4"}


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
