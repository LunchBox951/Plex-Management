"""Tests for guessit-field -> quality mapping, incl. the CAM/TS golden table.

The field mappings below are the *exact* shapes guessit 4.0.2 emits for these
release names (verified locally); the tests stay pure (no guessit import) by
feeding those recorded mappings into ``to_parsed_release``.
"""

from __future__ import annotations

from plex_manager.domain.quality import (
    BLURAY480P,
    BLURAY720P,
    BLURAY1080P,
    DVDR,
    DVDSCR,
    HDTV1080P,
    REGIONAL,
    REMUX1080P,
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
    _coerce_episode,  # pyright: ignore[reportPrivateUsage]
    _strip_release_group,  # pyright: ignore[reportPrivateUsage]
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


# -- resolve_quality REMUX gating by source (#107) ------------------------------


def test_resolve_quality_remux_gated_by_source() -> None:
    # BluRay: remux tier at 2160p/1080p. A KNOWN resolution below 1080p has no
    # remux tier, so the token is ignored and the source+resolution lookup wins
    # (720p -> Bluray720p, 480p -> Bluray480p). A BluRay remux with NO parseable
    # resolution takes Radarr's bluray-branch assumption of Remux1080p rather than
    # falling through to the source-only Bluray-480p fallback (#107 regression).
    assert resolve_quality(QualitySource.BLURAY, Resolution.R2160P, Modifier.REMUX) is REMUX2160P
    assert resolve_quality(QualitySource.BLURAY, Resolution.R1080P, Modifier.REMUX) is REMUX1080P
    assert resolve_quality(QualitySource.BLURAY, Resolution.R720P, Modifier.REMUX) is BLURAY720P
    assert resolve_quality(QualitySource.BLURAY, Resolution.R480P, Modifier.REMUX) is BLURAY480P
    assert resolve_quality(QualitySource.BLURAY, Resolution.UNKNOWN, Modifier.REMUX) is REMUX1080P
    # Unknown source: Radarr's no-source-remux branch, cell for cell -- 2160p ->
    # Remux2160p, 1080p -> Remux1080p, 720p -> Bluray720p, 480p -> Bluray480p.
    # SD oddballs (576p et al.) and a missing resolution have no cell in that
    # branch (Radarr resolves them to Unknown), so they fall through to the
    # conservative UNKNOWN-source guard.
    assert resolve_quality(QualitySource.UNKNOWN, Resolution.R2160P, Modifier.REMUX) is REMUX2160P
    assert resolve_quality(QualitySource.UNKNOWN, Resolution.R1080P, Modifier.REMUX) is REMUX1080P
    assert resolve_quality(QualitySource.UNKNOWN, Resolution.R720P, Modifier.REMUX) is BLURAY720P
    assert resolve_quality(QualitySource.UNKNOWN, Resolution.R480P, Modifier.REMUX) is BLURAY480P
    assert (
        resolve_quality(QualitySource.UNKNOWN, Resolution.R576P, Modifier.REMUX) is UNKNOWN_QUALITY
    )
    assert (
        resolve_quality(QualitySource.UNKNOWN, Resolution.UNKNOWN, Modifier.REMUX)
        is UNKNOWN_QUALITY
    )
    # DVD: conservative in-tier choice, regardless of resolution.
    assert resolve_quality(QualitySource.DVD, Resolution.R480P, Modifier.REMUX) is DVDR
    assert resolve_quality(QualitySource.DVD, Resolution.R1080P, Modifier.REMUX) is DVDR
    # WEBDL/WEBRIP/TV: the modifier is ignored; source+resolution lookup wins.
    assert resolve_quality(QualitySource.WEBDL, Resolution.R1080P, Modifier.REMUX) is WEBDL1080P
    assert resolve_quality(QualitySource.WEBRIP, Resolution.R1080P, Modifier.REMUX) is WEBRIP1080P
    assert resolve_quality(QualitySource.TV, Resolution.R1080P, Modifier.REMUX) is HDTV1080P


# (raw_title, recorded guessit 4.1.0 field mapping, expected quality) for the
# source-gated REMUX table above, fed through the full to_parsed_release ->
# resolve_quality pipeline.
_REMUX_TABLE: list[tuple[str, dict[str, object], Quality]] = [
    (
        "Movie.2024.1080p.WEB-DL.REMUX-GRP",
        {"source": "Web", "screen_size": "1080p", "other": "Remux", "release_group": "GRP"},
        WEBDL1080P,
    ),
    (
        "Movie.2024.1080p.WEBRip.REMUX-GRP",
        {
            "source": "Web",
            "screen_size": "1080p",
            "other": ["Rip", "Remux"],
            "release_group": "GRP",
        },
        WEBRIP1080P,
    ),
    (
        "Movie.2024.1080p.HDTV.REMUX-GRP",
        {"source": "HDTV", "screen_size": "1080p", "other": "Remux", "release_group": "GRP"},
        HDTV1080P,
    ),
    (
        "Movie.2024.1080p.DVD.REMUX-GRP",
        {"source": "DVD", "screen_size": "1080p", "other": "Remux", "release_group": "GRP"},
        DVDR,
    ),
    (
        "Movie.2024.1080p.REMUX-GRP",  # no source
        {"screen_size": "1080p", "other": "Remux", "release_group": "GRP"},
        REMUX1080P,
    ),
    (
        "Movie.2024.1080p.BluRay.REMUX-GRP",  # unchanged
        {"source": "Blu-ray", "screen_size": "1080p", "other": "Remux", "release_group": "GRP"},
        REMUX1080P,
    ),
    (
        "Movie.2024.720p.BluRay.REMUX-GRP",  # remux ignored at known 720p
        {"source": "Blu-ray", "screen_size": "720p", "other": "Remux", "release_group": "GRP"},
        BLURAY720P,
    ),
    (
        "Movie.BluRay.REMUX.x264-GRP",  # BluRay remux, NO parseable resolution
        {"source": "Blu-ray", "other": "Remux", "release_group": "GRP"},
        REMUX1080P,
    ),
    (
        "Movie.2024.2160p.REMUX-GRP",  # no-source 2160p
        {"screen_size": "2160p", "other": "Remux", "release_group": "GRP"},
        REMUX2160P,
    ),
    (
        # No-source 720p remux: Radarr's no-source-remux branch yields Bluray720p
        # ("720p remux should fallback as 720p BluRay"), NOT Unknown -- falling to
        # the UNKNOWN guard here rejected a previously-accepted release.
        "Movie.2024.720p.REMUX-GRP",
        {"screen_size": "720p", "other": "Remux", "release_group": "GRP"},
        BLURAY720P,
    ),
    (
        # No-source 480p remux: the same branch's SD cell -> Bluray480p.
        "Movie.2024.480p.REMUX-GRP",
        {"screen_size": "480p", "other": "Remux", "release_group": "GRP"},
        BLURAY480P,
    ),
]


def test_remux_classification_by_source_end_to_end() -> None:
    profile = default_profile()
    for raw_title, fields, expected in _REMUX_TABLE:
        parsed = to_parsed_release(fields, raw_title)
        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
        assert quality is expected, f"{raw_title} -> {quality.name}, expected {expected.name}"
        assert check_quality(quality, profile).accepted is True, raw_title


# -- reject nets ignore the release-group suffix (#108) -------------------------

# (raw_title, recorded guessit 4.1.0 fields) for release-group names that
# collide with a reject-net token. Pre-#108 these all false-rejected; post-#108
# they must resolve to the clean WEBDL1080P classification the title implies.
_GROUP_COLLISION_TABLE: list[tuple[str, dict[str, object]]] = [
    (
        "Movie.2024.1080p.WEB-DL.x264-SCR",
        {"source": "Web", "screen_size": "1080p", "release_group": "SCR"},
    ),
    (
        "Movie.2024.1080p.WEB-DL.x264-R5",
        {"source": "Web", "screen_size": "1080p", "release_group": "R5"},
    ),
    (
        "Movie.2024.1080p.WEB-DL.x264-R6",
        {"source": "Web", "screen_size": "1080p", "release_group": "R6"},
    ),
    (
        "Movie.2024.1080p.WEB-DL.x264-HQCAM",
        {"source": "Web", "screen_size": "1080p", "release_group": "HQCAM"},
    ),
    # Group token ALSO embedded earlier in the title. An unanchored first-match
    # strip deletes the ``Scr`` of ``Scream`` and leaves the genuine ``-SCR``
    # suffix behind to false-trip the reject-modifier net (regression guard).
    (
        "Scream.2024.1080p.WEB-DL.x264-SCR",
        {"source": "Web", "screen_size": "1080p", "release_group": "SCR"},
    ),
    # ``R5`` embedded in the title word ``Barr5`` (contrived but exercises the
    # same "token appears before its suffix occurrence" hazard for the R-code).
    (
        "Barr5.2024.1080p.WEB-DL.x264-R5",
        {"source": "Web", "screen_size": "1080p", "release_group": "R5"},
    ),
    # Group token appears on BOTH the release folder and the filename. Import
    # validation parses the full relative PATH, so a suffix-only strip would
    # leave the folder's ``-SCR`` behind to false-reject a clean import. Stripping
    # every attached occurrence removes both, so this resolves to clean WEBDL.
    (
        "Movie.2024.1080p.WEB-DL.x264-SCR/Movie.2024.1080p.WEB-DL.x264-SCR.mkv",
        {"source": "Web", "screen_size": "1080p", "release_group": "SCR"},
    ),
    # Bracket-attached group tag (anime/p2p convention): the strip recognizes
    # ``[SCR]`` as a group tag too, so a bracketed group named after a reject
    # token cannot false-trip the net.
    (
        "[SCR] Movie 2024 1080p WEB-DL x264",
        {"source": "Web", "screen_size": "1080p", "release_group": "SCR"},
    ),
]


def test_reject_nets_ignore_release_group_suffix() -> None:
    profile = default_profile()
    for raw_title, fields in _GROUP_COLLISION_TABLE:
        parsed = to_parsed_release(fields, raw_title)
        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
        assert quality is WEBDL1080P, f"{raw_title} -> {quality.name}"
        assert check_quality(quality, profile).accepted is True, raw_title


def test_real_reject_token_survives_release_group_strip() -> None:
    # The group happens to collide with a reject token too (or share a real
    # reject source): the guessit-native source classification still wins, so
    # stripping the group must not launder a genuine CAM release.
    profile = default_profile()
    for raw_title, fields, reject_name in (
        ("Movie.2024.HDCAM.x264-RARBG", {"source": "HD Camera", "release_group": "RARBG"}, "CAM"),
        ("Movie.2024.HDCAM.x264-CAM", {"source": "HD Camera", "release_group": "CAM"}, "CAM"),
        # Genuine embedded reject token that the raw-title net must still catch
        # AFTER the group strip. guessit missed the screener flag (no ``other``),
        # so classification leans on the net: stripping the ``-SCR`` suffix group
        # tag must leave the ``SCR`` embedded in ``DVDSCR`` intact so the release
        # still rejects as a screener rather than laundering to acceptable DVD.
        ("Movie.2024.DVDSCR.x264-SCR", {"source": "DVD", "release_group": "SCR"}, "DVDSCR"),
        # Strip-all guard on a duplicated relative path: a genuine ``DVDSCR``
        # token on BOTH the folder and the filename, with the group ALSO named
        # ``SCR``. Removing every ``-SCR`` group tag must still leave both
        # embedded ``DVDSCR`` tokens intact, so the release rejects as a screener
        # rather than laundering to an acceptable DVD.
        (
            "Movie.2024.DVDSCR.x264-SCR/Movie.2024.DVDSCR.x264-SCR.mkv",
            {"source": "DVD", "release_group": "SCR"},
            "DVDSCR",
        ),
        # A STANDALONE dot-attached body token colliding with the group name:
        # ``HC.SCR`` is a genuine hardcoded-screener marker guessit misses (no
        # ``other`` emitted -- same shape as the ``HC.SCR.WEB`` leak-table row),
        # while ``-SCR`` is the group tag. The attachment-anchored strip removes
        # only the hyphen-attached tag; the dot-attached ``SCR`` stays for the
        # net, so the screener still rejects instead of laundering to WEBDL.
        (
            "Movie.2024.1080p.HC.SCR.WEB-DL.x264-SCR",
            {"source": "Web", "screen_size": "1080p", "release_group": "SCR"},
            "DVDSCR",
        ),
        # Same laundering hazard with a hyphen separator: the reject regexes treat
        # ``HC-SCR`` as a real screener marker, so the group strip must not remove
        # that body token while removing the trailing ``-SCR`` release group.
        (
            "Movie.2024.1080p.HC-SCR.WEB-DL.x264-SCR",
            {"source": "Web", "screen_size": "1080p", "release_group": "SCR"},
            "DVDSCR",
        ),
    ):
        parsed = to_parsed_release(fields, raw_title)
        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
        assert quality.name == reject_name, f"{raw_title} -> {quality.name}"
        assert check_quality(quality, profile).accepted is False, raw_title


def test_strip_release_group_noop_without_group() -> None:
    title = "Movie.2024.1080p.WEB-DL.x264-SCR"
    assert _strip_release_group(title, {}) == title
    assert _strip_release_group(title, {"release_group": None}) == title
    assert _strip_release_group(title, {"release_group": ""}) == title


def test_strip_release_group_removes_span_case_insensitive() -> None:
    assert _strip_release_group("Movie-scr", {"release_group": "SCR"}) == "Movie-"
    # Regex-special characters in the group name are matched literally.
    assert _strip_release_group("Movie-R5+X", {"release_group": "R5+X"}) == "Movie-"


def test_strip_release_group_anchors_to_suffix_not_an_embedded_mention() -> None:
    # The group token also appears earlier in the title (inside a longer word).
    # The strip must remove the *suffix* group tag, never the embedded mention:
    # deleting the ``Scr`` of ``Scream`` would leave the genuine ``-SCR`` suffix
    # to false-trip the reject net.
    assert (
        _strip_release_group("Scream.2024.1080p.WEB-DL.x264-SCR", {"release_group": "SCR"})
        == "Scream.2024.1080p.WEB-DL.x264-"
    )
    # Word-boundary flanks: a group token that is a substring of a longer
    # alphanumeric run (``SCR`` inside ``DVDSCR``) is left intact, so a genuine
    # reject token is never laundered by the group strip.
    assert (
        _strip_release_group("Movie.2024.DVDSCR.x264-SCR", {"release_group": "SCR"})
        == "Movie.2024.DVDSCR.x264-"
    )
    # No whole-token occurrence -> nothing is stripped (the group name only
    # appears as a substring of a larger word).
    assert (
        _strip_release_group("Scream.2024.1080p.WEB-DL.x264", {"release_group": "SCR"})
        == "Scream.2024.1080p.WEB-DL.x264"
    )


def test_strip_release_group_removes_every_attached_occurrence() -> None:
    # A duplicated folder/filename relative path where the group name appears as a
    # hyphen-attached tag on BOTH segments: every attached occurrence is stripped,
    # so nothing is left behind for the reject net to false-trip on. A suffix-only
    # strip would leave the folder's ``-SCR`` and wrongly reject the clean import.
    # Only the ``SCR`` token is removed; the ``-`` separator is not part of it.
    assert (
        _strip_release_group(
            "Movie.2024.1080p.WEB-DL.x264-SCR/Movie.2024.1080p.WEB-DL.x264-SCR.mkv",
            {"release_group": "SCR"},
        )
        == "Movie.2024.1080p.WEB-DL.x264-/Movie.2024.1080p.WEB-DL.x264-.mkv"
    )
    # Embedded mentions on both segments survive (``DVDSCR`` is not attached),
    # so a genuine reject token is never laundered by the strip.
    assert (
        _strip_release_group(
            "Movie.2024.DVDSCR.x264-SCR/Movie.2024.DVDSCR.x264-SCR.mkv",
            {"release_group": "SCR"},
        )
        == "Movie.2024.DVDSCR.x264-/Movie.2024.DVDSCR.x264-.mkv"
    )


def test_strip_release_group_only_removes_attached_tags() -> None:
    # A DOT-attached body token that collides with the group name is NOT a group
    # tag and must survive the strip: ``HC.SCR`` is a genuine hardcoded-screener
    # marker; only the hyphen-attached ``-SCR`` suffix is the group tag. A strip
    # of every token-bounded occurrence (regardless of attachment) would delete
    # both and launder the screener into an acceptable WEBDL.
    assert (
        _strip_release_group("Movie.2024.1080p.HC.SCR.WEB-DL.x264-SCR", {"release_group": "SCR"})
        == "Movie.2024.1080p.HC.SCR.WEB-DL.x264-"
    )
    assert (
        _strip_release_group("Movie.2024.1080p.HC-SCR.WEB-DL.x264-SCR", {"release_group": "SCR"})
        == "Movie.2024.1080p.HC-SCR.WEB-DL.x264-"
    )
    # Bracket attachment (the anime/p2p ``[GROUP]`` convention Radarr's
    # ReleaseGroupParser also recognizes) is stripped like the hyphen form, so a
    # bracketed group named after a reject token cannot false-trip the net.
    assert (
        _strip_release_group("[SCR] Movie 2024 1080p WEB-DL x264", {"release_group": "SCR"})
        == "[] Movie 2024 1080p WEB-DL x264"
    )


def test_strip_release_group_leaves_guessit_native_other_intact() -> None:
    # The guessit-native `other` path is untouched by the strip.
    assert (
        map_modifier({"other": "Screener", "release_group": "GRP"}, "Movie.DVDSCR-GRP")
        is Modifier.SCREENER
    )


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
