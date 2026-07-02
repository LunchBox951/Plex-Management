"""Settings endpoints — AUTHENTICATED (require the ``X-Api-Key`` header).

``GET`` returns a redacted view (secrets masked to ``"***"``). ``PUT`` upserts
the provided config, encrypting secret values at rest. Only fields present in the
request body are written; absent fields are left unchanged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.db import get_session
from plex_manager.ports.library import LibraryPort
from plex_manager.web.deps import (
    SECRET_MASK,
    SECRET_SETTING_KEYS,
    SettingsStore,
    get_disk_pressure_target_percent,
    get_disk_pressure_threshold_percent,
    get_library,
    require_api_key,
)
from plex_manager.web.schemas import PlexLibraryOption, SettingsResponse, SettingsUpdate
from plex_manager.web.setup_validation import library_options

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/settings",
    tags=["settings"],
    dependencies=[Depends(require_api_key)],
)


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
