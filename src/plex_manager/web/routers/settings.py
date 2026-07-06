"""Settings endpoints — authenticated by Plex session or ``X-Api-Key``.

``GET`` returns a redacted view (secrets masked to ``"***"``). ``PUT`` upserts
the provided config, encrypting secret values at rest. Only fields present in the
request body are written; absent fields are left unchanged.

The ``/app-key`` endpoints manage the app's own ``X-Api-Key``
(``SystemSettings.app_api_key``) -- an OPT-IN recovery/automation credential.
Setup mints nothing, so the key starts absent: ``GET /app-key/status`` reports
whether one exists (without revealing it), ``POST /app-key/rotate`` GENERATES the
first key (when none exists) and ROTATES thereafter, ``GET /app-key`` reveals the
current key for a new device, and ``DELETE /app-key`` revokes it (issue #28's
OAuth-deferral analysis).
"""

from __future__ import annotations

import asyncio
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.adapters.plex.oauth import PlexTvClient
from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.models import AuthSession
from plex_manager.ports.library import LibraryPort
from plex_manager.web.deps import (
    API_KEY_HEADER_NAME,
    PLEX_MACHINE_ID_SETTING,
    SECRET_MASK,
    SECRET_SETTING_KEYS,
    AuthContext,
    AuthMethod,
    SettingsStore,
    api_key_matches,
    ensure_system_settings,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_http_client,
    get_library,
    load_system_settings,
    require_admin,
)
from plex_manager.web.errors import AppError
from plex_manager.web.schemas import (
    AppApiKeyResponse,
    AppApiKeyStatusResponse,
    ErrorEnvelope,
    PlexLibraryOption,
    SettingsResponse,
    SettingsUpdate,
)
from plex_manager.web.setup_validation import library_options

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/settings",
    tags=["settings"],
    dependencies=[Depends(require_admin)],
)

# 32 bytes -> secrets.token_urlsafe(32) yields a 43-char URL-safe token. Setup
# mints no key any more; BOTH the first generate (from no key) and every later
# rotate mint with this length, so a key is indistinguishable in shape/strength
# regardless of when it was issued.
_API_KEY_BYTES = 32

# Mirrors ``web.routers.auth._CLIENT_ID_SETTING`` / ``web.routers.setup`` — the
# plex.tv device identifier the sign-in flow persists. Read (never re-created)
# here so the repoint verification probe uses the SAME device identity as every
# other plex.tv/Plex-server call; the fallback only matters on a DB that never
# saw a sign-in.
_CLIENT_ID_SETTING = "plex_oauth_client_identifier"
_FALLBACK_CLIENT_IDENTIFIER = "plex-manager"

_PUT_SETTINGS_RESPONSES: dict[int | str, dict[str, Any]] = {
    502: {
        "model": ErrorEnvelope,
        "description": "The replacement Plex server did not answer the /identity probe",
    },
}

# Serialises app-key rotation so the compare-and-swap in ``rotate_app_key_endpoint``
# is a genuine atomic read-modify-write, not check-then-act. Two rotations racing
# with the SAME old key would otherwise both re-read the old value and both pass
# the compare BEFORE either commits, so the second write silently clobbers the
# first's freshly minted key (leaving the first client showing a dead key). Under
# this lock the loser's re-read happens only AFTER the winner has committed, so it
# observes the new key and honestly 409s.
#
# Correctness relies on this being a SINGLE-PROCESS deployment: uvicorn runs one
# worker and the in-app reconcile / eviction / log-drain lifespan loops (web/app.py)
# already assume the same single process, so an in-process ``asyncio.Lock`` — not a
# DB row lock or advisory lock — is the right, matching tool. A multi-worker
# deployment would need a DB-level guard instead (a conditional UPDATE is not an
# option here: the key column is EncryptedStr/Fernet, whose ciphertext is
# non-deterministic, so a ``WHERE app_api_key = <ciphertext>`` predicate can never
# match).
_rotate_lock = asyncio.Lock()


