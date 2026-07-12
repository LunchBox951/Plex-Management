"""TmdbMetadata.season_episodes tests (ADR-0020, issue #178).

Mirrors ``test_tmdb_adapter.py``'s ``httpx.MockTransport`` pattern. No real
network in the default run.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from plex_manager.adapters.tmdb import TmdbApiError, TmdbAuthError, TmdbMetadata

API_KEY = "test-key-never-logged"

SEASON_DETAIL: dict[str, Any] = {
    "id": 12345,
    "season_number": 4,
    "episodes": [
        {"episode_number": 1, "air_date": "2024-01-05", "name": "Ep 1"},
        {"episode_number": 2, "air_date": "2024-01-12", "name": "Ep 2"},
        # Unaired episode: TMDB returns "" (not a missing key) for the date.
        {"episode_number": 3, "air_date": "", "name": "Ep 3"},
        # Missing air_date key entirely -- also treated as unaired.
        {"episode_number": 4, "name": "Ep 4"},
    ],
}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    assert request.url.params.get("api_key") == API_KEY
    if path == "/3/tv/12345/season/4":
        return httpx.Response(200, json=SEASON_DETAIL)
    if path == "/3/tv/99999/season/1":
        return httpx.Response(404, json={"status_message": "not found"})
    if path == "/3/tv/401/season/1":
        return httpx.Response(401, json={"status_message": "Invalid API key"})
    if path == "/3/tv/500/season/1":
        return httpx.Response(500, json={"status_message": "server error"})
    return httpx.Response(404, json={"status_message": "unhandled"})


def _adapter() -> TmdbMetadata:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return TmdbMetadata(client, API_KEY)


async def test_season_episodes_maps_episode_number_and_air_date() -> None:
    episodes = await _adapter().season_episodes(12345, 4)

    by_number = {e.episode_number: e.air_date for e in episodes}
    assert by_number == {
        1: date(2024, 1, 5),
        2: date(2024, 1, 12),
        3: None,
        4: None,
    }


async def test_season_episodes_caches_second_call() -> None:
    calls = {"n": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(counting_handler))
    adapter = TmdbMetadata(client, API_KEY)
    first = await adapter.season_episodes(12345, 4)
    second = await adapter.season_episodes(12345, 4)

    assert first == second
    assert calls["n"] == 1


async def test_season_episodes_cache_returns_independent_lists() -> None:
    # Issue #106: mutating one caller's returned list must not corrupt a later
    # cache hit's list.
    adapter = _adapter()
    first = await adapter.season_episodes(12345, 4)
    first.clear()
    second = await adapter.season_episodes(12345, 4)
    assert len(second) == 4


async def test_season_episodes_401_raises_tmdb_auth_error() -> None:
    with pytest.raises(TmdbAuthError) as exc_info:
        await _adapter().season_episodes(401, 1)
    assert API_KEY not in str(exc_info.value)


async def test_season_episodes_500_raises_tmdb_api_error() -> None:
    with pytest.raises(TmdbApiError) as exc_info:
        await _adapter().season_episodes(500, 1)
    assert API_KEY not in str(exc_info.value)


async def test_season_episodes_404_raises_tmdb_api_error() -> None:
    """A 404 on the season-detail route means the route/tmdb id/season is wrong,
    NOT "no episodes" -- surfaced, never silently mapped to an empty list
    (issue #89 pattern, ADR-0020's "never guess a target")."""
    with pytest.raises(TmdbApiError) as exc_info:
        await _adapter().season_episodes(99999, 1)
    assert API_KEY not in str(exc_info.value)
