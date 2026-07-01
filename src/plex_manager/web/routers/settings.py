"""Settings endpoints — AUTHENTICATED (require the ``X-Api-Key`` header).

``GET`` returns a redacted view (secrets masked to ``"***"``). ``PUT`` upserts
the provided config, encrypting secret values at rest. Only fields present in the
request body are written; absent fields are left unchanged.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.db import get_session
from plex_manager.ports.library import LibraryPort
from plex_manager.web.deps import (
    SECRET_MASK,
    SECRET_SETTING_KEYS,
    SettingsStore,
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
    return library_options(await library.list_sections(), probe_writable=True)


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
    """
    store = SettingsStore(session)
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None:
            continue
        if field in SECRET_SETTING_KEYS and value == SECRET_MASK:
            continue
        await store.set(field, value)
    await session.commit()
    return await _redacted(store)