async def _redacted(store: SettingsStore) -> SettingsResponse:
    return SettingsResponse.model_validate(await store.redacted())


def _to_stored_string(value: object) -> str:
    """Render an incoming ``SettingsUpdate`` field value as the plain-text string
    :meth:`SettingsStore.set` persists (``settings.value`` has no typed columns).

    Booleans render lowercase (``"true"``/``"false"``) to match this codebase's
    own convention for the setting (see ``web.deps._TRUE_STRINGS`` and the
    eviction tests that seed ``store.set("eviction_enabled", "true")`` directly)
    rather than Python's capitalized ``str(True)`` -- both round-trip correctly
    through ``web.deps``'s case-insensitive parse, but the lowercase form is
    the one actually written elsewhere, so a raw DB read stays consistent
    regardless of which path wrote the value.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


@router.get("")
async def get_settings_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SettingsResponse:
    """Return the redacted service config (secrets shown as ``"***"``)."""
    return await _redacted(SettingsStore(session))


@router.get("/plex-libraries")
async def plex_libraries_endpoint(
    library: Annotated[LibraryPort, Depends(get_library)],
) -> list[PlexLibraryOption]:
    """Library folders (movie AND tv) Plex reports, for the Settings
    ``movies_root`` / ``tv_root`` pickers -- each option is tagged by
    ``section_type`` so the frontend can filter to the picker it's rendering.

    Uses the stored Plex creds (no re-typing the token); 409 if Plex is unconfigured.
    """
    # probe_writable=True (the default): authenticated, and the Plex creds are the
    # operator's own stored config — so the real writability signal is legitimate
    # here (unlike the pre-init validate/plex step, which must NOT probe).
    #
    # use_cache=False: this is an infrequent, human-driven read (once per
    # Settings page load, no polling) so it must reflect Plex as it is RIGHT
    # NOW, not a snapshot cached for up to 300s -- otherwise a movie library the
    # operator just added in Plex stays invisible in the movies_root picker for
    # up to 5 minutes (issue #15). Same use_cache=False treatment already given
    # to validate_plex (setup wizard + health dashboard) for the identical
    # reason. The warmed fast paths (is_available/scan/watch_state) are
    # untouched and stay on the cached default.
    return library_options(await library.list_sections(use_cache=False), probe_writable=True)


@router.get("/app-key")
async def reveal_app_key_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AppApiKeyResponse:
    """Return the current app ``X-Api-Key`` in plaintext.

    Authenticated: the caller already proved they have a valid Plex session or
    app key, so this is not an anonymous disclosure -- it is the break-glass
    recovery path for a NEW device/browser that needs to be paired without
    re-running setup.

    Setup mints no key, so a fresh install has none to reveal: that is an honest
    ``app_key_not_set`` envelope (404) whose hint points the operator at the
    Generate control, never a bare/opaque failure (north star #3).
    """
    system = await load_system_settings(session)
    if system is None or system.app_api_key is None:
        raise AppError(
            status_code=404,
            code="app_key_not_set",
            message="No recovery key exists.",
            hint="Generate one below.",
        )
    return AppApiKeyResponse(app_api_key=system.app_api_key)


@router.get("/app-key/status")
async def app_key_status_endpoint(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AppApiKeyStatusResponse:
    """Whether a recovery key currently exists, WITHOUT revealing it.

    Lets Settings → Access render Generate (no key yet) vs Rotate/Revoke (a key is
    present) without invoking the break-glass reveal. Admin-gated like every route
    on this router; the plaintext key is never part of this response.
    """
    system = await load_system_settings(session)
    exists = system is not None and system.app_api_key is not None
    return AppApiKeyStatusResponse(exists=exists)


@router.post("/app-key/rotate")
async def rotate_app_key_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> AppApiKeyResponse:
    """Mint an app ``X-Api-Key`` -- GENERATE the first one, or ROTATE an existing key.

    This is the sole mint path now that setup is keyless: when no key exists
    (``app_api_key IS NULL``) it GENERATES the first key (the CAS below has nothing
    to compare against, so it simply mints); when a key exists it ROTATES,
    invalidating the old one. Both run under ``_rotate_lock`` and return the
    plaintext exactly once.

    Every OTHER device/browser with the OLD key saved (localStorage) is
    immediately locked out -- there is exactly one live key at a time, matching
    ``require_api_key``'s single-key comparison. The frontend persists the
    returned key immediately for future access-key recovery, but normal browser
    auth continues to use the Plex session cookie.

    Compare-and-swap against concurrent rotations: two rotate requests can both
    pass authentication against the OLD stored key (each request loads it before
    either commits) regardless of HOW they authenticated — two api-key callers,
    two Plex-SESSION admins, or a mix. Without a guard the write that commits
    second would silently overwrite the first's freshly minted key, so the client
    that fired the first request would be left displaying an already-dead key.
    The re-read/compare/mint/commit is run under the module-level ``_rotate_lock``
    so it is a true atomic read-modify-write rather than check-then-act: the
    compare and the write cannot interleave with another rotation, so the loser's
    re-read runs only AFTER the winner has committed. Inside THIS request's own
    transaction we re-read the stored key and require it to still equal the key
    this request OBSERVED — the presented ``X-Api-Key`` header for api-key auth,
    else (session auth, which carries no key header) the stored value as loaded
    at auth time. If it has already changed, the race happened and we answer 409
    (``app_key_changed``) rather than clobber the winner. The CAS is deliberately
    UNCONDITIONAL on auth method: gating it to api-key callers would let two
    session-authenticated admins re-create the exact dead-key race it exists to
    prevent. The check is skipped only under ``dev_auth_bypass`` (there is no
    authenticated key to compare against), exactly like ``require_api_key``
    itself.

    The CAS also closes the revoke null-hole: a stored key that has become NULL
    is the genuine first-key GENERATE only when this request ALSO observed null.
    If this request observed a NON-null key that a concurrent REVOKE cleared
    mid-flight, minting again would silently resurrect the revoked key, so it
    409s too — a null stored value is not a blanket "nothing to compare, just
    mint".
    """
    system = await ensure_system_settings(session)
    async with _rotate_lock:
        if not get_settings().dev_auth_bypass:
            # The key this request observed BEFORE the fresh read below: api-key
            # callers presented it in the header; session callers observed the value
            # their request session loaded at auth time (``authenticate_request``
            # pulled this row into the identity map, and ``ensure_system_settings``
            # returned that same cached instance — a concurrent commit does not
            # update it).
            observed = (
                request.headers.get(API_KEY_HEADER_NAME)
                if auth.method is AuthMethod.api_key
                else system.app_api_key
            )
            # Force a fresh read (in the same transaction as the write below, and
            # under _rotate_lock so no other rotation can commit between this read
            # and our own commit) so the CAS reflects any rotation that committed
            # while this request was in flight.
            await session.refresh(system)
            if system.app_api_key is None:
                # No key is stored right now. Two very different reasons land here:
                #  * Genuine first-key GENERATE: this request ALSO observed no key
                #    (``observed`` is None) -- nothing ever existed to change, so
                #    simply MINT (fall through).
                #  * Revoked-key null-hole: this request OBSERVED a non-null key
                #    that a concurrent REVOKE cleared while we were in flight.
                #    Minting now would silently resurrect a just-revoked key, so
                #    honour the same 409 the rotate-vs-rotate CAS gives rather than
                #    defeat the revoke. (The old ``is not None`` guard skipped the
                #    CAS entirely here and re-minted -- the reported null-hole.)
                if observed is not None:
                    raise HTTPException(status_code=409, detail="app_key_changed")
            elif not api_key_matches(observed, system.app_api_key):
                # A key IS stored but it is no longer the one this request observed:
                # a concurrent generate/rotate already minted it, so 409 rather than
                # clobber the winner.
                raise HTTPException(status_code=409, detail="app_key_changed")
        new_key = secrets.token_urlsafe(_API_KEY_BYTES)
        system.app_api_key = new_key
        await session.commit()
    return AppApiKeyResponse(app_api_key=new_key)


@router.delete("/app-key", status_code=204)
async def revoke_app_key_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> None:
    """Revoke the app ``X-Api-Key``: clear the stored key so none authenticates.

    Every device holding the old key is locked out at once (``X-Api-Key`` auth
    401s until a new key is generated); browser Plex-session auth is unaffected.
    Idempotent -- revoking a keyless install is a no-op 204, since the end state is
    the same either way.

    Compare-and-swap, mirroring :func:`rotate_app_key_endpoint`: an EARLIER draft
    loaded ``system`` then wrote ``None`` unconditionally, which lost the update
    when a rotate committed a fresh key in between — a stale revoke (authenticated
    against a now-superseded key) would wipe a key the operator had just rotated
    to. Under ``_rotate_lock`` we re-read the stored key and, if it is non-null
    and no longer the value THIS request observed (the presented ``X-Api-Key``
    header for api-key auth, else the session-loaded value at auth time), 409
    ``app_key_changed`` rather than clobber that rotation. A currently-null stored
    key stays the idempotent 204 no-op (nothing to lose). The check is skipped
    only under ``dev_auth_bypass`` (no authenticated key to compare against),
    exactly like the rotate CAS and ``require_api_key`` itself.
    """
    system = await ensure_system_settings(session)
    async with _rotate_lock:
        if not get_settings().dev_auth_bypass:
            # The key this request observed BEFORE the fresh read below — same
            # sourcing as the rotate CAS: api-key callers presented it in the
            # header; session callers observed the value their request session
            # loaded at auth time (a concurrent commit does not update that cached
            # instance).
            observed = (
                request.headers.get(API_KEY_HEADER_NAME)
                if auth.method is AuthMethod.api_key
                else system.app_api_key
            )
            # Force a fresh read in this transaction, under _rotate_lock, so the CAS
            # reflects any rotation that committed while this revoke was in flight.
            await session.refresh(system)
            if system.app_api_key is not None and not api_key_matches(observed, system.app_api_key):
                raise HTTPException(status_code=409, detail="app_key_changed")
        system.app_api_key = None
        await session.commit()


async def _verify_plex_repoint(
    body: SettingsUpdate, store: SettingsStore, client: httpx.AsyncClient
) -> tuple[bool, str | None]:
    """Detect a Plex repoint and, when verifiable, derive the NEW server's identity.

    Returns ``(identity_changed, derived_machine_id)``:

    * ``(False, None)`` — this PUT does not change the effective ``plex_url`` /
      ``plex_token`` pair (absent/``null`` fields, the masked-secret ``"***"``
      round-trip, and a same-value re-PUT are all NON-changes). No probe is ever
      issued — an ordinary settings save must not pay a live Plex round-trip.
    * ``(True, machine_id)`` — the identity changes and the EFFECTIVE (post-PUT)
      url+token pair is complete: the REPLACEMENT server's ``/identity`` was
      probed live and answered. The effective value of each half is this PUT's
      submitted value when it carries one, else the currently-stored value — so
      a masked/omitted token still probes with the STORED real token when only
      ``plex_url`` changed.
    * ``(True, None)`` — the identity changes but the effective pair is
      INCOMPLETE (a half-configured install, or an explicit clear-to-``""``).
      There is nothing to probe; the caller keeps the settings write but treats
      the repoint as UNVERIFIED (stale-id drop only, no session revocation —
      see :func:`put_settings_endpoint`).

    A probe failure raises the adapter's ``PlexVerifyError`` — rendered as the
    SAME honest 502 envelope (``server_unreachable_from_backend`` /
    ``server_identity_failed``) ``/setup/complete`` and ``/setup/validate/plex``
    use — BEFORE anything is written, so a typo'd-but-parseable url can never
    commit a broken identity (let alone revoke the sessions that could fix it).
    """
    submitted_url = (
        body.plex_url if "plex_url" in body.model_fields_set and body.plex_url is not None else None
    )
    submitted_token = (
        body.plex_token
        if "plex_token" in body.model_fields_set
        and body.plex_token is not None
        and body.plex_token != SECRET_MASK
        else None
    )
    if submitted_url is None and submitted_token is None:
        return False, None
    stored_url = await store.get("plex_url")
    stored_token = await store.get("plex_token")
    identity_changed = (submitted_url is not None and submitted_url != stored_url) or (
        submitted_token is not None and submitted_token != stored_token
    )
    if not identity_changed:
        return False, None
    # EFFECTIVE post-PUT halves: the submitted value when this PUT carries one
    # (including an explicit ``""`` clear), else what is currently stored.
    effective_url = submitted_url if submitted_url is not None else stored_url
    effective_token = submitted_token if submitted_token is not None else stored_token
    if not effective_url or not effective_token:
        return True, None
    plex_tv = PlexTvClient(
        client,
        client_identifier=await store.get(_CLIENT_ID_SETTING) or _FALLBACK_CLIENT_IDENTIFIER,
    )
    return True, await plex_tv.fetch_server_identity(effective_url, effective_token)


@router.put("", responses=_PUT_SETTINGS_RESPONSES)
async def put_settings_endpoint(
    body: SettingsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
) -> SettingsResponse:
    """Upsert the provided config and return the redacted result.

    A secret field whose incoming value is the redaction mask (``"***"``) is a
    no-op: GET returns ``"***"`` for a configured secret, so a FE that round-trips
    the whole object back (e.g. after editing only ``plex_url``) must not clobber
    the real credential with the mask — a silent secret-wipe that would only
    surface later as an auth failure to the downstream service.

    The disk-pressure pair is cross-checked against the EFFECTIVE (post-update)
    values, not just what this one request happens to carry: ``SettingsUpdate``'s
    own ``model_validator`` only catches a target above the threshold when BOTH
    are submitted together, but ``PUT`` is a PARTIAL update — sending just one
    side against an already-stored (and now-inverted) other side would otherwise
    silently leave the whole threshold-to-target band unable to relieve pressure
    (see :func:`~plex_manager.web.routers.settings._validate_disk_pressure_pair`).
    Checked, and rejected with the SAME 422 shape, BEFORE anything is written.

    Repointing Plex is VERIFIED before it is committed. The
    ``plex_machine_identifier`` snapshot (:data:`PLEX_MACHINE_ID_SETTING`) is the
    id post-init sign-in trusts to admit users, so when this PUT actually CHANGES
    the effective ``plex_url``/``plex_token`` the REPLACEMENT server's
    ``/identity`` is probed live FIRST (:func:`_verify_plex_repoint` — the same
    code path ``/setup/complete`` and ``/setup/validate/plex`` use, resolving a
    masked/omitted token to the stored real one). Only a server that answers
    with a machine id gets committed: the settings are written, the freshly
    DERIVED id replaces the cached one (better than clearing it — it was just
    derived, so sign-in never needs a per-request re-probe), and every active
    browser session is revoked. A probe failure is the same honest 502 envelope
    as setup's, with NOTHING committed and every session intact — a typo'd
    (syntactically valid but unreachable/wrong) url must not both break sign-in
    AND sign everyone out, which would leave a keyless install recoverable only
    by DB surgery (the exact never-locked-out violation ADR-0005 forbids).

    Ownership is deliberately NOT asserted here, unlike ``/setup/complete``: a
    PUT caller may be an ``X-Api-Key`` admin with no Plex account, so there is
    no resource list to assert against — reachability + identity is the right
    bar for a config write. Ownership continues to gate who can SIGN IN (and
    who is admin), which the freshly derived machine id now anchors to the NEW
    server (ADR-0016).

    Why sessions are revoked on a verified repoint: clearing/replacing the id
    alone only changes how FUTURE sign-ins resolve server access; an
    already-minted :class:`AuthSession` keeps authorizing against its persisted
    ``User.permissions`` for up to 30 days, so the OLD server's users (and
    admins) would silently survive the repoint. A repoint is an auth-domain
    change (ADR-0016 derives every session's authority from access to THE
    configured server), so everyone — including the admin performing the
    repoint — must re-sign-in and be re-evaluated against the NEW server. The
    self-lockout is deliberate and honest, not collateral damage: this request
    already passed auth at dependency time, so the response completes normally
    for the now-revoked session, and the admin's very next request
    re-authenticates against a server this PUT just PROVED is answering.
    Revocation stamps ``revoked_at`` on rows where it is NULL (the model's
    auditable-revoke convention) rather than deleting; API-key auth is
    untouched, so the ``X-Api-Key`` recovery path still works throughout.

    An UNVERIFIABLE identity change — the effective pair is incomplete (a
    half-configured install, or an explicit ``""`` clear) — cannot be probed:
    the write proceeds, the STALE cached id is dropped (nothing may keep
    anchoring sign-in to the old server), but sessions are NOT revoked. An
    incomplete identity cannot mint new sign-ins anyway (an honest
    ``service_not_configured``), and revoking on a half-configured install is
    the same lockout trap the probe exists to prevent; the PUT that completes
    the pair is a verified repoint and revokes then.
    """
    await _validate_disk_pressure_pair(body, session)

    store = SettingsStore(session)
    # Probe BEFORE any write: a failed verification must leave nothing behind.
    plex_identity_changed, machine_identifier = await _verify_plex_repoint(body, store, client)

    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None:
            continue
        if field in SECRET_SETTING_KEYS and value == SECRET_MASK:
            continue
        await store.set(field, _to_stored_string(value))
    if plex_identity_changed:
        if machine_identifier is None:
            # Unverifiable (incomplete pair): drop the stale anchor, keep sessions.
            await store.delete(PLEX_MACHINE_ID_SETTING)
        else:
            # Verified repoint, all in this SAME transaction: cache the id just
            # derived from the NEW server and revoke every ACTIVE session so
            # nobody's old-server authority outlives the repoint (the caller's
            # own session included, deliberately — see the docstring).
            await store.set(PLEX_MACHINE_ID_SETTING, machine_identifier)
            await session.execute(
                update(AuthSession)
                .where(AuthSession.revoked_at.is_(None))
                .values(revoked_at=datetime.now(UTC))
            )
    await session.commit()
    return await _redacted(store)


async def _validate_disk_pressure_pair(body: SettingsUpdate, session: AsyncSession) -> None:
    """422 when the EFFECTIVE (threshold, target) pair would be inverted.

    "Effective" means: this request's submitted value for a side, when it
    actually supplies one (present in ``model_fields_set`` AND non-``null`` —
    mirroring ``put_settings_endpoint``'s own persist loop, which treats a
    ``null`` field as "leave unchanged", never as "clear to null"); otherwise
    whatever is CURRENTLY STORED for that side (via the same typed getters the
    eviction sweep itself reads, so this check reasons about the identical
    effective values the sweep would see after this PUT commits). Catches the
    single-field split-update ``SettingsUpdate._target_at_or_below_threshold``
    documents as its known residual: e.g. a stored target of 80 plus a PUT
    naming only ``disk_pressure_threshold_percent=70`` would otherwise leave
    ``target(80) > threshold(70)`` in effect, with nothing else ever
    cross-checking it.
    """
    fields = body.model_fields_set
    threshold = (
        body.disk_pressure_threshold_percent
        if "disk_pressure_threshold_percent" in fields
        and body.disk_pressure_threshold_percent is not None
        else await get_disk_pressure_threshold_percent(session)
    )
    target = (
        body.disk_pressure_target_percent
        if "disk_pressure_target_percent" in fields
        and body.disk_pressure_target_percent is not None
        else await get_disk_pressure_target_percent(session)
    )
    if target > threshold:
        raise HTTPException(
            status_code=422,
            detail=(
                "disk_pressure_target_percent must be <= disk_pressure_threshold_percent "
                "(a target above the trigger leaves the whole threshold-to-target band "
                "under 'pressure' with nothing to evict)"
            ),
        )
