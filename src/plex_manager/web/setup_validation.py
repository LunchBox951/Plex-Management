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
from pathlib import Path

import httpx

from plex_manager.adapters.qbittorrent.adapter import (
    QbittorrentAuthError,
    QbittorrentClient,
    QbittorrentError,
)
from plex_manager.adapters.tmdb.adapter import TmdbApiError, TmdbAuthError, TmdbMetadata
from plex_manager.web.schemas import ServiceValidateResponse

__all__ = [
    "validate_movies_root",
    "validate_plex",
    "validate_prowlarr",
    "validate_qbittorrent",
    "validate_tmdb",
]

_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403


async def validate_plex(client: httpx.AsyncClient, url: str, token: str) -> ServiceValidateResponse:
    """Check a Plex server + token via ``GET /identity`` (token in header)."""
    try:
        response = await client.get(
            f"{url.rstrip('/')}/identity",
            headers={"X-Plex-Token": token, "Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        # The token travels in a header, not the URL, so str(exc) cannot leak it.
        return ServiceValidateResponse(
            ok=False, message="Could not reach the Plex server.", detail=str(exc)
        )
    if response.status_code == _HTTP_OK:
        return ServiceValidateResponse(ok=True, message="Connected to Plex.")
    if response.status_code in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        return ServiceValidateResponse(
            ok=False,
            message="Plex rejected the token.",
            detail=f"HTTP {response.status_code}",
        )
    return ServiceValidateResponse(
        ok=False,
        message="Unexpected response from Plex.",
        detail=f"HTTP {response.status_code}",
    )


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


def validate_movies_root(path: str) -> ServiceValidateResponse:
    """Check the Movies library folder exists and is writable (a local path).

    Runs server-side, so it validates the path as the app's process (and, in a
    container, the mounted volume) sees it — the same path the importer will route
    movies into. A path is not a secret, so it may appear in the message.
    """
    candidate = path.strip()
    if not candidate:
        return ServiceValidateResponse(ok=False, message="Enter a library folder path.")
    # Require an absolute, traversal-free path: the importer writes movies to an
    # absolute root, and a relative / ``..``-laden value is both broken for import
    # and a needless way to point the probe outside the intended tree.
    if not os.path.isabs(candidate) or ".." in Path(candidate).parts:
        return ServiceValidateResponse(
            ok=False,
            message="Use an absolute path with no '..' segments (e.g. /library/movies).",
            detail=path,
        )
    target = Path(candidate)
    if not target.exists():
        return ServiceValidateResponse(ok=False, message="That folder does not exist.", detail=path)
    if not target.is_dir():
        return ServiceValidateResponse(ok=False, message="That path is not a folder.", detail=path)
    if not os.access(target, os.W_OK):
        return ServiceValidateResponse(
            ok=False, message="That folder is not writable.", detail=path
        )
    return ServiceValidateResponse(ok=True, message="Library folder is ready.")


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
