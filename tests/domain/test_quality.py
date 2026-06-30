"""Sanity tests for the ported quality taxonomy (ids, weights, ordering)."""

from __future__ import annotations

from plex_manager.domain.quality import (
    ALL_QUALITIES,
    BLURAY1080P,
    CAM,
    QUALITY_BY_ID,
    SDTV,
    UNKNOWN,
    WEBDL1080P,
    Modifier,
    Quality,
    QualitySource,
    Resolution,
)


def test_quality_source_ordinals_match_radarr() -> None:
    assert QualitySource.UNKNOWN == 0
    assert QualitySource.CAM == 1
    assert QualitySource.TELESYNC == 2
    assert QualitySource.TELECINE == 3
    assert QualitySource.WORKPRINT == 4
    assert QualitySource.DVD == 5
    assert QualitySource.TV == 6
    assert QualitySource.WEBDL == 7
    assert QualitySource.WEBRIP == 8
    assert QualitySource.BLURAY == 9


def test_resolution_values() -> None:
    assert [r.value for r in Resolution] == [0, 360, 480, 540, 576, 720, 1080, 2160]


def test_modifier_ordinals_match_radarr() -> None:
    assert Modifier.NONE == 0
    assert Modifier.REGIONAL == 1
    assert Modifier.SCREENER == 2
    assert Modifier.RAWHD == 3
    assert Modifier.BRDISK == 4
    assert Modifier.REMUX == 5


def test_registry_has_30_qualities_with_unique_ids() -> None:
    assert len(ALL_QUALITIES) == 30
    ids = [q.id for q in ALL_QUALITIES]
    assert len(set(ids)) == 30
    assert QUALITY_BY_ID[WEBDL1080P.id] is WEBDL1080P


def test_all_qualities_ordered_by_weight_ascending() -> None:
    weights = [q.weight for q in ALL_QUALITIES]
    assert weights == sorted(weights)
    assert weights[0] == 1
    assert weights[-1] == 26


def test_known_weights_match_radarr_defaults() -> None:
    assert UNKNOWN.weight == 1
    assert CAM.weight == 3
    assert SDTV.weight == 8
    assert WEBDL1080P.weight == 18
    assert BLURAY1080P.weight == 19


def test_webdl1080p_shape() -> None:
    assert (
        Quality(
            id=3,
            name="WEBDL-1080p",
            source=QualitySource.WEBDL,
            resolution=Resolution.R1080P,
            modifier=Modifier.NONE,
            weight=18,
        )
        == WEBDL1080P
    )


def test_quality_is_frozen() -> None:
    import dataclasses

    try:
        WEBDL1080P.weight = 99  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        pass
    else:  # pragma: no cover - frozen dataclass must raise
        raise AssertionError("Quality must be immutable")
