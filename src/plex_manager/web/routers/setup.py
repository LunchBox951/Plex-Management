"""First-run setup wizard endpoints.

Plex sign-in is the sole credential model (there is no app key to mint or
disclose), so every endpoint here except ``status`` is gated by
:func:`require_setup_admin` — a signed-in administrator (a Plex-server owner
pre-init, an owner/api-key post-init), never a one-time bootstrap key.

The wizard, in order:

* ``GET /plex/servers`` enumerates the signed-in admin's OWNED Plex servers, each
  advertised connection probed for reachability from THIS backend (a dead
  connection is annotated, never dropped — the operator picks a reachable one).
* ``POST /validate/{plex,prowlarr,qbittorrent,tmdb}`` are the live "Test
  connection" probes. ``validate/plex`` additionally asserts the probed server's
  ``machineIdentifier`` is one the signed-in admin OWNS (else 403), and returns it
  so ``complete`` can store it.
* ``POST /complete`` is one-shot and keyless: a conditional update claims
  ``initialized`` (a concurrent second caller is rejected 409), the validated creds
  are stored, and ``plex_token`` defaults to the signed-in admin's stored OAuth
  token. The stored ``plex_machine_identifier`` is RE-DERIVED live from the
  submitted server's ``/identity`` and ownership-asserted again (the body's id is
  advisory at most — a direct API caller cannot pair server-X creds with
  server-Y's id). It never touches the sign-in claim's ``setup_started_at``.
  Post-init, config changes go through ``PUT /settings``.

``status`` stays unauthenticated so the SPA can discover whether the install is
initialized and whether the OPTIONAL pre-init hardening token is required.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.status import HTTP_403_FORBIDDEN, HTTP_409_CONFLICT

from plex_manager.adapters.plex.oauth import (
    PlexResource,
    PlexTvClient,
    find_owned_server,
    owned_servers,
)
from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.models import SystemSettings, User
from plex_manager.web.deps import (
    PLEX_MACHINE_ID_SETTING,
    AuthContext,
    SettingsStore,
    ensure_system_settings,
    get_http_client,
    is_setup_token_required,
    load_system_settings,
    require_setup_admin,
)
from plex_manager.web.errors import AppError
from plex_manager.web.schemas import (
    ErrorDetail,
    ErrorEnvelope,
    PlexServerConnection,
    PlexServerOption,
    PlexServersResponse,
    PlexValidateRequest,
    ProwlarrValidateRequest,
    QbittorrentValidateRequest,
    ServiceValidateResponse,
    SetupCompleteRequest,
    SetupStatusResponse,
    TmdbValidateRequest,
)
from plex_manager.web.setup_validation import (
    validate_plex,
    validate_prowlarr,
    validate_qbittorrent,
    validate_tmdb,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])

# Mirrors ``web.routers.auth._CLIENT_ID_SETTING`` — the plex.tv device identifier
# the sign-in flow persists. Read (never re-created) here so the plex.tv resource
# fetches use the SAME device identity the admin signed in with; the fallback only
# matters for a caller that reached setup without going through sign-in.
_CLIENT_ID_SETTING = "plex_oauth_client_identifier"
_FALLBACK_CLIENT_IDENTIFIER = "plex-manager"
# Per-request timeout for a connection-reachability probe. Passed per ``get`` call,
# so the shared upstream client is never mutated.
_PROBE_TIMEOUT_SECONDS = 5.0
_SERVER_UNREACHABLE_CODE = "server_unreachable_from_backend"

_AUTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorEnvelope, "description": "Sign in to continue setup"},
    403: {"model": ErrorEnvelope, "description": "Administrator required"},
}
_PLEX_ACCOUNT_RESPONSE: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorEnvelope, "description": "A Plex-signed-in admin is required"},
}
_SERVERS_RESPONSES: dict[int | str, dict[str, Any]] = {**_AUTH_RESPONSES, **_PLEX_ACCOUNT_RESPONSE}
_PLEX_VALIDATE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_AUTH_RESPONSES,
    **_PLEX_ACCOUNT_RESPONSE,
    502: {"model": ErrorEnvelope, "description": "The Plex server was unreachable"},
}
_COMPLETE_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_AUTH_RESPONSES,
    409: {"model": ErrorDetail, "description": "Setup already initialized"},
    502: {"model": ErrorEnvelope, "description": "The Plex server was unreachable"},
}


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
async def _admin_plex_token(session: AsyncSession, context: AuthContext) -> str:
    """Return the signed-in admin's stored Plex OAuth token, or 409.

    Only a Plex-session admin has a ``User`` row carrying a token; an api-key /
    dev-bypass admin has ``user_id is None`` and no Plex account, so it cannot
    enumerate servers or assert ownership — an honest 409, never a fabricated
    empty result.
    """
    if context.user_id is not None:
        user = await session.get(User, context.user_id)
        if user is not None and user.encrypted_plex_token:
            return user.encrypted_plex_token
    raise AppError(
        status_code=HTTP_409_CONFLICT,
        code="plex_account_required",
        message="Server discovery needs a Plex-signed-in admin.",
        hint="Sign in with Plex first.",
    )


async def _plex_tv_client(session: AsyncSession, client: httpx.AsyncClient) -> PlexTvClient:
    identifier = await SettingsStore(session).get(_CLIENT_ID_SETTING) or _FALLBACK_CLIENT_IDENTIFIER
    return PlexTvClient(client, client_identifier=identifier)


def _assert_admin_owns_server(resources: Sequence[PlexResource], machine_identifier: str) -> None:
    """403 ``server_not_owned`` unless ``machine_identifier`` is among the OWNED servers.

    THE ownership assertion of the wizard, shared verbatim by ``validate/plex``
    and ``complete`` so the two cannot drift: ``resources`` must be the SIGNED-IN
    admin's own plex.tv resource list, and ``machine_identifier`` an id derived
    live from the candidate server's ``/identity`` — never a caller-supplied
    claim (see each endpoint's docstring for why its inputs satisfy this).
    """
    if find_owned_server(resources, machine_identifier) is None:
        raise AppError(
            status_code=HTTP_403_FORBIDDEN,
            code="server_not_owned",
            message="Your Plex account does not own that server.",
            hint="Choose a server your Plex account owns, or sign in with the owner account.",
        )


async def _probe_connection(
    client: httpx.AsyncClient, uri: str
) -> tuple[Literal["ok", "unreachable"], str | None]:
    """Probe ``{uri}/identity`` for reachability from THIS backend (never raises).

    A transport failure (or timeout) is the reachability verdict, not an error that
    fails the enclosing listing: the operator sees which connection to use. A
    malformed uri plex.tv advertised raises ``httpx.InvalidURL`` (NOT an
    ``httpx.HTTPError``) while building the request — caught here too, so one bad
    connection reads as unreachable instead of failing the whole listing.
    """
    try:
        await client.get(f"{uri.rstrip('/')}/identity", timeout=_PROBE_TIMEOUT_SECONDS)
    except (httpx.HTTPError, httpx.InvalidURL):
        return "unreachable", _SERVER_UNREACHABLE_CODE
    return "ok", None


async def _probe_owned_servers(
    client: httpx.AsyncClient, owned: Sequence[PlexResource]
) -> list[PlexServerOption]:
    """Map owned servers to options, probing every connection concurrently.

    All connections across all servers are probed in one ``asyncio.gather`` so the
    listing pays one round-trip latency, not one per connection; a per-connection
    failure only annotates THAT connection.
    """
    flat = [(index, conn) for index, server in enumerate(owned) for conn in server.connections]
    verdicts = await asyncio.gather(*(_probe_connection(client, conn.uri) for _, conn in flat))
    grouped: dict[int, list[PlexServerConnection]] = {index: [] for index in range(len(owned))}
    for (index, conn), (probe_status, error_code) in zip(flat, verdicts, strict=True):
        grouped[index].append(
            PlexServerConnection(
                uri=conn.uri,
                local=conn.local,
                relay=conn.relay,
                status=probe_status,
                error_code=error_code,
            )
        )
    return [
        PlexServerOption(
            name=server.name or "",
            machine_identifier=server.client_identifier or "",
            connections=grouped[index],
        )
        for index, server in enumerate(owned)
    ]


# --------------------------------------------------------------------------- #
# Server discovery
# --------------------------------------------------------------------------- #
@router.get("/plex/servers", responses=_SERVERS_RESPONSES)
async def plex_servers_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    context: Annotated[AuthContext, Depends(require_setup_admin)],
) -> PlexServersResponse:
    """List the signed-in admin's OWNED Plex servers with each connection probed."""
    admin_token = await _admin_plex_token(session, context)
    plex_tv = await _plex_tv_client(session, client)
    resources = await plex_tv.fetch_resources(admin_token)
    servers = await _probe_owned_servers(client, owned_servers(resources))
    return PlexServersResponse(servers=servers)


# --------------------------------------------------------------------------- #
# Connection validation ("Test connection")
# --------------------------------------------------------------------------- #
@router.post("/validate/plex", responses=_PLEX_VALIDATE_RESPONSES)
async def validate_plex_endpoint(
    body: PlexValidateRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    context: Annotated[AuthContext, Depends(require_setup_admin)],
) -> ServiceValidateResponse:
    """Test a candidate Plex server AND assert the signed-in admin owns it.

    The server is probed with the body's token override, or (the wizard's happy
    path) the admin's stored OAuth token. Ownership is asserted against the SIGNED-IN
    admin's plex.tv resources (always their own account), so a custom token can
    never configure a server they do not own.
    """
    admin_token = await _admin_plex_token(session, context)
    plex_tv = await _plex_tv_client(session, client)
    result = await validate_plex(
        client, body.url, body.token or admin_token, identity_client=plex_tv
    )
    if not result.ok or result.machine_identifier is None:
        return result
    resources = await plex_tv.fetch_resources(admin_token)
    _assert_admin_owns_server(resources, result.machine_identifier)
    return result


@router.post("/validate/prowlarr", responses=_AUTH_RESPONSES)
async def validate_prowlarr_endpoint(
    body: ProwlarrValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    _context: Annotated[AuthContext, Depends(require_setup_admin)],
) -> ServiceValidateResponse:
    """Test candidate Prowlarr credentials."""
    return await validate_prowlarr(client, body.url, body.api_key)


@router.post("/validate/qbittorrent", responses=_AUTH_RESPONSES)
async def validate_qbittorrent_endpoint(
    body: QbittorrentValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    _context: Annotated[AuthContext, Depends(require_setup_admin)],
) -> ServiceValidateResponse:
    """Test candidate qBittorrent credentials."""
    return await validate_qbittorrent(client, body.url, body.username, body.password)


@router.post("/validate/tmdb", responses=_AUTH_RESPONSES)
async def validate_tmdb_endpoint(
    body: TmdbValidateRequest,
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    _context: Annotated[AuthContext, Depends(require_setup_admin)],
) -> ServiceValidateResponse:
    """Test a candidate TMDB api key."""
    return await validate_tmdb(client, body.api_key)


# --------------------------------------------------------------------------- #
# Completion + status
# --------------------------------------------------------------------------- #
@router.post("/complete", responses=_COMPLETE_RESPONSES)
async def complete(
    body: SetupCompleteRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    context: Annotated[AuthContext, Depends(require_setup_admin)],
) -> SetupStatusResponse:
    """Persist the validated creds + chosen server and mark the install initialized.

    Keyless: Plex sign-in is the only credential model, so nothing is minted or
    disclosed here. One-shot AND concurrency-safe via a CONDITIONAL update
    (``... WHERE id = 1 AND initialized = false``): exactly one caller flips the row;
    a concurrent second sees ``rowcount == 0`` and is rejected 409, so it can neither
    overwrite the stored creds nor re-claim the install. The claim sets ONLY
    ``initialized``/``setup_completed_at`` — the pre-init sign-in already stamped
    ``setup_started_at`` (deliberately never overwritten here).

    The persisted machine identifier is RE-DERIVED here, never trusted from the
    body. ``validate/plex`` asserts ownership of the server it probes, but that
    proves nothing about what a direct API caller later POSTs to THIS endpoint:
    pairing server-X's ``plex_url``/``plex_token`` with server-Y's machine id
    would make post-init sign-in admit (and grant admin to) server-Y's audience
    while the app actually operates server-X. So, in the same request that
    persists it, the id is probed live from the SUBMITTED ``plex_url`` +
    resolved token via the exact ``/identity`` code path ``validate/plex`` uses
    (:meth:`PlexTvClient.fetch_server_identity`; an unreachable server is the
    same honest 502 envelope), and the SAME ownership assertion
    (:func:`_assert_admin_owns_server`, 403 ``server_not_owned``) is re-checked
    against the signed-in admin's own plex.tv resources. Only the re-derived id
    is stored; ``body.plex_machine_identifier`` is advisory at most (the wizard
    sends the matching one — a mismatch means the caller bypassed the wizard,
    and the derived truth simply wins). Like the token resolution, all of this
    runs BEFORE the claim, so a failed probe / foreign server can never leave a
    half-claimed, credential-less row. Under ``dev_auth_bypass`` (dev only, no
    Plex account exists to assert ownership with — the whole credential model is
    already bypassed) the body id is stored as-is, exactly as the bypass skips
    ``require_setup_admin`` itself.
    """
    # Resolve the Plex token BEFORE the claim so the None-token path's own 409 (no
    # signed-in Plex account) can never leave a half-claimed, credential-less row.
    plex_token = body.plex_token
    machine_identifier = body.plex_machine_identifier
    if get_settings().dev_auth_bypass:
        if plex_token is None:
            plex_token = await _admin_plex_token(session, context)
    else:
        # The ownership assertion always needs the SIGNED-IN admin's own token
        # (their resource list is the ownership source of truth), even when the
        # body carries an explicit service-token override.
        admin_token = await _admin_plex_token(session, context)
        if plex_token is None:
            plex_token = admin_token
        plex_tv = await _plex_tv_client(session, client)
        machine_identifier = await plex_tv.fetch_server_identity(body.plex_url, plex_token)
        resources = await plex_tv.fetch_resources(admin_token)
        _assert_admin_owns_server(resources, machine_identifier)

    # Ensure the singleton row (id=1) exists so the conditional update has a target.
    await ensure_system_settings(session)
    now = datetime.now(UTC)
    # Atomically claim initialization; a concurrent second caller updates 0 rows and
    # is rejected below. ``setup_started_at`` is intentionally absent from ``values``.
    # Unquoted cast: ``CursorResult``/``Any`` must be runtime references, or static
    # scanners that skip string annotations flag them as unused imports.
    claim = cast(
        CursorResult[Any],
        await session.execute(
            update(SystemSettings)
            .where(SystemSettings.id == 1, SystemSettings.initialized.is_(False))
            .values(initialized=True, setup_completed_at=now)
        ),
    )
    if claim.rowcount == 0:
        await session.rollback()
        raise HTTPException(status_code=HTTP_409_CONFLICT, detail="already_initialized")

    store = SettingsStore(session)
    # Exactly the fields the wizard collects (plus the chosen server id), NOT
    # ``KNOWN_SETTING_KEYS`` — that tuple also carries operability-beta knobs that
    # are not part of the wizard and must stay unset so their typed getters fall
    # back to their safe defaults.
    values: dict[str, str] = {
        "plex_url": body.plex_url,
        "plex_token": plex_token,
        # The id derived live from the submitted server above — NEVER the body's
        # claim (see the docstring; dev_auth_bypass is the only body-id path).
        PLEX_MACHINE_ID_SETTING: machine_identifier,
        "prowlarr_url": body.prowlarr_url,
        "prowlarr_api_key": body.prowlarr_api_key,
        "qbittorrent_url": body.qbittorrent_url,
        "qbittorrent_username": body.qbittorrent_username,
        "qbittorrent_password": body.qbittorrent_password,
        "tmdb_api_key": body.tmdb_api_key,
    }
    for key, value in values.items():
        await store.set(key, value)
    # Library roots are independently optional (movie-only, tv-only, mixed, or
    # anime-routed installs are all valid): write a root only when supplied, so an
    # unset root reads back as None from GET /settings rather than an empty string.
    if body.movies_root:
        await store.set("movies_root", body.movies_root)
    if body.tv_root:
        await store.set("tv_root", body.tv_root)
    if body.anime_movie_root:
        await store.set("anime_movie_root", body.anime_movie_root)
    if body.anime_tv_root:
        await store.set("anime_tv_root", body.anime_tv_root)

    await session.commit()
    return SetupStatusResponse(initialized=True)


@router.get("/status")
async def status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SetupStatusResponse:
    """Report install state + whether the optional pre-init hardening token applies.

    Unauthenticated so the SPA can decide whether to show the setup wizard and a
    setup-token field. No app key is ever served (Plex sign-in is the credential).
    """
    system = await load_system_settings(session)
    initialized = system is not None and system.initialized
    return SetupStatusResponse(
        initialized=initialized,
        setup_token_required=not initialized and is_setup_token_required(request),
    )
