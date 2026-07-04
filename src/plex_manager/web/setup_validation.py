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
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit

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
    "library_options",
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


def _require_http_url(url: str) -> ServiceValidateResponse | None:
    """Reject a malformed / non-http(s) URL before it reaches an outbound request.

    This is honest input hygiene, NOT a claimed SSRF sanitizer: it narrows the
    scheme to ``http``/``https`` and requires a hostname, but the host/port/path
    itself is still fully operator-controlled by design (these are "test
    connection" probes against an operator-supplied, usually-private service —
    see the SSRF risk-acceptance note on alert #247). Its job is only to turn an
    obviously-broken input (``file://...``, a scheme-less string, an empty host)
    into a clear, retryable rejection instead of an opaque ``httpx`` transport
    error. Returns ``None`` when ``url`` is acceptable to try.

    ``urlsplit`` (and reading ``.hostname`` / ``.port``) itself RAISES
    ``ValueError`` on several obviously-broken inputs, all of which are guarded so
    a parse failure surfaces as the same retryable ``ok=False`` rather than
    crashing the validate endpoint with a 500:

    * a malformed bracketed host -- an unterminated IPv6 literal (``http://[::1``)
      or an invalid IPvFuture form (``http://[v7.x]``) -- trips ``.hostname``;
    * a non-numeric (``http://x:bad``) or out-of-range (``http://x:99999``) port
      trips ``.port``. Without touching ``.port`` these slip past the hostname
      check and reach httpx, which raises ``httpx.InvalidURL`` -- and that is NOT
      an ``httpx.HTTPError`` subclass, so it would escape the endpoints' transport
      handlers as a 500 instead of this rejection.

    Raw control characters (C0 + DEL) are rejected up front, BEFORE parsing or any
    log/probe: ``urlsplit`` silently tolerates or strips some of them
    (``http://\\nplex.local`` parses to a plausible host), but httpx then raises
    the same uncaught ``httpx.InvalidURL`` for the non-printable byte. A CR/LF- or
    NUL-bearing URL is exactly "obviously-broken input", so it gets the honest
    ``ok=False`` here rather than a 500 (and never reaches an outbound request).
    """
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return ServiceValidateResponse(ok=False, message="Enter a valid http(s) URL.")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        # Reading ``.port`` validates it -- urllib raises ValueError for a
        # non-numeric or out-of-range port, which we reject rather than let httpx
        # turn into an uncaught InvalidURL (or a doomed connect attempt).
        port = parts.port
    except ValueError:
        return ServiceValidateResponse(ok=False, message="Enter a valid http(s) URL.")
    # Port 0 parses cleanly but is never connectable -- reject it up front too.
    if parts.scheme not in {"http", "https"} or not hostname or port == 0:
        return ServiceValidateResponse(ok=False, message="Enter a valid http(s) URL.")
    return None


def _section_type(kind: Literal["movie", "show"]) -> Literal["movie", "tv"]:
    """Map Plex's own section-type vocabulary (``"show"``) to ours (``"tv"``)."""
    return "tv" if kind == "show" else "movie"


def library_options(
    sections: Sequence[LibrarySection], *, probe_writable: bool = True
) -> list[PlexLibraryOption]:
    """Map Plex's movie AND show sections to pickable library folders + writability.

    The paths come from Plex's own ``/library/sections`` (not a typed request
    value), so choosing one avoids a path-injection sink AND guarantees the
    targeted-scan path match. Every movie- or show-section location is offered
    (tagged by ``section_type``, ``"movie"``/``"tv"``); the UI marks (and
    disables) the non-writable ones, which is the split-mount signal.

    ``probe_writable`` gates the only filesystem touch. The authenticated Settings
    picker leaves it True (the operator's own stored creds make the probe theirs).
    The PRE-INIT ``validate/plex`` wizard step passes False: there the Plex server
    is caller-supplied and unauthenticated, so probing its reported locations would
    turn this into a pre-auth local-FS existence/writability oracle. With it False
    we report ``writable=None`` (UNKNOWN) — honest, never a faked bool — and never
    call ``_is_writable`` / ``os.access`` on an attacker-chosen path.
    """
    return [
        PlexLibraryOption(
            section_key=section.key,
            title=section.title,
            path=path,
            section_type=_section_type(section.type),
            writable=_is_writable(path) if probe_writable else None,
        )
        for section in sections
        for path in section.locations
    ]


async def validate_plex(client: httpx.AsyncClient, url: str, token: str) -> ServiceValidateResponse:
    """Validate Plex + token AND return the movie/tv library folders to pick from.

    Uses the real adapter (``list_sections``): one call both proves connectivity +
    token and yields the library locations, so the wizard offers writable-folder
    pick-lists for ``movies_root`` / ``tv_root`` instead of a typed, mismatch-prone
    path. The token rides the ``X-Plex-Token`` header, never the URL.

    ``use_cache=False``: this is BOTH the setup wizard's "Test connection" AND
    (via ``health_service._check_plex``) the live health-card probe -- both must
    always reflect reality, never a section list cached from a previous healthy
    probe up to 300s stale.
    """
    rejection = _require_http_url(url)
    if rejection is not None:
        return rejection
    try:
        sections = await PlexLibrary(client, url, token).list_sections(use_cache=False)
    except PlexAuthError:
        return ServiceValidateResponse(ok=False, message="Plex rejected the token.")
    except PlexLibraryError as exc:
        return ServiceValidateResponse(
            ok=False, message="Could not reach the Plex server.", detail=str(exc)
        )
    # probe_writable=False: this endpoint is reachable PRE-INIT against a
    # caller-supplied Plex server, so never touch the local filesystem here (no
    # pre-auth existence/writability oracle). Writability is reported UNKNOWN
    # (None); the authenticated Settings picker fills in the real signal later.
    libraries = library_options(sections, probe_writable=False)
    if not libraries:
        # Connectivity + token are fine, but an install with NEITHER a Movie NOR a
        # TV library cannot import anything (every scan would raise "no Plex
        # library section" for that kind). A movie-only OR tv-only Plex is legit --
        # only the fully-empty case stops the wizard here, honest with a next step,
        # never a silent pass into a configured-but-unusable state.
        return ServiceValidateResponse(
            ok=False,
            message="Connected to Plex, but no Movie or TV library exists yet — "
            "add one in Plex, then test again.",
            libraries=[],
        )
    return ServiceValidateResponse(ok=True, message="Connected to Plex.", libraries=libraries)


async def validate_prowlarr(
    client: httpx.AsyncClient, url: str, api_key: str
) -> ServiceValidateResponse:
    """Check Prowlarr + api key via ``GET /api/v1/system/status`` (key in header)."""
    rejection = _require_http_url(url)
    if rejection is not None:
        return rejection
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
        try:
            payload_obj = cast(object, response.json())
        except ValueError:
            return ServiceValidateResponse(
                ok=False,
                message="Unexpected response from Prowlarr.",
                detail="status endpoint did not return JSON",
            )
        if not isinstance(payload_obj, dict):
            return ServiceValidateResponse(
                ok=False,
                message="Unexpected response from Prowlarr.",
                detail="status endpoint did not look like Prowlarr",
            )
        payload = cast(dict[str, object], payload_obj)
        if not isinstance(payload.get("version"), str):
            return ServiceValidateResponse(
                ok=False,
                message="Unexpected response from Prowlarr.",
                detail="status endpoint did not look like Prowlarr",
            )
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
    rejection = _require_http_url(url)
    if rejection is not None:
        return rejection
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
