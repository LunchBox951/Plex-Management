"""PlexLibrary — the :class:`LibraryPort` implementation backed by the Plex API.

The adapter answers three questions for the import/availability pipeline:
"which sections exist", "is this tmdb id already in the library", and "scan this
path". It maps Plex's ``MediaContainer`` JSON into the frozen domain DTO
(:class:`LibrarySection`) and never leaks Plex's wire shape past this module.

Construction is dependency-injected: ``base_url``, ``token`` and an
``httpx.AsyncClient`` are passed in (the web/services layer wires decrypted creds
later). The token is sent in the ``X-Plex-Token`` header — NEVER in the URL (which
could be logged) — and is redacted from ``repr``.

Caching: the web layer builds a fresh adapter per request, so the section list and
the set of present tmdb ids live in MODULE-LEVEL TTL caches keyed by ``base_url``
(a per-instance cache would never be hit). The TTL is short — availability changes
when a scan completes, and a stale "present" answer for a few minutes is harmless.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from typing import Final, Literal, cast

import httpx

from plex_manager.ports.library import LibrarySection

__all__ = ["PlexAuthError", "PlexLibrary", "PlexLibraryError"]

_logger = logging.getLogger(__name__)

_HTTP_UNAUTHORIZED: Final = 401
_HTTP_FORBIDDEN: Final = 403
_PAGE_SIZE: Final = 100
_CACHE_TTL_SECONDS: Final = 300.0

# Plex stores agent ids as ``<agent>://<id>``. The modern Movie agent uses
# ``tmdb://``; the legacy "The Movie Database" agent uses ``themoviedb://``. Both
# are matched (verified against overseerr's plex scanner GUID regex set).
_TMDB_GUID_RE: Final = re.compile(r"(?:tmdb|themoviedb)://([0-9]+)")


class PlexLibraryError(RuntimeError):
    """Raised when Plex returns a non-2xx status other than 401/403, is
    unreachable, or returns a non-JSON 200.

    A surfaced, retryable error. The message names the request *path* and status
    code only — never the full URL (the token travels in a header, but the path is
    all the message needs). Letting httpx's transport/status error escape would
    surface as an opaque 500, so it is converted at the boundary.
    """


class PlexAuthError(RuntimeError):
    """Raised when Plex rejects the token (HTTP 401/403).

    A clear, surfaced error — never a silent empty result. The message names the
    cause but never includes the token.
    """


class _TtlCache[V]:
    """A minimal monotonic-clock TTL cache (hit-on-fresh, evict-on-expired).

    Only successful results are stored; misses (``None``) are not cached, so the
    sentinel ambiguity between "absent" and "cached None" never arises.
    """

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, V]] = {}

    def get(self, key: str) -> V | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= time.monotonic():
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: V) -> None:
        self._store[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        self._store.clear()

    def invalidate(self, key: str) -> None:
        """Drop one key so the next ``get`` re-fetches (e.g. after a Plex scan)."""
        self._store.pop(key, None)


# Module-level caches (keyed by base_url) — see the module docstring for why these
# cannot be per-instance.
_SECTIONS_CACHE: _TtlCache[tuple[LibrarySection, ...]] = _TtlCache(_CACHE_TTL_SECONDS)
_PRESENT_TMDB_CACHE: _TtlCache[frozenset[int]] = _TtlCache(_CACHE_TTL_SECONDS)


def reset_caches() -> None:
    """Clear the module-level caches. Test-isolation helper (not part of the port)."""
    _SECTIONS_CACHE.clear()
    _PRESENT_TMDB_CACHE.clear()


def _as_mapping(value: object) -> Mapping[str, object]:
    """Narrow an untyped JSON node to a string-keyed mapping (else empty)."""
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    return {}


def _as_sequence(value: object) -> Sequence[object]:
    """Narrow an untyped JSON node to a sequence (str is not a sequence here)."""
    if isinstance(value, (list, tuple)):
        return cast("Sequence[object]", value)
    return ()


def _get_str(fields: Mapping[str, object], key: str) -> str | None:
    value = fields.get(key)
    return value if isinstance(value, str) and value else None


def _media_container(payload: Mapping[str, object]) -> Mapping[str, object]:
    """Unwrap Plex's top-level ``MediaContainer`` envelope."""
    return _as_mapping(payload.get("MediaContainer"))


