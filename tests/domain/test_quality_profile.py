"""Tests for the default quality profile: ordering, cutoff, and gating."""

from __future__ import annotations

from plex_manager.domain.quality import (
    ALL_QUALITIES,
    CAM,
    DVDSCR,
    REGIONAL,
    SDTV,
    TELECINE,
    TELESYNC,
    UNKNOWN,
    WEBDL1080P,
    WORKPRINT,
)
from plex_manager.domain.quality_profile import default_profile


def test_default_profile_items_ordered_low_to_high_by_weight() -> None:
    profile = default_profile()
    assert [item.quality_id for item in profile.items] == [q.id for q in ALL_QUALITIES]


def test_default_profile_cutoff_is_webdl_1080p() -> None:
    profile = default_profile()
    assert profile.cutoff_quality_id == WEBDL1080P.id
    cutoff_index = profile.get_index(profile.cutoff_quality_id)
    assert cutoff_index is not None
    assert profile.items[cutoff_index].allowed is True


def test_prerelease_and_screener_tiers_disallowed() -> None:
    profile = default_profile()
    for quality in (UNKNOWN, WORKPRINT, CAM, TELESYNC, TELECINE, DVDSCR, REGIONAL):
        index = profile.get_index(quality.id)
        assert index is not None
        assert profile.items[index].allowed is False, quality.name


def test_sdtv_and_above_allowed() -> None:
    profile = default_profile()
    for quality in ALL_QUALITIES:
        index = profile.get_index(quality.id)
        assert index is not None
        if quality.weight >= SDTV.weight:
            assert profile.items[index].allowed is True, quality.name


def test_get_index_returns_none_for_unknown_id() -> None:
    profile = default_profile()
    assert profile.get_index(99999) is None
