"""Import-validation gate — re-run the decision brain on a *completed* download.

The prototype's worst failure was structural: it trusted whatever a download
client reported as "done" and imported the largest file blind, so a torrent that
swapped in a CAM, named a different movie, or shipped only a 30 MB sample landed
in the library. This module closes that hole by reusing the SAME pure brain the
grab path uses (:func:`plex_manager.domain.media_match.matches_media` +
:func:`plex_manager.domain.source_mapping.resolve_quality` +
:func:`plex_manager.domain.quality_service.check_quality`) against the on-disk file
*name* before anything is hardlinked into place.

It mirrors :func:`plex_manager.services.decision_service.preview`'s movie path: the
file carries no authoritative ``tmdb_id`` of its own, so the identity test falls
back to the conservative normalized-title + year comparison and REJECTS when
uncertain rather than risk importing the wrong media.

Honesty over silence: every failure mode is a typed, surfaced
:class:`ImportRejection`, never a swallowed default. All checks run and ALL
reasons are collected so the operator sees the full picture (a file can be both
wrong-media *and* a sample). An indeterminate file (unknown size, no video) is a
REJECT, never an optimistic accept — the Radarr posture.

The quality gate keys on PROFILE-ALLOWED, not equal-to-what-was-grabbed: a benign
source drift (the indexer advertised WEB-DL, the finished file parses as WEBRip)
is still acceptable when the profile allows it, so honest variance does not block
an otherwise-correct import.

Pure domain: stdlib + the local decision modules only. The download-client file
list is mapped onto :class:`VideoFile` by the import *service*; this module never
imports a port or adapter, so it stays trivially testable and I/O-free.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from plex_manager.domain.media_match import matches_media
from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.quality_service import check_quality
from plex_manager.domain.release import ParsedRelease
from plex_manager.domain.source_mapping import resolve_quality
from plex_manager.ports.parser import ParserPort

__all__ = [
    "VIDEO_EXTENSIONS",
    "ImportRejection",
    "ImportRejectionReason",
    "ImportValidation",
    "VideoFile",
    "validate_import",
]

# Container extensions we treat as the feature video. Lower-case, leading dot.
# Disk-image / stream containers (``.iso``, ``.ts``, ``.m2ts``) are intentionally
# excluded: they are the multi-part / raw-disk shapes the gate is meant to catch,
# not a clean importable file.
VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".webm", ".flv"}
)

# Files whose *name* marks them as a sample/extra rather than the feature. These
# are dropped from consideration up front so a 40 MB "sample.mkv" can never be
# chosen as the largest-file feature when the real movie is also present.
_SAMPLE_EXTRAS = re.compile(
    r"(?:^|[\s._\-])(?:sample|trailer|extras?|featurette|behind[\s._\-]?the[\s._\-]?scenes|"
    r"deleted[\s._\-]?scene|proof|rarbg\.com)(?:$|[\s._\-])",
    re.IGNORECASE,
)

# Multi-part / split-disk markers (``CD1``, ``part2``, ``disc 3``). A completed
# download whose feature file is one slice of a set cannot be imported as a single
# movie, so it is surfaced rather than half-imported.
_MULTI_PART = re.compile(
    r"(?:^|[\s._\-])(?:cd|dvd|disc|disk|part|pt)[\s._\-]?\d{1,2}(?:$|[\s._\-])",
    re.IGNORECASE,
)

# Absolute floor below which a "video" file is almost certainly a sample, a decoy,
# or a truncated/failed download. 50 MiB is comfortably below any real feature
# encode yet above typical sample clips. A file of unknown size (0) is treated the
# same way — indeterminate means reject, never an optimistic import.
_SAMPLE_FLOOR_BYTES = 50 * 1024 * 1024


class ImportRejectionReason(StrEnum):
    """Why a completed download was refused import. Surfaced, never swallowed."""

    WRONG_MEDIA = "wrong_media"
    QUALITY_NOT_WANTED = "quality_not_wanted"
    SAMPLE = "sample"
    NO_VIDEO_FILE = "no_video_file"
    MULTI_PART = "multi_part"


@dataclass(frozen=True)
class VideoFile:
    """One file from a completed download, as the import service maps it.

    ``relative_path`` is the path within the download root (POSIX or Windows
    separators tolerated); ``size_bytes`` is the on-disk size, ``0`` when the
    download client did not report it (treated as indeterminate -> reject).
    """

    relative_path: str
    size_bytes: int


@dataclass(frozen=True)
class ImportRejection:
    """A single surfaced reason an import was refused, with operator-facing detail."""

    reason: ImportRejectionReason
    detail: str


@dataclass(frozen=True)
class ImportValidation:
    """Outcome of validating a completed download against the decision brain.

    ``accepted`` is True iff ``rejections`` is empty. ``video`` is the chosen
    feature file (the largest non-sample video), ``None`` when none was found;
    ``parsed`` is its parse, ``None`` when there was nothing to parse.
    """

    accepted: bool
    video: VideoFile | None
    parsed: ParsedRelease | None
    rejections: tuple[ImportRejection, ...]


def _basename(relative_path: str) -> str:
    """Return the final path component, tolerating ``/`` or ``\\`` separators."""
    return relative_path.replace("\\", "/").rsplit("/", 1)[-1]


def _extension(name: str) -> str:
    """Return the lower-cased extension (``".mkv"``) or ``""`` when there is none."""
    dot = name.rfind(".")
    if dot <= 0:  # no dot, or a leading-dot dotfile with no real extension
        return ""
    return name[dot:].lower()


def _is_video(name: str) -> bool:
    return _extension(name) in VIDEO_EXTENSIONS


def _looks_like_sample_name(name: str) -> bool:
    return _SAMPLE_EXTRAS.search(name) is not None


def validate_import(
    files: Sequence[VideoFile],
    *,
    parser: ParserPort,
    profile: QualityProfile,
    expected_title: str,
    expected_year: int | None,
    expected_tmdb_id: int,
) -> ImportValidation:
    """Validate a completed download's feature file against the decision brain.

    Steps (movies-first):

    1. Keep only video-extension files and drop names that mark a sample/extra; if
       nothing survives, reject :attr:`~ImportRejectionReason.NO_VIDEO_FILE`.
    2. Choose the largest survivor by ``size_bytes`` as the feature.
    3. Parse its basename and run the SAME identity gate the grab path uses
       (:func:`matches_media`, title+year fallback because a file carries no
       ``tmdb_id``); a mismatch is :attr:`~ImportRejectionReason.WRONG_MEDIA`.
    4. Resolve the quality and gate it against the profile; a disallowed/hard-cutoff
       result (CAM/TS/...) is :attr:`~ImportRejectionReason.QUALITY_NOT_WANTED`. The
       gate keys on PROFILE-ALLOWED, so benign source drift still passes.
    5. A file below the absolute sample floor (or of unknown size) is
       :attr:`~ImportRejectionReason.SAMPLE`.
    6. A multi-part / split-disk marker in the path is
       :attr:`~ImportRejectionReason.MULTI_PART`.

    ALL applicable rejections are collected; ``accepted`` is True iff there are
    none. An indeterminate file is always a reject, never an optimistic import.
    """
    videos = [
        video
        for video in files
        if _is_video(_basename(video.relative_path))
        and not _looks_like_sample_name(_basename(video.relative_path))
    ]
    if not videos:
        return ImportValidation(
            accepted=False,
            video=None,
            parsed=None,
            rejections=(
                ImportRejection(
                    reason=ImportRejectionReason.NO_VIDEO_FILE,
                    detail=(
                        "no importable video file in completed download "
                        f"({len(files)} file(s) inspected)"
                    ),
                ),
            ),
        )

    chosen = max(videos, key=lambda video: video.size_bytes)
    name = _basename(chosen.relative_path)
    parsed = parser.parse(name)

    rejections: list[ImportRejection] = []

    # 3. Media identity — mirror decision_service.preview's movie path. A file has
    #    no authoritative tmdb id of its own, so this falls to the conservative
    #    normalized-title + year comparison and rejects when uncertain.
    if not matches_media(
        parsed,
        expected_title=expected_title,
        expected_year=expected_year,
        candidate_tmdb_id=0,
        expected_tmdb_id=expected_tmdb_id,
    ):
        rejections.append(
            ImportRejection(
                reason=ImportRejectionReason.WRONG_MEDIA,
                detail=(
                    f"file {name!r} parsed as {parsed.clean_title!r} "
                    f"(year={parsed.year}); expected {expected_title!r} "
                    f"(year={expected_year})"
                ),
            )
        )

    # 4. Quality hard gate — key on PROFILE-ALLOWED, not equal-to-grabbed-quality.
    quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
    verdict = check_quality(quality, profile)
    if not verdict.accepted:
        rejections.append(
            ImportRejection(
                reason=ImportRejectionReason.QUALITY_NOT_WANTED,
                detail=(
                    f"file {name!r} resolved to quality {quality.name!r}, "
                    "which the profile does not allow"
                ),
            )
        )

    # 5. Sample / indeterminate-size floor.
    if chosen.size_bytes < _SAMPLE_FLOOR_BYTES:
        rejections.append(
            ImportRejection(
                reason=ImportRejectionReason.SAMPLE,
                detail=(
                    f"file {name!r} is {chosen.size_bytes} bytes, below the "
                    f"{_SAMPLE_FLOOR_BYTES}-byte sample floor (unknown size counts as sample)"
                ),
            )
        )

    # 6. Multi-part / split-disk shape.
    if _MULTI_PART.search(chosen.relative_path):
        rejections.append(
            ImportRejection(
                reason=ImportRejectionReason.MULTI_PART,
                detail=f"path {chosen.relative_path!r} carries a multi-part / split-disk marker",
            )
        )

    return ImportValidation(
        accepted=not rejections,
        video=chosen,
        parsed=parsed,
        rejections=tuple(rejections),
    )
