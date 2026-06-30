"""ProwlarrIndexer adapter tests — recorded ``/api/v1/search`` shapes.

The JSON array mirrors a real Prowlarr aggregated search (field names verified
against ``Prowlarr.Api.V1/Search/ReleaseResource.cs`` — note it carries NO
``indexerPriority`` field): multiple indexers, a duplicate ``guid`` across two
indexers (de-dup keeps the lower priority, resolved out-of-band from
``/api/v1/indexer``), one entry with neither a magnet nor a download url (skipped
with a warning), and a malformed publish date (tolerated). No real network in the
default run; an OPTIONAL live smoke test is env-guarded.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from plex_manager.adapters.prowlarr import IndexerRateLimitError, ProwlarrIndexer
from plex_manager.domain.release import IndexerSearchRequest

API_KEY = "prowlarr-key-never-logged"
BASE_URL = "http://prowlarr.local:9696"

# A recorded /api/v1/search payload. The real ``ReleaseResource`` does NOT carry
# any priority — that lives only on the indexer (resolved below). The worse-priority
# duplicate is listed FIRST so the test proves priority-based selection rather than
# mere first-appearance-wins.
SEARCH_RESULTS: list[dict[str, Any]] = [
    {
        # Same guid as the next row, served by a *worse* (higher) priority indexer
        # and listed first — de-dup must still discard this one.
        "guid": "https://indexer-a/details/1",
        "title": "Inception 2010 1080p BluRay x264-GROUP",
        "size": 8589934592,
        "indexerId": 2,
        "indexer": "Indexer B",
        "downloadUrl": "https://indexer-b/download/1.torrent",
        "seeders": 5,
        "peers": 9,
        "publishDate": "2023-01-02T03:04:05Z",
        "protocol": "torrent",
    },
    {
        "guid": "https://indexer-a/details/1",
        "title": "Inception 2010 1080p BluRay x264-GROUP",
        "size": 8589934592,
        "indexerId": 1,
        "indexer": "Indexer A",
        "magnetUrl": "magnet:?xt=urn:btih:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "infoHash": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "seeders": 120,
        "leechers": 8,
        "publishDate": "2023-01-02T03:04:05Z",
        "imdbId": 1375666,
        "tmdbId": 27205,
        "categories": [{"id": 2000, "name": "Movies"}, {"id": 2040, "name": "HD"}],
        "protocol": "torrent",
    },
    {
        "guid": "https://indexer-c/details/9",
        "title": "Some Other Movie 2021 2160p WEB-DL",
        "size": 21474836480,
        "indexerId": 3,
        "indexer": "Indexer C",
        "downloadUrl": "https://indexer-c/download/9.torrent",
        "seeders": 50,
        "peers": 60,  # leechers derived as peers - seeders = 10
        "publishDate": "not-a-date",  # tolerated -> epoch
        "protocol": "torrent",
    },
    {
        # No magnet AND no download url -> skipped with a warning, never raised.
        "guid": "https://indexer-d/details/3",
        "title": "Broken Release No Urls",
        "size": 100,
        "indexerId": 4,
        "indexer": "Indexer D",
        "publishDate": "2023-05-05T00:00:00Z",
        "protocol": "torrent",
    },
]

# A recorded /api/v1/indexer payload (IndexerResource: id, name, priority, …).
# This is where the indexer priority actually lives on the wire.
INDEXERS: list[dict[str, Any]] = [
    {"id": 1, "name": "Indexer A", "priority": 10, "enable": True, "protocol": "torrent"},
    {"id": 2, "name": "Indexer B", "priority": 40, "enable": True, "protocol": "torrent"},
    {"id": 3, "name": "Indexer C", "priority": 25, "enable": True, "protocol": "torrent"},
]


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("X-Api-Key") == API_KEY
    if request.url.path == "/api/v1/indexer":
        return httpx.Response(200, json=INDEXERS)
    assert request.url.path == "/api/v1/search"
    return httpx.Response(200, json=SEARCH_RESULTS)


def _adapter(handler: Any = _handler) -> ProwlarrIndexer:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ProwlarrIndexer(client, BASE_URL, API_KEY)


async def test_search_maps_and_dedupes_by_guid() -> None:
    results = await _adapter().search(IndexerSearchRequest(media_type="movie", tmdb_id=27205))
    # 4 rows in -> dup collapsed, no-url row skipped -> 2 candidates out.
    assert len(results) == 2
    first, second = results
    assert first.guid == "https://indexer-a/details/1"
    assert first.indexer_priority == 10  # kept the lower-priority duplicate
    assert first.indexer_name == "Indexer A"
    assert first.info_hash == "a" * 40  # lowercased
    assert first.size_bytes == 8589934592
    assert first.seeders == 120
    assert first.leechers == 8
    assert first.categories == [2000, 2040]
    assert first.imdb_id == 1375666
    assert first.tmdb_id == 27205
    assert first.publish_date.year == 2023
    assert second.leechers == 10  # derived from peers - seeders
    assert second.publish_date.year == 1970  # malformed date -> epoch


async def test_search_degrades_when_indexer_priorities_unavailable() -> None:
    # /api/v1/indexer failing must NOT abort the search; de-dup falls back to
    # first-appearance-wins and every candidate gets the default priority.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=SEARCH_RESULTS)

    results = await _adapter(handler).search(
        IndexerSearchRequest(media_type="movie", tmdb_id=27205)
    )
    assert len(results) == 2
    # First appearance of the duplicate guid is Indexer B (listed first); with no
    # priorities to compare, first-wins keeps it, and priority is the default.
    assert results[0].indexer_name == "Indexer B"
    assert results[0].indexer_priority == 25


async def test_search_builds_expected_query_params() -> None:
    captured: dict[str, list[tuple[str, str]]] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=INDEXERS)
        captured["params"] = list(request.url.params.multi_items())
        return httpx.Response(200, json=[])

    await _adapter(capture).search(
        IndexerSearchRequest(
            media_type="tv",
            tmdb_id=1399,
            imdb_id="944947",
            season=2,
            episode="5",
            categories=[5000, 5040],
            indexer_ids=[7],
        )
    )
    params = dict(captured["params"])
    multi = captured["params"]
    assert ("type", "tvsearch") in multi
    assert ("tmdbid", "1399") in multi
    assert ("imdbid", "tt0944947") in multi  # zero-padded to 7 digits
    assert ("season", "2") in multi
    assert ("ep", "5") in multi
    assert ("categories", "5000") in multi
    assert ("categories", "5040") in multi
    assert ("indexerIds", "7") in multi
    assert "query" not in params  # id-based search: no free-text query


async def test_search_omits_categories_when_empty() -> None:
    captured: dict[str, str] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=INDEXERS)
        captured["raw"] = str(request.url)
        return httpx.Response(200, json=[])

    await _adapter(capture).search(IndexerSearchRequest(media_type="search", query="dune"))
    assert "categories" not in captured["raw"]
    assert "query=dune" in captured["raw"]


async def test_rate_limited_400_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "all indexers rate-limited"})

    with pytest.raises(IndexerRateLimitError):
        await _adapter(handler).search(IndexerSearchRequest(query="x"))


async def test_rate_limit_error_excludes_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400)

    try:
        await _adapter(handler).search(IndexerSearchRequest(query="x"))
    except IndexerRateLimitError as exc:
        assert API_KEY not in str(exc)
    else:  # pragma: no cover - guarded by the call above
        pytest.fail("expected IndexerRateLimitError")


def test_adapter_satisfies_indexer_port() -> None:
    from plex_manager.ports.indexer import IndexerPort

    assert isinstance(_adapter(), IndexerPort)


@pytest.mark.skipif(
    not os.getenv("PLEX_MANAGER_LIVE_TESTS"),
    reason="live Prowlarr smoke test; set PLEX_MANAGER_LIVE_TESTS + PROWLARR_URL/API_KEY",
)
async def test_live_smoke_search() -> None:  # pragma: no cover - live only
    base_url = os.environ.get("PROWLARR_URL")
    api_key = os.environ.get("PROWLARR_API_KEY")
    if not base_url or not api_key:
        pytest.skip("PROWLARR_URL / PROWLARR_API_KEY not set")
    async with httpx.AsyncClient(timeout=300.0) as client:
        adapter = ProwlarrIndexer(client, base_url, api_key)
        results = await adapter.search(IndexerSearchRequest(media_type="movie", tmdb_id=27205))
        assert isinstance(results, list)
