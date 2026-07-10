"""Quality decision service — the hard cutoff gate and profile-ordered compare.

Ported from Radarr's ``QualityAllowedByProfileSpecification.cs`` and
``QualityModelComparer.cs``. ``check_quality`` is the first gate the decision
engine runs: a quality not present in the profile, or present but disallowed, is
a *permanent* rejection and is never scored. ``compare_by_profile`` orders two
qualities by their index in the profile — deliberately not by raw resolution, so
the operator's profile order wins.

Pure domain: stdlib + the local quality model only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from plex_manager.domain.quality import Quality
from plex_manager.domain.quality_profile import QualityProfile

__all__ = [
    "QualityVerdict",
    "RejectionReason",
    "check_quality",
    "compare_by_profile",
]


class RejectionReason(StrEnum):
    """Why a release was rejected. Surfaced to the operator, never swallowed."""

    QUALITY_NOT_WANTED = "quality_not_wanted"
    FORMAT_SCORE_TOO_LOW = "format_score_too_low"
    NO_QUALITY_DETECTED = "no_quality_detected"
    BLOCKLISTED = "blocklisted"
    WRONG_MEDIA = "wrong_media"
    MULTI_SEASON_PACK = "multi_season_pack"
    NOT_SEASON_PACK = "not_season_pack"


@dataclass(frozen=True)
class QualityVerdict:
    """Outcome of the quality gate. ``reason`` is ``None`` iff ``accepted``."""

    accepted: bool
    reason: RejectionReason | None
    quality: Quality


def check_quality(quality: Quality, profile: QualityProfile) -> QualityVerdict:
    """Hard gate: reject permanently if the quality is absent or disallowed.

    A rejected quality is *never* scored — this enforces the north-star hard
    cutoff (no relaxed fallback to a blocked source).
    """
    index = profile.get_index(quality.id)
    if index is None or not profile.items[index].allowed:
        return QualityVerdict(
            accepted=False,
            reason=RejectionReason.QUALITY_NOT_WANTED,
            quality=quality,
        )
    return QualityVerdict(accepted=True, reason=None, quality=quality)


def compare_by_profile(a: Quality, b: Quality, profile: QualityProfile) -> int:
    """Compare two qualities by profile index: -1 / 0 / +1 (a vs b).

    Ordering is by position in ``profile.items`` (operator intent), not by raw
    resolution. A quality absent from the profile sorts below any present one.
    """
    ia = profile.get_index(a.id)
    ib = profile.get_index(b.id)
    if ia is None and ib is None:
        return 0
    if ia is None:
        return -1
    if ib is None:
        return 1
    return (ia > ib) - (ia < ib)
