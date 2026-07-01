"""Tests for guessit-field -> quality mapping, incl. the CAM/TS golden table.

The field mappings below are the *exact* shapes guessit 4.0.2 emits for these
release names (verified locally); the tests stay pure (no guessit import) by
feeding those recorded mappings into ``to_parsed_release``.
"""

from __future__ import annotations

from plex_manager.domain.quality import (
    BLURAY480P,
    BLURAY1080P,
    DVDSCR,
    REGIONAL,
    REMUX2160P,
    SDTV,
    TELESYNC,
    WEBDL480P,
    WEBDL720P,
    WEBDL1080P,
    WEBRIP1080P,
    Modifier,
    Quality,
    QualitySource,
    Resolution,
)
from plex_manager.domain.quality import (
    UNKNOWN as UNKNOWN_QUALITY,
)
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.quality_service import check_quality
from plex_manager.domain.source_mapping import (
    _coerce_episode,
    map_modifier,
    map_source,
    resolve_quality,
    to_parsed_release,
)

# The only sources the reject-keyword net is permitted to emit.
_REJECT_TIERS: frozenset[QualitySource] = frozenset(
    {
        QualitySource.CAM,
        QualitySource.TELESYNC,
        QualitySource.TELECINE,
        QualitySource.WORKPRINT,
    }
)

# (raw_title, guessit-style field mapping) for known pirate-cam leak names.
# These MUST all classify into a reject tier and be rejected by the default
# profile. HQCAM / PDVD carry NO guessit ``source`` (guessit files them as
# ``alternative_title``) — they rely entirely on the reject-keyword net.
_LEAK_TABLE: list[tuple[str, dict[str, object]]] = [
    ("Movie.2024.TELESYNC.x264-GROUP", {"source": "Telesync"}),
    ("Movie.2024.1080p.HDTS.x264-GROUP", {"source": "HD Telesync", "screen_size": "1080p"}),
    ("Movie.2024.CAMRip.x264-GROUP", {"source": "Camera", "other": "Rip"}),
    ("Movie.2024.HQCAM.x264-GROUP", {"alternative_title": "HQCAM"}),
    ("Movie.2024.HDCAM.x264-GROUP", {"source": "HD Camera"}),
    ("Movie.2024.PDVD.x264-GROUP", {"alternative_title": "PDVD"}),
    ("Movie.2024.WORKPRINT.x264-GROUP", {"source": "Workprint"}),
    ("Movie.2024.DVDSCR.x264-GROUP", {"source": "DVD", "other": "Screener"}),
    ("Movie.2024.TS-Rip.x264-GROUP", {"source": "Telesync", "other": "Rip"}),
    # R5/R6 regional pre-releases (prototype BLOCKED_PATTERNS \bR5\b/\bR6\b).
    # R5: guessit emits other:'Region 5' -> map_modifier REGIONAL.
    ("Movie.2024.R5.DVDRip.x264-GRP", {"source": "DVD", "other": ["Region 5", "Rip"]}),
    # R6: guessit MISSES 'Region 6' (only emits 'Rip') -> raw-title net catches R6.
    ("Movie.2024.R6.DVDRip.x264-GRP", {"source": "DVD", "other": "Rip"}),
    # R5 without a DVD source token: guessit emits other:'Region 5', source None.
    ("Movie.2024.R5.x264-GRP", {"other": "Region 5"}),
    # Non-standard screener tag: guessit emits NO 'other' -> raw-title net catches SCR.
    ("Movie.2024.1080p.HC.SCR.WEB.x264-GROUP", {"source": "Web", "screen_size": "1080p"}),
]

# Acceptable releases: guessit field shapes that must classify into an allowed
# quality (and the reject net must NOT fire on their titles).
_ACCEPT_TABLE: list[tuple[str, dict[str, object], Quality]] = [
    (
        "Movie.2024.1080p.WEB-DL.x264-GROUP",
        {"source": "Web", "screen_size": "1080p"},
        WEBDL1080P,
    ),
    (
        "Movie.2024.1080p.WEBRip.x264-GROUP",
        {"source": "Web", "screen_size": "1080p", "other": "Rip"},
        WEBRIP1080P,
    ),
    (
        "Movie.2024.1080p.BluRay.x264-GROUP",
        {"source": "Blu-ray", "screen_size": "1080p"},
        BLURAY1080P,
    ),
    (
        "Show.S01E02.720p.WEB-DL.x264-GROUP",
        {"source": "Web", "screen_size": "720p"},
        WEBDL720P,
    ),
    (
        "Movie.2024.480p.HDTV.x264-GROUP",
        {"source": "HDTV", "screen_size": "480p"},
        SDTV,
    ),
    (
        "Movie.2024.2160p.UHD.BluRay.REMUX.x265-GROUP",
        {"source": "Ultra HD Blu-ray", "screen_size": "2160p", "other": "Remux"},
        REMUX2160P,
    ),
]


def test_golden_leak_names_are_rejected() -> None:
    profile = default_profile()
    for raw_title, fields in _LEAK_TABLE:
        parsed = to_parsed_release(fields, raw_title)
        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
        verdict = check_quality(quality, profile)
        assert verdict.accepted is False, f"{raw_title} -> {quality.name} was accepted"


