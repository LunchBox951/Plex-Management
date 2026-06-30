"""Quality-profile endpoint — the serialized default profile (read-only). AUTHENTICATED."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from plex_manager.domain.quality import QUALITY_BY_ID, Resolution
from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.web.deps import get_quality_profile, require_api_key
from plex_manager.web.schemas import (
    QualityProfileItemResponse,
    QualityProfileResponse,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/quality-profile",
    tags=["quality-profile"],
    dependencies=[Depends(require_api_key)],
)


def _resolution_label(resolution: Resolution) -> str:
    return f"{resolution.value}p" if resolution is not Resolution.UNKNOWN else "unknown"


@router.get("")
async def get_quality_profile_endpoint(
    profile: Annotated[QualityProfile, Depends(get_quality_profile)],
) -> QualityProfileResponse:
    """Return the alpha default quality profile (ordered low -> high, with cutoff)."""
    items: list[QualityProfileItemResponse] = []
    for item in profile.items:
        quality = QUALITY_BY_ID[item.quality_id]
        items.append(
            QualityProfileItemResponse(
                quality_id=quality.id,
                name=quality.name,
                source=quality.source.name,
                resolution=_resolution_label(quality.resolution),
                allowed=item.allowed,
            )
        )
    cutoff = QUALITY_BY_ID[profile.cutoff_quality_id]
    return QualityProfileResponse(
        id=profile.id,
        name=profile.name,
        cutoff_quality_id=profile.cutoff_quality_id,
        cutoff_name=cutoff.name,
        upgrade_allowed=profile.upgrade_allowed,
        items=items,
    )
