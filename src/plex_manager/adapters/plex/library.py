"""PlexLibrary — the :class:`LibraryPort` adapter, deferred to v1.

The import/availability pipeline is out of scope for the alpha (see the build
spec's deferred list), but the port is defined now so the reconciler and import
service can be written against it. This adapter exists so the wiring point is
real; every method raises a clear, honest ``NotImplementedError`` naming the
deferred capability rather than silently returning a misleading default
(north-star: surface states, never swallow them).

Construction still takes the eventual config (``base_url`` + ``token``) and an
``httpx.AsyncClient`` so the v1 implementation is a drop-in body fill, not a
signature change. The token is held privately and never logged.
"""

from __future__ import annotations

from typing import Literal

import httpx

from plex_manager.ports.library import LibrarySection

__all__ = ["PlexLibrary"]

_DEFERRED = "deferred to v1"


class PlexLibrary:
    """Stub :class:`LibraryPort` implementation; methods raise until v1.

    The constructor signature is the real one; only the method bodies are
    deferred. ``repr`` redacts the token so it cannot leak into logs.
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str, token: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._token = token

    def __repr__(self) -> str:
        return f"PlexLibrary(base_url={self._base_url!r}, token='***')"

    async def is_available(self, tmdb_id: int, media_type: Literal["movie", "tv"]) -> bool:
        raise NotImplementedError(f"{_DEFERRED}: PlexLibrary.is_available")

    async def trigger_scan(self, path: str) -> None:
        raise NotImplementedError(f"{_DEFERRED}: PlexLibrary.trigger_scan")

    async def list_sections(self) -> list[LibrarySection]:
        raise NotImplementedError(f"{_DEFERRED}: PlexLibrary.list_sections")
