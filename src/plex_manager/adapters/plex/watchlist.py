"""Plex cloud universal-watchlist adapter.

The cloud endpoint is intentionally isolated here because Plex does not include
it in the documented PMS API.  A failed or partial fetch raises; callers must
retain their last complete snapshot rather than interpreting failure as an empty
watchlist.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final, Literal, cast

import httpx

from plex_manager.headersafe import is_header_safe
from plex_manager.ports.watchlist import WatchlistEntry

__all__ = ["PlexWatchlist", "PlexWatchlistAuthError", "PlexWatchlistError"]

_BASE_URL: Final = "https://discover.provider.plex.tv"
_PATH: Final = "/library/sections/watchlist/all"
_METADATA_PATH: Final = "/library/metadata"
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


def _media_type(item: Mapping[str, object]) -> Literal["movie", "tv"] | None:
    wire_type = item.get("type")
    if wire_type == "show":
        return "tv"
    if wire_type == "movie":
        return "movie"
    return None


def _rating_key(item: Mapping[str, object]) -> str | None:
    value = item.get("ratingKey")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value:
        return value
    return None


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
            total = container.get("totalSize")
            if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                raise PlexWatchlistError("Plex watchlist response has invalid totalSize")
            raw_metadata = container.get("Metadata")
            if raw_metadata is None and total == 0:
                raw_items: Sequence[object] = ()
            elif isinstance(raw_metadata, (list, tuple)):
                raw_items = cast("Sequence[object]", raw_metadata)
            else:
                raise PlexWatchlistError("Plex watchlist response has invalid Metadata")
            for raw in raw_items:
                resolved = await self._resolve_entry(_mapping(raw))
                if resolved is None:
                    continue
                entries[(resolved.tmdb_id, resolved.media_type)] = resolved
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

    async def _resolve_entry(self, item: Mapping[str, object]) -> WatchlistEntry | None:
        """Resolve one watchlist row to a supported ``(tmdb_id, media_type)`` entry.

        The watchlist page frequently returns rows that carry only a
        ``ratingKey`` -- the ``type`` and ``Guid`` fields we need are absent
        until the item's own metadata is fetched. Treating such a row as
        "nothing to protect" is exactly the bug that lets a first sync commit
        an EMPTY snapshot (wiping eviction protection) even though the account
        has a full watchlist. So when the row alone does not yield both a
        supported ``type`` and a TMDB id, fall back to the item's
        ``/library/metadata/{ratingKey}`` detail before deciding to skip it.
        A row that resolves to no supported type or no TMDB id even after the
        detail fetch is genuinely not requestable and is skipped; only actual
        fetch/transport failures raise (so callers retain their last snapshot).
        """
        media_type = _media_type(item)
        if media_type is not None:
            tmdb_id = _tmdb_id(item)
            if tmdb_id is not None:
                return WatchlistEntry(tmdb_id=tmdb_id, media_type=media_type)
        elif item.get("type") is not None:
            # A present-but-unsupported type (clip, episode, ...) will not become
            # a movie/show by fetching its detail, so skip it without the round
            # trip. Only rows that OMIT ``type`` (the ratingKey-only case) or are
            # a supported type still missing their Guid are worth resolving.
            return None
        rating_key = _rating_key(item)
        if rating_key is None:
            return None
        detail = await self._fetch_details(rating_key)
        if detail is None:
            return None
        media_type = _media_type(detail)
        if media_type is None:
            return None
        tmdb_id = _tmdb_id(detail)
        if tmdb_id is None:
            return None
        return WatchlistEntry(tmdb_id=tmdb_id, media_type=media_type)

    async def _fetch_details(self, rating_key: str) -> Mapping[str, object] | None:
        """Fetch a single watchlist item's full metadata (``type``/``Guid``).

        Returns the item mapping, or ``None`` when the response is well formed
        but carries no metadata for the key (a genuinely unresolvable item that
        the caller then skips). Transport errors, auth rejections, non-2xx
        statuses, and undecodable bodies RAISE -- the module contract is that a
        partial/failed fetch must never be mistaken for an empty watchlist.
        """
        try:
            response = await self._client.get(
                f"{_BASE_URL}{_METADATA_PATH}/{rating_key}",
                headers={"Accept": "application/json", "X-Plex-Token": self._token},
                params={"includeExternalMedia": 1},
            )
        except httpx.HTTPError as exc:
            raise PlexWatchlistError("Plex watchlist item is unreachable") from exc
        if response.status_code in {401, 403}:
            raise PlexWatchlistAuthError("Plex rejected the watchlist credential")
        if not 200 <= response.status_code < 300:
            raise PlexWatchlistError(f"Plex watchlist item returned status {response.status_code}")
        try:
            payload = cast(object, response.json())
        except (json.JSONDecodeError, ValueError) as exc:
            raise PlexWatchlistError("Plex watchlist item returned invalid JSON") from exc
        raw_container = _mapping(payload).get("MediaContainer")
        if not isinstance(raw_container, Mapping):
            raise PlexWatchlistError("Plex watchlist item is missing MediaContainer")
        metadata = _sequence(cast("Mapping[str, object]", raw_container).get("Metadata"))
        # The detail endpoint returns the requested item as the sole (or first)
        # Metadata entry; prefer an exact ratingKey match, else fall back to the
        # first entry. An empty Metadata list means the item is unresolvable.
        for raw in metadata:
            detail = _mapping(raw)
            if _rating_key(detail) == rating_key:
                return detail
        return _mapping(metadata[0]) if metadata else None