def _parse_section(entry: Mapping[str, object]) -> LibrarySection | None:
    """Map one ``Directory`` row to a :class:`LibrarySection` (None if unusable)."""
    key = _get_str(entry, "key")
    title = _get_str(entry, "title")
    section_type = _get_str(entry, "type")
    if key is None or title is None or section_type not in ("movie", "show"):
        return None
    locations = tuple(
        path
        for loc in _as_sequence(entry.get("Location"))
        if (path := _get_str(_as_mapping(loc), "path")) is not None
    )
    return LibrarySection(key=key, title=title, type=section_type, locations=locations)


def _extract_tmdb_ids(text: str) -> list[int]:
    """Pull every ``tmdb://`` / ``themoviedb://`` numeric id out of a guid string."""
    return [int(match) for match in _TMDB_GUID_RE.findall(text)]


def _collect_item_tmdb_ids(item: Mapping[str, object], present: set[int]) -> None:
    """Add the tmdb ids of one ``Metadata`` item to ``present``.

    Plex exposes ids both on the legacy scalar ``guid`` field and (with
    ``includeGuids=1``) on the ``Guid[]`` array of ``{"id": "<agent>://<id>"}``
    rows; both are scanned so either agent layout resolves.
    """
    guid = _get_str(item, "guid")
    if guid is not None:
        present.update(_extract_tmdb_ids(guid))
    for guid_entry in _as_sequence(item.get("Guid")):
        gid = _get_str(_as_mapping(guid_entry), "id")
        if gid is not None:
            present.update(_extract_tmdb_ids(gid))


def _section_covers(section: LibrarySection, path: str) -> bool:
    """Whether any of the section's locations is a path-prefix of ``path``."""
    return any(_is_path_prefix(location, path) for location in section.locations)


def _is_path_prefix(prefix: str, path: str) -> bool:
    """True if ``prefix`` is ``path`` or a parent directory of it (segment-aware).

    ``/data/movies`` covers ``/data/movies/Foo/foo.mkv`` but not ``/data/movies-4k``.
    """
    norm = prefix.rstrip("/")
    return path == norm or path.startswith(f"{norm}/")


