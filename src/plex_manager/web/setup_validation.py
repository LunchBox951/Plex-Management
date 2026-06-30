"""Live connection checks for the setup wizard's "Test connection" buttons.

Each function does a real, lightweight request against the candidate service and
maps the outcome to :class:`ServiceValidateResponse`. Failures are surfaced
honestly (never a silent ``ok=True``), and secrets are never placed into the
returned ``message`` / ``detail`` nor logged.

TMDB and qBittorrent reuse their adapters (so the real auth path is exercised);
Plex and Prowlarr have no read adapter in the alpha, so a raw lightweight GET is
issued with the credential carried in a header (never in a logged URL).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibrary, PlexLibraryError
from plex_manager.adapters.qbittorrent.adapter import (
    QbittorrentAuthError,
    QbittorrentClient,
    QbittorrentError,
)
from plex_manager.adapters.tmdb.adapter import TmdbApiError, TmdbAuthError, TmdbMetadata
from plex_manager.web.schemas import PlexLibraryOption, ServiceValidateResponse

if TYPE_CHECKING:
    from collections.abc import Sequence

    from plex_manager.ports.library import LibrarySection

__all__ = [
    "movie_library_options",
    "validate_plex",
    "validate_prowlarr",
    "validate_qbittorrent",
    "validate_tmdb",
]

_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403


def _is_writable(path: str) -> bool:
    """Whether the app's OWN process can write into ``path`` (a Plex library dir).

    Deliberately read-only (``os.access``): it never writes, so a Plex an attacker
    pointed us at cannot be turned into an arbitrary-write probe. It can be
    optimistic under NFS root-squash / a container-UID mismatch — a false positive
    surfaces later as an honest, retryable ``ImportBlocked``, never a silent fail.
    """
    return os.path.isdir(path) and os.access(path, os.W_OK)


def movie_library_options(sections: Sequence[LibrarySection]) -> list[PlexLibraryOption]:
    """Map Plex's movie sections to pickable ``movies_root`` folders + writability.

    The paths come from Plex's own ``/library/sections`` (not a typed request
    value), so choosing one avoids a path-injection sink AND guarantees the
    targeted-scan path match. Every movie-section location is offered; the UI marks
    (and disables) the non-writable ones, which is the split-mount signal.
    """
    return [
        PlexLibraryOption(
            section_key=section.key,
            title=section.title,
            path=path,
            writable=_is_writable(path),
        )
        for section in sections
        if section.type == "movie"
        for path in section.locations
    ]


async def validate_plex(client: httpx.AsyncClient, url: str, token: str) -> ServiceValidateResponse:
    """Validate Plex + token AND return the movie library folders to pick from.

    Uses the real adapter (``list_sections``): one call both proves connectivity +
    token and yields the library locations, so the wizard offers a writable-folder
    pick-list for ``movies_root`` instead of a typed, mismatch-prone path. The token
    rides the ``X-Plex-Token`` header, never the URL.
    """
    try:
        sections = await PlexLibrary(client, url, token).list_sections()
    except PlexAuthError:
        return ServiceValidateResponse(ok=False, message="Plex rejected the token.")
    except PlexLibraryError as exc:
        return ServiceValidateResponse(
            ok=False, message="Could not reach the Plex server.", detail=str(exc)
        )
    libraries = movie_library_options(sections)
    if not libraries:
        # Connectivity + token are fine, but an install with no Movie library cannot
        # import anything (every scan would raise "no Plex movie library section").
        # Report ok=False so the wizard stops here instead of letting the operator
        # finish into a configured-but-unusable state — north-star: honest, with a
        # next step, never a silent pass.
        return ServiceValidateResponse(
            ok=False,
            message="Connected to Plex, but no Movie library exists yet — "
            "add a Movie library in Plex, then test again.",
            libraries=[],
        )
    return ServiceValidateResponse(ok=True, message="Connected to Plex.", libraries=libraries)


async def validate_prowlarr(
    client: httpx.AsyncClient, url: str, api_key: str
) -> ServiceValidateResponse:
    """Check Prowlarr + api key via ``GET /api/v1/system/status`` (key in header)."""
    try:
        response = await client.get(
            f"{url.rstrip('/')}/api/v1/system/status",
            headers={"X-Api-Key": api_key},
        )
    except httpx.HTTPError as exc:
        # The api key travels in a header, not the URL, so str(exc) cannot leak it.
        return ServiceValidateResponse(
            ok=False, message="Could not reach Prowlarr.", detail=str(exc)
        )
    if response.status_code == _HTTP_OK:
        return ServiceValidateResponse(ok=True, message="Connected to Prowlarr.")
    if response.status_code in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        return ServiceValidateResponse(
            ok=False,
            message="Prowlarr rejected the api key.",
            detail=f"HTTP {response.status_code}",
        )
    return ServiceValidateResponse(
        ok=False,
        message="Unexpected response from Prowlarr.",
        detail=f"HTTP {response.status_code}",
    )


async def validate_qbittorrent(
    client: httpx.AsyncClient, url: str, username: str, password: str
) -> ServiceValidateResponse:
    """Check qBittorrent + credentials by logging in and listing torrents."""
    adapter = QbittorrentClient(client, url, username, password)
    try:
        await adapter.get_all_statuses()
    except QbittorrentAuthError:
        return ServiceValidateResponse(
            ok=False, message="qBittorrent rejected the username or password."
        )
    except QbittorrentError as exc:
        # The adapter wraps httpx transport/status errors into QbittorrentError so
        # they never escape as the app-level 502; the wizard expects the validation
        # shape (ok=False). The QbittorrentError message carries a status code only
        # — never the url, username or password — so str(exc) cannot leak a secret.
        return ServiceValidateResponse(
            ok=False, message="Could not reach qBittorrent.", detail=str(exc)
        )
    except httpx.HTTPError as exc:
        # The password travels in a POST body, not the URL, so str(exc) is safe.
        return ServiceValidateResponse(
            ok=False, message="Could not reach qBittorrent.", detail=str(exc)
        )
    return ServiceValidateResponse(ok=True, message="Connected to qBittorrent.")


async def validate_tmdb(client: httpx.AsyncClient, api_key: str) -> ServiceValidateResponse:
    """Check a TMDB api key with a trivial search through the adapter."""
    adapter = TmdbMetadata(client, api_key)
    try:
        await adapter.search("inception")
    except TmdbAuthError:
        return ServiceValidateResponse(ok=False, message="TMDB rejected the api key.")
    except (TmdbApiError, httpx.HTTPError) as exc:
        # A raw httpx error here could embed the URL (api key is a query param),
        # so only the exception *type* is surfaced — never str(exc).
        return ServiceValidateResponse(
            ok=False, message="Could not reach TMDB.", detail=type(exc).__name__
        )
    return ServiceValidateResponse(ok=True, message="Connected to TMDB.")