def test_golden_acceptable_names_resolve_and_pass() -> None:
    profile = default_profile()
    for raw_title, fields, expected in _ACCEPT_TABLE:
        parsed = to_parsed_release(fields, raw_title)
        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
        assert quality is expected, f"{raw_title} -> {quality.name}, expected {expected.name}"
        assert check_quality(quality, profile).accepted is True, raw_title


def test_telesync_resolution_does_not_promote() -> None:
    # "1080p HDTS": source must win over the 1080p resolution token.
    parsed = to_parsed_release(
        {"source": "HD Telesync", "screen_size": "1080p"},
        "Movie.2024.1080p.HDTS.x264-GROUP",
    )
    assert parsed.source is QualitySource.TELESYNC
    assert parsed.resolution is Resolution.R1080P
    assert resolve_quality(parsed.source, parsed.resolution, parsed.modifier) is TELESYNC


def test_dvdscr_maps_to_screener_quality() -> None:
    parsed = to_parsed_release(
        {"source": "DVD", "other": "Screener"}, "Movie.2024.DVDSCR.x264-GROUP"
    )
    assert parsed.modifier is Modifier.SCREENER
    assert resolve_quality(parsed.source, parsed.resolution, parsed.modifier) is DVDSCR


def test_web_rip_flag_selects_webrip() -> None:
    webdl = to_parsed_release({"source": "Web", "screen_size": "1080p"}, "X.1080p.WEB-DL")
    webrip = to_parsed_release(
        {"source": "Web", "screen_size": "1080p", "other": "Rip"}, "X.1080p.WEBRip"
    )
    assert webdl.source is QualitySource.WEBDL
    assert webrip.source is QualitySource.WEBRIP


def test_revision_proper_and_repack() -> None:
    proper = to_parsed_release(
        {"source": "Web", "screen_size": "1080p", "proper_count": "1"},
        "Movie.2024.PROPER.1080p.WEB-DL-GROUP",
    )
    assert proper.revision.version == 2
    assert proper.revision.is_repack is False

    repack = to_parsed_release(
        {"source": "Web", "screen_size": "1080p", "proper_count": "1"},
        "Movie.2024.REPACK.1080p.WEB-DL-GROUP",
    )
    assert repack.revision.is_repack is True


def test_unknown_source_resolves_to_unknown_quality() -> None:
    parsed = to_parsed_release({"title": "Mystery"}, "Mystery.Release.Name")
    assert parsed.source is QualitySource.UNKNOWN
    quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
    assert quality is UNKNOWN_QUALITY
    assert check_quality(quality, default_profile()).accepted is False


# -- reject-keyword net invariant: it may ONLY ever add rejections --------------


def test_net_only_emits_reject_tiers_over_a_corpus() -> None:
    # With empty guessit fields, ``map_source`` returns UNKNOWN unless the
    # reject-keyword net forces a reject tier. Across a corpus of titles, the
    # result is therefore always UNKNOWN or a reject tier — the net is
    # structurally incapable of emitting an acceptable source.
    corpus = [
        "Movie.HQCAM.x264",
        "Movie.HD-CAM.x264",
        "Movie.1080p.HDTS.x264",
        "Movie.TS-Rip.x264",
        "Movie.PDVD.x264",
        "Movie.PreDVD.x264",
        "Movie.WORKPRINT.x264",
        "Movie.HDTC.x264",
        "Random.Title.Without.Markers",
        "Camelot.2024.1080p.BluRay.x264-GROUP",
    ]
    for title in corpus:
        source = map_source({}, title)
        assert source is QualitySource.UNKNOWN or source in _REJECT_TIERS, title


def test_net_fires_on_leaks_and_is_silent_on_clean_titles() -> None:
    leaks = [
        "Movie.HQCAM.x264",
        "Movie.HD-CAM.x264",
        "Movie.1080p.HDTS.x264",
        "Movie.TS-Rip.x264",
        "Movie.PDVD.x264",
        "Movie.PreDVD.x264",
        "Movie.WORKPRINT.x264",
        "Movie.HDTC.x264",
    ]
    for title in leaks:
        assert map_source({}, title) in _REJECT_TIERS, title

    clean = [
        "Movie.2024.1080p.WEB-DL.x264-GROUP",
        "Movie.2024.2160p.BluRay.x265-GROUP",
        "Show.S01E01.720p.HDTV.x264-GROUP",
        "Camelot.2024.1080p.BluRay.x264-GROUP",  # 'Cam' substring must not fire
    ]
    for title in clean:
        # Empty fields => base UNKNOWN; the net must not fire on these titles.
        assert map_source({}, title) is QualitySource.UNKNOWN, title


def test_reject_net_never_promotes_an_acceptable_resolution() -> None:
    # A title that the net flags can never end up acceptable, even with a strong
    # resolution token present.
    profile = default_profile()
    parsed = to_parsed_release({"screen_size": "2160p"}, "Movie.2024.2160p.HQCAM.x265-GROUP")
    quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
    assert check_quality(quality, profile).accepted is False