class PlexLibrary:
    """Query availability, trigger scans and list sections. Implements ``LibraryPort``.

    The token is held privately, sent only in the ``X-Plex-Token`` header, and
    redacted from ``repr`` so it cannot leak into logs.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token

    def __repr__(self) -> str:  # pragma: no cover - trivial, redacts the token
        return f"PlexLibrary(base_url={self._base_url!r}, token='***')"

    async def _request(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """GET ``path`` with the token header; raise typed errors on failure.

        The token is added here and never logged. A transport failure, a 401/403,
        or any other non-2xx is converted to a surfaced error built from ``path``
        and the status only — httpx's own error embeds the URL, so it must never
        escape. JSON is NOT decoded here (refresh returns an empty body).
        """
        request_headers: dict[str, str] = {
            "X-Plex-Token": self._token,
            "Accept": "application/json",
        }
        if headers is not None:
            request_headers.update(headers)
        try:
            response = await self._client.get(
                f"{self._base_url}{path}", params=params, headers=request_headers
            )
        except httpx.RequestError as exc:
            # Plex unreachable (DNS / connection refused / timeout): httpx raises
            # before any status check, so without this it would propagate as an
            # opaque 500. Convert to the surfaced, retryable PlexLibraryError —
            # the message names the path only, never the url or token.
            raise PlexLibraryError(f"plex request to {path} failed") from exc
        status = response.status_code
        if status in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            raise PlexAuthError(
                f"Plex rejected the request to {path} (HTTP {status}): "
                "the token is missing or invalid"
            )
        if response.is_error:
            raise PlexLibraryError(f"Plex request to {path} failed (HTTP {status})")
        return response

    async def _get(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        """:meth:`_request` plus JSON decoding (non-JSON 200 -> ``PlexLibraryError``)."""
        response = await self._request(path, params, headers=headers)
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            # A 200 with a non-JSON body (a reverse-proxy / auth HTML page in front
            # of Plex) would otherwise raise a raw JSONDecodeError that bypasses the
            # PlexLibraryError handler and surfaces as an opaque 500. Convert it at
            # the boundary — the message names the path only, never the url or token.
            raise PlexLibraryError(f"Plex returned a non-JSON body for {path}") from exc
        return _as_mapping(payload)

    async def list_sections(self) -> list[LibrarySection]:
        """Return the configured library sections (movie / show), cached briefly."""
        cached = _SECTIONS_CACHE.get(self._base_url)
        if cached is not None:
            return list(cached)
        payload = await self._get("/library/sections")
        container = _media_container(payload)
        sections: list[LibrarySection] = []
        for entry in _as_sequence(container.get("Directory")):
            section = _parse_section(_as_mapping(entry))
            if section is not None:
                sections.append(section)
        _SECTIONS_CACHE.set(self._base_url, tuple(sections))
        return sections

    async def is_available(self, tmdb_id: int, media_type: Literal["movie", "tv"]) -> bool:
        """Whether ``tmdb_id`` is already present in the library.

        TV availability needs per-season presence logic and is deferred — it raises
        honestly rather than returning a misleading ``False``.
        """
        if media_type == "tv":
            raise NotImplementedError("tv availability deferred to next beta")
        present = _PRESENT_TMDB_CACHE.get(self._base_url)
        if present is None:
            present = await self._collect_present_tmdb_ids()
            _PRESENT_TMDB_CACHE.set(self._base_url, present)
        return tmdb_id in present

    async def _collect_present_tmdb_ids(self) -> frozenset[int]:
        """Page every movie section and gather the tmdb ids of its items."""
        present: set[int] = set()
        for section in await self.list_sections():
            if section.type != "movie":
                continue
            await self._collect_section_tmdb_ids(section.key, present)
        return frozenset(present)

    async def _collect_section_tmdb_ids(self, key: str, present: set[int]) -> None:
        """Walk one section's items page-by-page, accumulating tmdb ids."""
        start = 0
        while True:
            payload = await self._get(
                f"/library/sections/{key}/all",
                {"includeGuids": "1"},
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(_PAGE_SIZE),
                },
            )
            items = _as_sequence(_media_container(payload).get("Metadata"))
            for item in items:
                _collect_item_tmdb_ids(_as_mapping(item), present)
            if len(items) < _PAGE_SIZE:
                break
            start += _PAGE_SIZE

    async def trigger_scan(self, path: str) -> None:
        """Ask Plex to scan ``path`` (a targeted partial-scan on the owning section).

        The movie section whose location is a parent of ``path`` gets a partial
        refresh of just that path. If NO section covers it (a path-mapping
        difference between the app and Plex, or Plex didn't report locations), we
        do a real FULL refresh of each movie section instead — heavier, but it
        actually indexes the new file, unlike refreshing with a path Plex does not
        own (a silent no-op that would strand the request at "Finalizing"). With no
        movie section at all, raise so the import blocks honestly. A 2xx (possibly
        empty body) is success. After scanning, the presence cache is invalidated so
        the availability check re-pages Plex instead of returning a pre-import snapshot.
        """
        movie_sections = [s for s in await self.list_sections() if s.type == "movie"]
        if not movie_sections:
            raise PlexLibraryError("no Plex movie library section to scan into")
        matched = [s for s in movie_sections if _section_covers(s, path)]
        try:
            if matched:
                # The raw path is handed to httpx as a query param so it is
                # percent-encoded exactly once (pre-quoting here would double-encode).
                for section in matched:
                    await self._request(f"/library/sections/{section.key}/refresh", {"path": path})
            else:
                _logger.warning(
                    "import path is not under any Plex movie section location; "
                    "full-scanning every movie section instead of a no-op partial scan"
                )
                for section in movie_sections:
                    await self._request(f"/library/sections/{section.key}/refresh", {})
        finally:
            # Bust the per-server presence index so completed -> available promotion
            # is not delayed up to the full cache TTL after the file is in Plex.
            _PRESENT_TMDB_CACHE.invalidate(self._base_url)
