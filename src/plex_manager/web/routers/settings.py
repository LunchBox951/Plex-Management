"""Settings endpoints — authenticated by Plex session or ``X-Api-Key``.

``GET`` returns a redacted view (secrets masked to ``"***"``). ``PUT`` upserts
the provided config, encrypting secret values at rest. Only fields present in the
request body are written; absent fields are left unchanged.

``GET /app-key`` and ``POST /app-key/rotate`` reveal / rotate the app's own
``X-Api-Key`` (``SystemSettings.app_api_key``) -- the belt-and-braces recovery
path for a lost key on a new device, or a full rotate if the key was ever
exposed (issue #28's OAuth-deferral analysis).
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.config import get_settings
from plex_manager.db import get_session
from plex_manager.ports.library import LibraryPort
from plex_manager.web.deps import (
    API_KEY_HEADER_NAME,
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
from plex_manager.web.schemas import (
    AppApiKeyResponse,
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

# Same byte length setup.complete() mints the initial key with
# (secrets.token_urlsafe(32) — a 43-char URL-safe token), so a rotated key is
# indistinguishable in shape/strength from the one setup issued.
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
    """
    system = await load_system_settings(session)
    if system is None or system.app_api_key is None:
        raise HTTPException(status_code=409, detail="not_initialized")
    return AppApiKeyResponse(app_api_key=system.app_api_key)


@router.post("/app-key/rotate")
async def rotate_app_key_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> AppApiKeyResponse:
    """Mint a brand-new app ``X-Api-Key``, invalidating the old one, and return it once.

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
            if not api_key_matches(observed, system.app_api_key):
                raise HTTPException(
                    status_code=409,
                    detail="app_key_changed",
                )
        new_key = secrets.token_urlsafe(_API_KEY_BYTES)
        system.app_api_key = new_key
        await session.commit()
    return AppApiKeyResponse(app_api_key=new_key)


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
    """
    await _validate_disk_pressure_pair(body, session)

    store = SettingsStore(session)
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None:
            continue
        if field in SECRET_SETTING_KEYS and value == SECRET_MASK:
            continue
        await store.set(field, _to_stored_string(value))
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
