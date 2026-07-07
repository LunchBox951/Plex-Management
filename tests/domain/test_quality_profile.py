"""Tests for the default quality profile: ordering, cutoff, and gating."""

from __future__ import annotations

from plex_manager.domain.quality import (
    ALL_QUALITIES,
    CAM,
    DVDR,
    DVDSCR,
    REGIONAL,
    SDTV,
    TELECINE,
    TELESYNC,
    UNKNOWN,
    WEBDL1080P,
    WORKPRINT,
)
from plex_manager.domain.quality_profile import DISALLOWED_BY_DEFAULT_IDS, default_profile


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


def test_disallowed_ids_derived_from_sdtv_floor() -> None:
    # Locks the derivation to the SDTV weight floor AND to the current set, so
    # the #111 refactor is provably non-behavioral.
    assert frozenset(q.id for q in ALL_QUALITIES if q.weight < SDTV.weight) == (
        DISALLOWED_BY_DEFAULT_IDS
    )
    assert frozenset({0, 24, 25, 26, 27, 28, 29}) == DISALLOWED_BY_DEFAULT_IDS


def test_every_below_sdtv_quality_is_disallowed_by_default() -> None:
    # Future-proof complement to test_sdtv_and_above_allowed: asserts the "<"
    # side too, so a new below-SDTV quality can never sneak in allowed.
    profile = default_profile()
    for quality in ALL_QUALITIES:
        index = profile.get_index(quality.id)
        assert index is not None
        assert profile.items[index].allowed is (quality.weight >= SDTV.weight), quality.name


def test_dvdr_allowed_by_default() -> None:
    # Guards the #107 DVD-remux -> DVD-R interaction: DVD-R (weight 10) sits
    # above the SDTV floor (weight 8), so it stays allowed.
    profile = default_profile()
    index = profile.get_index(DVDR.id)
    assert index is not None
    assert profile.items[index].allowed is True


def test_items_is_an_immutable_tuple_not_a_mutable_list() -> None:
    """Issue #106: ``frozen=True`` on ``QualityProfile`` blocks reassigning
    ``profile.items`` but never stopped a plain list from being mutated IN
    PLACE -- which would corrupt every other holder of the same (shared)
    profile instance. ``items`` must be an immutable tuple, so there is no
    ``.append``/``.sort`` surface left to exploit."""
    profile = default_profile()
    assert isinstance(profile.items, tuple)
    assert not hasattr(profile.items, "append")
