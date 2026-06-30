"""Quality profile — the ordered allow-list with a hard cutoff.

Ported from Radarr's ``Profiles/Qualities/QualityProfile.cs`` and
``QualityProfileQualityItem.cs``. The profile orders qualities low -> high by
weight and marks each allowed/disallowed. The decision engine uses the *index*
within ``items`` (never the raw resolution) to compare releases, and treats a
disallowed quality as a permanent rejection — never a down-score.

Pure domain: pydantic + the local quality registry only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from plex_manager.domain.quality import ALL_QUALITIES, WEBDL1080P

__all__ = [
    "DISALLOWED_BY_DEFAULT_IDS",
    "QualityProfile",
    "QualityProfileItem",
    "default_profile",
]

# Pre-release / screener / regional tiers rejected by the alpha default profile.
# Ids: Unknown(0), WORKPRINT(24), CAM(25), TELESYNC(26), TELECINE(27),
# DVDSCR(28), REGIONAL(29). Everything >= SDTV (weight 8) is allowed.
DISALLOWED_BY_DEFAULT_IDS: frozenset[int] = frozenset({0, 24, 25, 26, 27, 28, 29})


class QualityProfileItem(BaseModel):
    """One entry in a profile: a quality and whether it is wanted."""

    model_config = ConfigDict(frozen=True)

    quality_id: int
    allowed: bool


class QualityProfile(BaseModel):
    """An ordered (low -> high) list of qualities with an upgrade cutoff.

    ``items`` is ordered by weight ascending; ``get_index`` returns a release's
    position so the decision engine can compare by profile order rather than by
    raw resolution.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    cutoff_quality_id: int
    items: list[QualityProfileItem] = Field(default_factory=list[QualityProfileItem])
    min_format_score: int = 0
    upgrade_allowed: bool = True

    def get_index(self, quality_id: int) -> int | None:
        """Return the position of ``quality_id`` in ``items``, or ``None``."""
        for index, item in enumerate(self.items):
            if item.quality_id == quality_id:
                return index
        return None


def default_profile() -> QualityProfile:
    """The alpha's hardcoded default profile (DB seed, no UI yet).

    Items are ordered low -> high by weight. CAM/TELESYNC/TELECINE/WORKPRINT/
    DVDSCR/REGIONAL and Unknown are disallowed (permanent reject); everything
    >= SDTV is allowed. The upgrade cutoff is WEBDL-1080p.
    """
    items = [
        QualityProfileItem(
            quality_id=quality.id,
            allowed=quality.id not in DISALLOWED_BY_DEFAULT_IDS,
        )
        for quality in ALL_QUALITIES
    ]
    return QualityProfile(
        id=1,
        name="Default",
        cutoff_quality_id=WEBDL1080P.id,
        items=items,
    )