# -- reject-MODIFIER net: SCREENER / REGIONAL hard-cutoff backstops -------------


def test_map_modifier_emits_regional_for_region_codes() -> None:
    # guessit-emitted Region markers (the dead REGIONAL path is now live).
    assert map_modifier({"other": "Region 5"}, "Movie.2024.R5.DVDRip") is Modifier.REGIONAL
    assert map_modifier({"other": ["Region 6", "Rip"]}, "X.R6.DVDRip") is Modifier.REGIONAL
    # raw-title backstop when guessit misses the region marker entirely (R6).
    assert map_modifier({"other": "Rip"}, "Movie.2024.R6.DVDRip.x264-GRP") is Modifier.REGIONAL
    assert map_modifier({}, "Movie.2024.R5.x264-GRP") is Modifier.REGIONAL


def test_map_modifier_screener_backstop_when_guessit_misses_other() -> None:
    # guessit detected source=Web but did NOT flag a screener (verified locally).
    assert map_modifier({"source": "Web"}, "Movie.2024.1080p.HC.SCR.WEB.x264") is Modifier.SCREENER
    assert map_modifier({}, "Movie.2024.DVDSCR.x264-GRP") is Modifier.SCREENER
    assert map_modifier({}, "Movie.2024.BDSCR.x264-GRP") is Modifier.SCREENER


def test_r5_dvdrip_does_not_leak_through_as_acceptable_dvd() -> None:
    # The end-to-end leak: R5.DVDRip parses to DVD source; without the REGIONAL
    # modifier path it would resolve to acceptable DVD and be GRABBED.
    profile = default_profile()
    parsed = to_parsed_release(
        {"source": "DVD", "other": ["Region 5", "Rip"]}, "Movie.2024.R5.DVDRip.x264-GRP"
    )
    assert parsed.modifier is Modifier.REGIONAL
    quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
    assert quality is REGIONAL
    assert check_quality(quality, profile).accepted is False


def test_map_modifier_does_not_fire_on_clean_titles() -> None:
    # The reject-modifier net must stay silent on clean releases (no SCR/R5/R6).
    for title in (
        "Movie.2024.1080p.WEB-DL.x264-GROUP",
        "Movie.2024.2160p.BluRay.x265-GROUP",
        "Show.S01E01.720p.HDTV.x264-GROUP",
    ):
        assert map_modifier({}, title) is Modifier.NONE, title
    # Remux is still recognised and is NOT overridden by the reject net.
    assert map_modifier({"other": "Remux"}, "Movie.2024.2160p.BluRay.REMUX.x265") is Modifier.REMUX


# -- resolve_quality conservative source-only fallback (no over-promotion) ------


# -- _coerce_episode: mirrors _coerce_season exactly ----------------------------


def test_coerce_episode_single_int() -> None:
    assert _coerce_episode(5) == 5


def test_coerce_episode_list_of_ints() -> None:
    assert _coerce_episode([5, 6]) == [5, 6]


def test_coerce_episode_none_when_absent() -> None:
    assert _coerce_episode(None) is None


def test_coerce_episode_bool_excluded() -> None:
    # bool is an int subclass; must not be misread as episode 0/1.
    assert _coerce_episode(True) is None
    assert _coerce_episode(False) is None


def test_coerce_episode_filters_non_int_list_members() -> None:
    assert _coerce_episode([5, "x", None, 6]) == [5, 6]


def test_coerce_episode_empty_list_collapses_to_none() -> None:
    assert _coerce_episode([]) is None
    assert _coerce_episode(["x"]) is None


def test_coerce_episode_wired_into_to_parsed_release() -> None:
    parsed = to_parsed_release(
        {"title": "Show", "season": 2, "episode": 5},
        "Show.S02E05.1080p.WEB-DL.x264-GROUP",
    )
    assert parsed.season == 2
    assert parsed.episode == 5


def test_coerce_episode_multi_episode_wired_into_to_parsed_release() -> None:
    parsed = to_parsed_release(
        {"title": "Show", "season": 2, "episode": [5, 6]},
        "Show.S02E05E06.1080p.WEB-DL.x264-GROUP",
    )
    assert parsed.episode == [5, 6]


def test_coerce_episode_absent_for_season_pack() -> None:
    # A whole-season pack carries no ``episode`` field at all.
    parsed = to_parsed_release(
        {"title": "Show", "season": 2},
        "Show.S02.1080p.WEB-DL.x264-GROUP",
    )
    assert parsed.season == 2
    assert parsed.episode is None


def test_resolve_quality_source_only_fallback_does_not_over_promote() -> None:
    # Known source, UNKNOWN resolution -> lowest-weight quality for that source.
    assert resolve_quality(QualitySource.WEBDL, Resolution.UNKNOWN, Modifier.NONE) is WEBDL480P
    assert resolve_quality(QualitySource.BLURAY, Resolution.UNKNOWN, Modifier.NONE) is BLURAY480P
    # An unmatched (source, resolution) pair also falls back to the lowest weight,
    # so an ambiguous parse can never over-promote.
    assert resolve_quality(QualitySource.WEBDL, Resolution.R540P, Modifier.NONE) is WEBDL480P
