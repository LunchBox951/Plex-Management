"""Quality taxonomy — the borrowed-brains quality model (ADR-0001).

Ported verbatim from Radarr's ``Qualities/Quality.cs`` (``DefaultQualityDefinitions``),
``Qualities/QualitySource.cs``, and ``Qualities/Modifier.cs``. The integer
``weight`` values come straight from Radarr's ``DefaultQualityDefinitions`` and
define the canonical ordering used by the quality profile to gate releases — they
are never invented here.

This module is pure domain: no I/O, no adapter or third-party-parser imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

__all__ = [
    "ALL_QUALITIES",
    "QUALITY_BY_ID",
    "Modifier",
    "Quality",
    "QualitySource",
    "Resolution",
]


class QualitySource(IntEnum):
    """Release source tier, exactly mirroring Radarr's ``QualitySource`` enum.

    Resolution (not source) distinguishes SDTV/HDTV — ``TV`` covers both.
    """

    UNKNOWN = 0
    CAM = 1
    TELESYNC = 2
    TELECINE = 3
    WORKPRINT = 4
    DVD = 5
    TV = 6
    WEBDL = 7
    WEBRIP = 8
    BLURAY = 9


class Resolution(IntEnum):
    """Vertical resolution in pixels, mirroring Radarr's ``Resolution`` enum.

    The integer values are the resolutions themselves so comparisons are
    meaningful. ``UNKNOWN`` is 0.
    """

    UNKNOWN = 0
    R360P = 360
    R480P = 480
    R540P = 540
    R576P = 576
    R720P = 720
    R1080P = 1080
    R2160P = 2160


class Modifier(IntEnum):
    """Quality modifier, mirroring Radarr's ``Modifier`` enum (same ordinals)."""

    NONE = 0
    REGIONAL = 1
    SCREENER = 2
    RAWHD = 3
    BRDISK = 4
    REMUX = 5


@dataclass(frozen=True)
class Quality:
    """A single named quality (e.g. ``WEBDL-1080p``).

    ``weight`` is the canonical ordering key from Radarr's
    ``DefaultQualityDefinitions`` — higher means better. ``id`` matches Radarr's
    static quality id so persisted profiles stay stable across the port.
    """

    id: int
    name: str
    source: QualitySource
    resolution: Resolution
    modifier: Modifier
    weight: int


# -- Named quality registry (ids + weights ported from Radarr Quality.cs) -------
# Pre-release / unwanted tiers
UNKNOWN = Quality(0, "Unknown", QualitySource.UNKNOWN, Resolution.UNKNOWN, Modifier.NONE, 1)
WORKPRINT = Quality(24, "WORKPRINT", QualitySource.WORKPRINT, Resolution.UNKNOWN, Modifier.NONE, 2)
CAM = Quality(25, "CAM", QualitySource.CAM, Resolution.UNKNOWN, Modifier.NONE, 3)
TELESYNC = Quality(26, "TELESYNC", QualitySource.TELESYNC, Resolution.UNKNOWN, Modifier.NONE, 4)
TELECINE = Quality(27, "TELECINE", QualitySource.TELECINE, Resolution.UNKNOWN, Modifier.NONE, 5)
REGIONAL = Quality(29, "REGIONAL", QualitySource.DVD, Resolution.R480P, Modifier.REGIONAL, 6)
DVDSCR = Quality(28, "DVDSCR", QualitySource.DVD, Resolution.R480P, Modifier.SCREENER, 7)

# SD
SDTV = Quality(1, "SDTV", QualitySource.TV, Resolution.R480P, Modifier.NONE, 8)
DVD = Quality(2, "DVD", QualitySource.DVD, Resolution.UNKNOWN, Modifier.NONE, 9)
DVDR = Quality(23, "DVD-R", QualitySource.DVD, Resolution.R480P, Modifier.REMUX, 10)

# WEB 480p (grouped weight)
WEBDL480P = Quality(8, "WEBDL-480p", QualitySource.WEBDL, Resolution.R480P, Modifier.NONE, 11)
WEBRIP480P = Quality(12, "WEBRip-480p", QualitySource.WEBRIP, Resolution.R480P, Modifier.NONE, 11)

