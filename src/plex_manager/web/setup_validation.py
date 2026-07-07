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

import httpx
from starlette.status import (
    HTTP_403_FORBIDDEN,
    HTTP_422_UNPROCESSABLE_CONTENT,
    HTTP_502_BAD_GATEWAY,
)

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibrary, PlexLibraryError
from plex_manager.adapters.plex.oauth import find_owned_server
from plex_manager.adapters.qbittorrent.adapter import (
    QbittorrentAuthError,
    QbittorrentClient,
    QbittorrentError,
)
from plex_manager.adapters.tmdb.adapter import TmdbApiError, TmdbAuthError, TmdbMetadata
from plex_manager.services.path_visibility import remap_to_visible
from plex_manager.web.errors import AppError
from plex_manager.web.schemas import PlexLibraryOption, ServiceValidateResponse
from plex_manager.web.url_validation import url_shape_error

if TYPE_CHECKING:
    from collections.abc import Sequence

    from plex_manager.adapters.plex.oauth import PlexResource, PlexTvClient
    from plex_manager.ports.library import LibrarySection

__all__ = [
    "assert_admin_owns_server",
    "assert_plex_token_authorized",
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

    The predicate itself lives in
    :func:`plex_manager.web.url_validation.url_shape_error` — the ONE source of
    truth shared with the write-time ``SettingsUpdate`` / ``SetupCompleteRequest``
    schema validators, so a malformed URL is rejected with the identical message
    and edge cases whether it is caught here (before an outbound probe) or before
    a row is ever written. See that function's docstring for the full rationale
    (control chars, malformed bracketed hosts, port parsing) — this wrapper only
    adapts its ``str | None`` result to this module's ``ServiceValidateResponse``
    shape.
    """
    message = url_shape_error(url)
    if message is None:
        return None
    return ServiceValidateResponse(ok=False, message=message)


def _section_type(kind: Literal["movie", "show"]) -> Literal["movie", "tv"]:
    """Map Plex's own section-type vocabulary (``"show"``) to ours (``"tv"``)."""
    return "tv" if kind == "show" else "movie"


def library_options(
    sections: Sequence[LibrarySection],
    *,
    probe_writable: bool = True,
    suggest_mounts: Sequence[str] = (),
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

    ``suggest_mounts`` (default ``()``, no remap attempted) is the set of KNOWN,
    app-owned LIBRARY mounts (:data:`~plex_manager.services.path_visibility.
    KNOWN_LIBRARY_MOUNTS`) to suffix-match a Plex-reported HOST path against (issue
    #132) -- library locations only ever remap under ``/media``, never
    ``/downloads``. ``probe_original`` mirrors ``probe_writable`` -- the SAME
    pre-auth-oracle guard: pre-init (``probe_writable=False``) never stats the
    raw, caller-supplied path, only candidate suffixes under the app's OWN
    mounts; post-init (``probe_writable=True``, the operator's own creds) may
    stat the raw path first. ``allow_mount_root`` is on: a whole-media-root Plex
    library (the bind SOURCE root, e.g. ``/srv/media`` -> ``/media``) maps to the
    mount root itself, which the suffix-only match could never reach.

    A location the remap can't resolve is offered with NO suggestion -- the raw
    Plex path plus the wizard/Settings visibility hint. Deliberately no guessing
    (PR #147 round 3, maintainer decision): a short-lived "low-confidence
    mount-root" suggestion was removed because a child section like
    ``/srv/plex-data/Movies`` would misroute to the bare mount root; the rare
    arbitrary-bind-root topology is served by manual entry instead.
    """
    options: list[PlexLibraryOption] = []
    for section in sections:
        for path in section.locations:
            suggested = (
                remap_to_visible(
                    path,
                    suggest_mounts,
                    probe_original=probe_writable,
                    allow_mount_root=True,
                )
                if suggest_mounts
                else None
            )
            effective = suggested or path
            options.append(
                PlexLibraryOption(
                    section_key=section.key,
                    title=section.title,
                    path=path,
                    section_type=_section_type(section.type),
                    writable=_is_writable(effective) if probe_writable else None,
                    suggested_path=suggested if (suggested and suggested != path) else None,
                )
            )
    return options


def assert_admin_owns_server(resources: Sequence[PlexResource], machine_identifier: str) -> None:
    """403 ``server_not_owned`` unless ``machine_identifier`` is among the OWNED servers.

    THE ownership assertion for anchoring the app to a Plex server, shared
    verbatim by ``setup/validate/plex``, ``setup/complete``, and the SESSION-admin
    path of ``PUT /settings``' repoint verification so they cannot drift:
    ``resources`` must be the SIGNED-IN admin's own plex.tv resource list, and
    ``machine_identifier`` an id derived live from the candidate server's
    ``/identity`` — never a caller-supplied claim (see each endpoint's docstring
    for why its inputs satisfy this).
    """
    if find_owned_server(resources, machine_identifier) is None:
        raise AppError(
            status_code=HTTP_403_FORBIDDEN,
            code="server_not_owned",
            message="Your Plex account does not own that server.",
            hint="Choose a server your Plex account owns, or sign in with the owner account.",
        )


async def assert_plex_token_authorized(client: httpx.AsyncClient, url: str, token: str) -> None:
    """Raise unless the Plex server at ``url`` ACCEPTS ``token`` on an authed call.

    ``/identity`` is deliberately UNAUTHENTICATED (see :func:`validate_plex`'s
    probe ordering below), so deriving a machine id proves reachability and
    identity but says NOTHING about the credential. This is the shared
    AUTHENTICATED bar for every path that PERSISTS a Plex identity
    (``POST /setup/complete`` and ``PUT /settings``' repoint verification): the
    same real-adapter ``list_sections`` call the validation path uses to catch
    bad tokens, so a reachable server paired with a wrong/revoked token is a
    FAILED verification — never a committed-but-unusable config.

    A rejected token (``PlexAuthError``, HTTP 401/403 from Plex) is a 422
    ``plex_token_invalid`` — the submitted config cannot be processed — reusing
    the SAME stable code the envelope vocabulary already carries for a rejected
    Plex credential. Any other failure (``PlexLibraryError``: transport error,
    unexpected status, non-JSON 200) is the familiar 502
    ``server_unreachable_from_backend`` envelope, matching the identity probe's
    own failure mode. ``use_cache=False`` for the same reason as
    :func:`validate_plex`: a verification must reflect the server as it is NOW,
    never a section list cached from a previous healthy probe.
    """
    try:
        await PlexLibrary(client, url, token).list_sections(use_cache=False)
    except PlexAuthError as exc:
        raise AppError(
            status_code=HTTP_422_UNPROCESSABLE_CONTENT,
            code="plex_token_invalid",
            message="Plex rejected the token.",
            hint="The server answered but refused this credential — check the Plex token.",
        ) from exc
    except PlexLibraryError as exc:
        raise AppError(
            status_code=HTTP_502_BAD_GATEWAY,
            code="server_unreachable_from_backend",
            message="Could not reach the Plex server.",
            hint="Check the Plex URL and that the server is running, then try again.",
        ) from exc


async def validate_plex(
    client: httpx.AsyncClient,
    url: str,
    token: str,
    *,
    identity_client: PlexTvClient | None = None,
    suggest_mounts: Sequence[str] = (),
) -> ServiceValidateResponse:
    """Validate Plex + token AND return the movie/tv library folders to pick from.

    Uses the real adapter (``list_sections``): one call both proves connectivity +
    token and yields the library locations, so the wizard offers writable-folder
    pick-lists for ``movies_root`` / ``tv_root`` instead of a typed, mismatch-prone
    path. The token rides the ``X-Plex-Token`` header, never the URL.

    ``use_cache=False``: this is BOTH the setup wizard's "Test connection" AND
    (via ``health_service._check_plex``) the live health-card probe -- both must
    always reflect reality, never a section list cached from a previous healthy
    probe up to 300s stale.

    ``identity_client`` opts into the ownership-verifying variant the setup wizard
    needs: when supplied, the server's ``/identity`` is probed FIRST (before the
    section list) and its ``machineIdentifier`` is returned on the response so the
    caller can assert the signed-in admin OWNS this server and store the id. The
    probe raises :class:`PlexVerifyError` on a transport failure (rendered as a 502
    ``server_unreachable_from_backend`` envelope) rather than being swallowed into a
    generic ``ok=False`` — an unreachable candidate is an honest, retryable upstream
    state. Left ``None`` (the health-card path) skips the probe entirely, so that
    path issues no extra request and ``machine_identifier`` stays ``None``.

    ``suggest_mounts`` (default ``()``) is forwarded to :func:`library_options` for
    each reported library location — the setup wizard passes the known container
    mounts so a HOST-shaped Plex location comes back with a container-visible
    ``suggested_path``; :func:`~plex_manager.services.health_service._check_plex`
    calls this with no ``suggest_mounts``, so the ~15s health poll adds no extra
    filesystem probes.
    """
    rejection = _require_http_url(url)
    if rejection is not None:
        return rejection
    # Identity FIRST when asked: a transport failure here surfaces as the 502
    # envelope (not the section-probe's ok=False), and Plex's /identity is
    # unauthenticated, so a bad token still falls through to the honest "Plex
    # rejected the token." from list_sections below.
    machine_identifier = (
        None if identity_client is None else await identity_client.fetch_server_identity(url, token)
    )
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
    libraries = library_options(sections, probe_writable=False, suggest_mounts=suggest_mounts)
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
    return ServiceValidateResponse(
        ok=True,
        message="Connected to Plex.",
        libraries=libraries,
        machine_identifier=machine_identifier,
    )


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
