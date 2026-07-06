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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

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
    get_library,
    load_system_settings,
    require_admin,
)
from plex_manager.web.errors import AppError
from plex_manager.web.schemas import (
    AppApiKeyResponse,
    AppApiKeyStatusResponse,
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


@router.put("")
async def put_settings_endpoint(
    body: SettingsUpdate,
    session: Annotated[AsyncSession, Depends(get_session)],
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

    Repointing Plex invalidates the cached server identity: the
    ``plex_machine_identifier`` snapshot cached at setup
    (:data:`PLEX_MACHINE_ID_SETTING`) is the id post-init sign-in trusts to admit
    users. If an admin changes ``plex_url`` or ``plex_token`` to aim at a
    DIFFERENT server, that cached id would keep admitting the OLD server's users
    and rejecting the new owner. So when the EFFECTIVE value of either actually
    CHANGES here, the cached id is cleared in this SAME transaction and every
    subsequent sign-in re-derives it live from ``/identity`` (nothing post-init
    re-caches it — a per-sign-in probe of the operator's own server is the
    deliberately-simple price of a rare repoint). A masked-secret round-trip
    (``"***"``, skipped below) and a same-value re-PUT are NOT changes, so neither
    needlessly drops a still-valid id.

    Repointing also revokes EVERY active browser session, in the SAME transaction
    that clears the cached id. Clearing the id alone only changes how FUTURE
    sign-ins resolve server access; an already-minted :class:`AuthSession` keeps
    authorizing against its persisted ``User.permissions`` for up to 30 days, so
    the OLD server's users (and admins) would silently survive the repoint —
    exactly the stale-authority leak the id invalidation exists to close. A
    repoint is an auth-domain change (ADR-0016 derives every session's authority
    from access to THE configured server), so everyone — including the admin
    performing the repoint — must re-sign-in and be re-evaluated against the NEW
    server. The self-lockout is deliberate and honest, not collateral damage:
    this request already passed auth at dependency time, so the response below
    completes normally for the now-revoked session; the admin's very next request
    re-authenticates (they still own a Plex account — one sign-in, no data loss).
    Revocation stamps ``revoked_at`` on rows where it is NULL (the model's
    auditable-revoke convention) rather than deleting; API-key auth is untouched,
    so the ``X-Api-Key`` recovery path still works throughout.
    """
    await _validate_disk_pressure_pair(body, session)

    store = SettingsStore(session)
    plex_identity_changed = False
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None:
            continue
        if field in SECRET_SETTING_KEYS and value == SECRET_MASK:
            continue
        new_value = _to_stored_string(value)
        if field in ("plex_url", "plex_token") and new_value != await store.get(field):
            # Read the CURRENT stored value BEFORE overwriting it: only a genuine
            # change (not a same-value re-PUT) invalidates the cached machine id.
            plex_identity_changed = True
        await store.set(field, new_value)
    if plex_identity_changed:
        await store.delete(PLEX_MACHINE_ID_SETTING)
        # Same transaction as the id invalidation: revoke every ACTIVE session so
        # nobody's old-server authority outlives the repoint (see the docstring —
        # this includes the caller's own session, deliberately).
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
