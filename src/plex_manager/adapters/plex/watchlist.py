"""Plex cloud universal-watchlist adapter.

The cloud endpoint is intentionally isolated here because Plex does not include
it in the documented PMS API.  A failed or partial fetch raises; callers must
retain their last complete snapshot rather than interpreting failure as an empty
watchlist.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final, cast

import httpx

from plex_manager.headersafe import is_header_safe
from plex_manager.ports.watchlist import WatchlistEntry

__all__ = ["PlexWatchlist", "PlexWatchlistAuthError", "PlexWatchlistError"]

_BASE_URL: Final = "https://metadata.provider.plex.tv"
_PATH: Final = "/library/sections/watchlist/all"
_PAGE_SIZE: Final = 100
_TMDB_PREFIXES: Final = ("tmdb://", "themoviedb://")


class PlexWatchlistError(RuntimeError):
    """The watchlist could not be fetched completely or decoded safely."""


class PlexWatchlistAuthError(PlexWatchlistError):
    """Plex rejected the account token."""


def _mapping(value: object) -> Mapping[str, object]:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return cast("Sequence[object]", value) if isinstance(value, (list, tuple)) else ()


def _tmdb_id(item: Mapping[str, object]) -> int | None:
    guid_values: list[str] = []
    direct = item.get("guid")
    if isinstance(direct, str):
        guid_values.append(direct)
    for raw in _sequence(item.get("Guid")):
        guid = _mapping(raw).get("id")
        if isinstance(guid, str):
            guid_values.append(guid)
    for guid in guid_values:
        for prefix in _TMDB_PREFIXES:
            if guid.startswith(prefix) and guid[len(prefix) :].isdecimal():
                return int(guid[len(prefix) :])
    return None


class PlexWatchlist:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        if not is_header_safe(token):
            raise PlexWatchlistAuthError("Plex token is not a valid credential value")
        self._client = client
        self._token = token

    async def list_entries(self) -> tuple[WatchlistEntry, ...]:
        entries: dict[tuple[int, str], WatchlistEntry] = {}
        start = 0
        while True:
            try:
                response = await self._client.get(
                    f"{_BASE_URL}{_PATH}",
                    headers={"Accept": "application/json", "X-Plex-Token": self._token},
                    params={
                        "X-Plex-Container-Start": start,
                        "X-Plex-Container-Size": _PAGE_SIZE,
                        "includeCollections": 1,
                        "includeExternalMedia": 1,
                    },
                )
            except httpx.HTTPError as exc:
                raise PlexWatchlistError("Plex watchlist is unreachable") from exc
            if response.status_code in {401, 403}:
                raise PlexWatchlistAuthError("Plex rejected the watchlist credential")
            if not 200 <= response.status_code < 300:
                raise PlexWatchlistError(f"Plex watchlist returned status {response.status_code}")
            try:
                payload = cast(object, response.json())
            except (json.JSONDecodeError, ValueError) as exc:
                raise PlexWatchlistError("Plex watchlist returned invalid JSON") from exc
            root = _mapping(payload)
            raw_container = root.get("MediaContainer")
            if not isinstance(raw_container, Mapping):
                raise PlexWatchlistError("Plex watchlist response is missing MediaContainer")
            container = cast("Mapping[str, object]", raw_container)
            raw_metadata = container.get("Metadata")
            if not isinstance(raw_metadata, (list, tuple)):
                raise PlexWatchlistError("Plex watchlist response has invalid Metadata")
            raw_items = cast("Sequence[object]", raw_metadata)
            total = container.get("totalSize")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise PlexWatchlistError("Plex watchlist response has invalid totalSize")
            for raw in raw_items:
                item = _mapping(raw)
                wire_type = item.get("type")
                if wire_type == "show":
                    media_type = "tv"
                elif wire_type == "movie":
                    media_type = "movie"
                else:
                    continue
                tmdb_id = _tmdb_id(item)
                if tmdb_id is None:
                    continue
                entry = WatchlistEntry(tmdb_id=tmdb_id, media_type=media_type)
                entries[(tmdb_id, media_type)] = entry
            size = len(raw_items)
            next_start = start + size
            if next_start < total and size == 0:
                raise PlexWatchlistError("Plex watchlist pagination did not advance")
            if next_start < total:
                start = next_start
                continue
            if next_start > total:
                raise PlexWatchlistError("Plex watchlist page exceeds declared totalSize")
            break
        return tuple(entries.values())
