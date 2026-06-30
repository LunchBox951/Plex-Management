"""Tests for the quality gate (check_quality) and profile-ordered compare."""

from __future__ import annotations

from plex_manager.domain.quality import (
    CAM,
    DVDSCR,
    SDTV,
    TELECINE,
    TELESYNC,
    UNKNOWN,
    WEBDL1080P,
    WEBDL2160P,
    WORKPRINT,
)
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.quality_service import (
    RejectionReason,
    check_quality,
    compare_by_profile,
)


def test_check_quality_rejects_prerelease_and_screener_tiers() -> None:
    profile = default_profile()
    for quality in (CAM, TELESYNC, TELECINE, WORKPRINT, DVDSCR, UNKNOWN):
        verdict = check_quality(quality, profile)
        assert verdict.accepted is False, quality.name
        assert verdict.reason is RejectionReason.QUALITY_NOT_WANTED
        assert verdict.quality is quality


def test_check_quality_accepts_allowed_quality() -> None:
    profile = default_profile()
    verdict = check_quality(WEBDL1080P, profile)
    assert verdict.accepted is True
    assert verdict.reason is None
    assert verdict.quality is WEBDL1080P


def test_check_quality_rejects_quality_absent_from_profile() -> None:
    # A profile with a single allowed item: anything else is "not wanted".
    profile = default_profile().model_copy(
        update={"items": [item for item in default_profile().items if item.quality_id == SDTV.id]}
    )
    verdict = check_quality(WEBDL1080P, profile)
    assert verdict.accepted is False
    assert verdict.reason is RejectionReason.QUALITY_NOT_WANTED


def test_compare_by_profile_orders_by_profile_index() -> None:
    profile = default_profile()
    assert compare_by_profile(SDTV, WEBDL1080P, profile) == -1
    assert compare_by_profile(WEBDL1080P, SDTV, profile) == 1
    assert compare_by_profile(WEBDL1080P, WEBDL1080P, profile) == 0
    # 2160p outranks 1080p by profile order, not just raw resolution.
    assert compare_by_profile(WEBDL2160P, WEBDL1080P, profile) == 1


def test_compare_by_profile_absent_sorts_below_present() -> None:
    # Profile containing only WEBDL1080p; CAM is absent -> sorts below.
    profile = default_profile().model_copy(
        update={
            "items": [item for item in default_profile().items if item.quality_id == WEBDL1080P.id]
        }
    )
    assert compare_by_profile(CAM, WEBDL1080P, profile) == -1
    assert compare_by_profile(WEBDL1080P, CAM, profile) == 1
