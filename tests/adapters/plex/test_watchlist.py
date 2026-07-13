from __future__ import annotations

import httpx
import pytest

from plex_manager.adapters.plex.watchlist import (
    PlexWatchlist,
    PlexWatchlistAuthError,
    PlexWatchlistError,
)
from plex_manager.ports.watchlist import WatchlistEntry

TOKEN = "watchlist-test-token"  # noqa: S105


async def test_lists_movie_and_show_tmdb_guids_with_header_token() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 3,
                    "Metadata": [
                        {"type": "movie", "Guid": [{"id": "tmdb://603"}]},
                        {"type": "show", "Guid": [{"id": "themoviedb://1396"}]},
                        {"type": "clip", "Guid": [{"id": "tmdb://1"}]},
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        entries = await PlexWatchlist(client, TOKEN).list_entries()

    assert entries == (
        WatchlistEntry(tmdb_id=603, media_type="movie"),
        WatchlistEntry(tmdb_id=1396, media_type="tv"),
    )
    assert seen[0].headers["X-Plex-Token"] == TOKEN
    assert TOKEN not in str(seen[0].url)


async def test_auth_failure_is_typed() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PlexWatchlistAuthError):
            await PlexWatchlist(client, TOKEN).list_entries()


async def test_invalid_json_is_typed() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, text="not-json"))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(PlexWatchlistError):
            await PlexWatchlist(client, TOKEN).list_entries()


async def test_paginates_until_total_size() -> None:
    starts: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["X-Plex-Container-Start"])
        starts.append(start)
        count = 100 if start == 0 else 1
        return httpx.Response(
            200,
            json={
                "MediaContainer": {
                    "totalSize": 101,
                    "Metadata": [
                        {"type": "movie", "Guid": [{"id": f"tmdb://{start + i + 1}"}]}
                        for i in range(count)
                    ],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        entries = await PlexWatchlist(client, TOKEN).list_entries()
    assert len(entries) == 101
    assert starts == [0, 100]
