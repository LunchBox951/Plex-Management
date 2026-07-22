"""End-to-end golden test: real guessit -> domain mapping -> quality gate.

This is the load-bearing proof that the CAM/TS hard cutoff survives the round
trip through the *actual* third-party parser (not a fake). For each name we run
``GuessitParser.parse`` (real guessit 4.0.2), resolve the named quality from the
parsed source/resolution/modifier, and assert the default profile's verdict.

Reject names (prototype leak list) MUST come back un-acceptable; good names MUST
be accepted. If a future guessit upgrade changes a classification, this test
fails loudly rather than silently re-admitting a CAM.
"""

from __future__ import annotations

import pytest

from plex_manager.adapters.parser import GuessitParser
from plex_manager.domain.quality import QualitySource
from plex_manager.domain.quality_profile import default_profile
from plex_manager.domain.quality_service import check_quality
from plex_manager.domain.season_pack import classify_release_scope
from plex_manager.domain.source_mapping import resolve_quality

# Prototype leak names — every one is a cam/pre-release/screener/regional rip and
# MUST be rejected by the default profile.
REJECT_NAMES = [
    "The Movie 2023 TELESYNC x264-GROUP",
    "The Movie 2023 1080p HDTS x264-GROUP",
    "The Movie 2023 CAMRip XviD-GROUP",
    "The Movie 2023 HQCAM x264-GROUP",
    "The Movie 2023 HDCAM x264-GROUP",
    "The Movie 2023 WORKPRINT x264-GROUP",
    "The Movie 2023 DVDSCR x264-GROUP",
    "The Movie 2023 R5 DVDRip XviD-GROUP",
    "The Movie 2023 R6 DVDRip XviD-GROUP",
]

# Clean releases that the default profile must accept.
GOOD_NAMES = [
    "The Movie 2023 1080p WEB-DL DD5.1 H264-GROUP",
    "The Movie 2023 2160p BluRay REMUX HEVC DTS-HD MA 5.1-GROUP",
]


@pytest.mark.parametrize("name", REJECT_NAMES)
def test_leak_names_are_rejected_end_to_end(name: str) -> None:
    parsed = GuessitParser().parse(name)
    quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
    verdict = check_quality(quality, default_profile())
    assert not verdict.accepted, f"{name!r} -> {quality.name} wrongly accepted"


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_good_names_are_accepted_end_to_end(name: str) -> None:
    parsed = GuessitParser().parse(name)
    quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
    verdict = check_quality(quality, default_profile())
    assert verdict.accepted, f"{name!r} -> {quality.name} wrongly rejected"


def test_telesync_maps_to_telesync_source() -> None:
    parsed = GuessitParser().parse("The Movie 2023 1080p HDTS x264-GROUP")
    # Source wins over the 1080p resolution: this is still TELESYNC.
    assert parsed.source is QualitySource.TELESYNC


def test_webdl_parses_to_allowed_source_and_resolution() -> None:
    parsed = GuessitParser().parse("The Movie 2023 1080p WEB-DL DD5.1 H264-GROUP")
    assert parsed.source is QualitySource.WEBDL
    assert parsed.clean_title == "The Movie"
    assert parsed.year == 2023


def test_suits_whole_show_pack_parses_as_all_nine_seasons() -> None:
    # Issue #409 anchor: the reported Suits whole-show pack must parse as the full
    # season span 1-9 with NO episode token, so it classifies as a multi_season_pack
    # -- the premise the in-flight-overlap rejection is built on. If a guessit
    # upgrade narrowed this to a single season, the dedup guard would silently stop
    # firing, so this test pins the real parser's behaviour.
    parsed = GuessitParser().parse("Suits.S01-S09.COMPLETE.1080p.WEB-DL.x264-GROUP")
    assert parsed.season == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert parsed.episode is None
    assert classify_release_scope(parsed) == "multi_season_pack"


def test_parse_never_raises_on_garbage() -> None:
    parsed = GuessitParser().parse("")
    assert parsed.source is QualitySource.UNKNOWN


def test_parse_never_raises_when_guessit_itself_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #86: the port contract says ``parse()`` never raises, but the
    adapter used to call ``guessit()`` bare -- any internal guessit exception on
    a pathological release name would abort preview/grab/import instead of
    degrading to an ``UNKNOWN`` parsed release. A monkeypatched ``guessit`` that
    raises proves the adapter's own try/except, not guessit's actual behavior on
    any particular string (guessit itself is well-behaved on ordinary input)."""
    import plex_manager.adapters.parser.guessit_adapter as guessit_adapter_module

    def _boom(_release_name: str) -> dict[str, object]:
        raise RuntimeError("guessit internal parser error")

    monkeypatch.setattr(guessit_adapter_module, "guessit", _boom)

    parsed = GuessitParser().parse("The Movie 2023 1080p WEB-DL DD5.1 H264-GROUP")

    assert parsed.source is QualitySource.UNKNOWN
    # The raw title survives even though guessit never got to classify it -- the
    # degraded parse is still identifiable, not a blank record.
    assert parsed.raw_title == "The Movie 2023 1080p WEB-DL DD5.1 H264-GROUP"


def test_parser_satisfies_port_protocol() -> None:
    from plex_manager.ports.parser import ParserPort

    assert isinstance(GuessitParser(), ParserPort)