# Bluray SD
BLURAY480P = Quality(20, "Bluray-480p", QualitySource.BLURAY, Resolution.R480P, Modifier.NONE, 12)
BLURAY576P = Quality(21, "Bluray-576p", QualitySource.BLURAY, Resolution.R576P, Modifier.NONE, 13)

# 720p
HDTV720P = Quality(4, "HDTV-720p", QualitySource.TV, Resolution.R720P, Modifier.NONE, 14)
WEBDL720P = Quality(5, "WEBDL-720p", QualitySource.WEBDL, Resolution.R720P, Modifier.NONE, 15)
WEBRIP720P = Quality(14, "WEBRip-720p", QualitySource.WEBRIP, Resolution.R720P, Modifier.NONE, 15)
BLURAY720P = Quality(6, "Bluray-720p", QualitySource.BLURAY, Resolution.R720P, Modifier.NONE, 16)

# 1080p
HDTV1080P = Quality(9, "HDTV-1080p", QualitySource.TV, Resolution.R1080P, Modifier.NONE, 17)
WEBDL1080P = Quality(3, "WEBDL-1080p", QualitySource.WEBDL, Resolution.R1080P, Modifier.NONE, 18)
WEBRIP1080P = Quality(
    15, "WEBRip-1080p", QualitySource.WEBRIP, Resolution.R1080P, Modifier.NONE, 18
)
BLURAY1080P = Quality(7, "Bluray-1080p", QualitySource.BLURAY, Resolution.R1080P, Modifier.NONE, 19)
REMUX1080P = Quality(30, "Remux-1080p", QualitySource.BLURAY, Resolution.R1080P, Modifier.REMUX, 20)

# 2160p
HDTV2160P = Quality(16, "HDTV-2160p", QualitySource.TV, Resolution.R2160P, Modifier.NONE, 21)
WEBDL2160P = Quality(18, "WEBDL-2160p", QualitySource.WEBDL, Resolution.R2160P, Modifier.NONE, 22)
WEBRIP2160P = Quality(
    17, "WEBRip-2160p", QualitySource.WEBRIP, Resolution.R2160P, Modifier.NONE, 22
)
BLURAY2160P = Quality(
    19, "Bluray-2160p", QualitySource.BLURAY, Resolution.R2160P, Modifier.NONE, 23
)
REMUX2160P = Quality(31, "Remux-2160p", QualitySource.BLURAY, Resolution.R2160P, Modifier.REMUX, 24)

# Disc / raw (heavy, ungated-by-size in Radarr)
BRDISK = Quality(22, "BR-DISK", QualitySource.BLURAY, Resolution.R1080P, Modifier.BRDISK, 25)
RAWHD = Quality(10, "Raw-HD", QualitySource.TV, Resolution.R1080P, Modifier.RAWHD, 26)


# Ordered low -> high by weight. Radarr groups WEBDL-Np and WEBRip-Np at one
# shared weight (they are *equal*). Flattening that group into two adjacent
# profile items would force a tie-break by list position, so we deliberately
# place WEBRip BEFORE WEBDL within each equal-weight WEB group: WEBDL then gets
# the higher profile index and wins the conventional "cleaner direct source"
# preference (and the WEBDL-1080p cutoff treats WEBRip-1080p as *below* it, so
# WEBRip -> WEBDL upgrades are still possible). Only same-weight items move;
# ids/weights are untouched.
ALL_QUALITIES: tuple[Quality, ...] = (
    UNKNOWN,
    WORKPRINT,
    CAM,
    TELESYNC,
    TELECINE,
    REGIONAL,
    DVDSCR,
    SDTV,
    DVD,
    DVDR,
    WEBRIP480P,
    WEBDL480P,
    BLURAY480P,
    BLURAY576P,
    HDTV720P,
    WEBRIP720P,
    WEBDL720P,
    BLURAY720P,
    HDTV1080P,
    WEBRIP1080P,
    WEBDL1080P,
    BLURAY1080P,
    REMUX1080P,
    HDTV2160P,
    WEBRIP2160P,
    WEBDL2160P,
    BLURAY2160P,
    REMUX2160P,
    BRDISK,
    RAWHD,
)

QUALITY_BY_ID: dict[int, Quality] = {q.id: q for q in ALL_QUALITIES}
