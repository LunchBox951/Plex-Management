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
from plex_manager.domain.season_pack import episode_numbers as _episode_numbers
from plex_manager.domain.source_mapping import resolve_quality
from plex_manager.ports.filesystem import VIDEO_EXTENSIONS
from plex_manager.ports.parser import ParserPort

__all__ = [
    "VIDEO_EXTENSIONS",
    "EpisodeImportRejection",
    "EpisodeImportResult",
    "ImportRejection",
    "ImportRejectionReason",
    "ImportValidation",
    "SeasonImportValidation",
    "VideoFile",
    "validate_import",
    "validate_season_import",
]

# NB: ``VIDEO_EXTENSIONS`` (imported above, re-exported via ``__all__``) is the
# SAME set ``FileSystemPort.largest_video_file`` uses to SELECT the source file, so
# the selector and this validator can never disagree — a file picked as the source
# but then rejected here as "no video" was a real beta bug.

# Files whose *name* marks them as a sample/extra rather than the feature. These
# are dropped from consideration up front so a 40 MB "sample.mkv" can never be
# chosen as the largest-file feature when the real movie is also present.
_SAMPLE_EXTRAS = re.compile(
    r"(?:^|[\s._\-])(?:sample|trailer|extras?|featurette|behind[\s._\-]?the[\s._\-]?scenes|"
    r"deleted[\s._\-]?scene|proof|rarbg\.com)(?:$|[\s._\-])",
    re.IGNORECASE,
)

# Multi-part / split-disk markers. A completed download whose feature file is one
# slice of a set cannot be imported as a single movie, so it is surfaced rather
# than half-imported. Path separators (/ and \) ARE token boundaries: a split-disk
# release stores each part under a ``CD1/`` / ``Disc 1/`` directory, so the marker
# is bounded by a slash, not just a space/dot/dash. ``cd``/``dvd``/``disc``/``disk``
# + a number are NEVER part of a real movie title, so a match is decisive.
_DISK_MARKER = re.compile(
    r"(?:^|[\s._\-/\\])(?:cd|dvd|disc|disk)[\s._\-]?\d{1,2}(?:$|[\s._\-/\\])",
    re.IGNORECASE,
)

# ``part``/``pt`` + a number are AMBIGUOUS: a 2-CD split names its slices
# ``Movie.Part1``/``Movie.Part2``, but a real title can also carry one
# (``...Deathly Hallows Part 1``). The captured number lets :func:`_is_multi_part`
# tell them apart — a part marker is a split only when the *expected* (canonical
# TMDB) title does not itself carry that same part number.
_PART_MARKER = re.compile(
    r"(?:^|[\s._\-/\\])(?:part|pt)[\s._\-]?(\d{1,2})(?:$|[\s._\-/\\])",
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
    # TV-only: the file parsed with no episode number at all (e.g. a stray extra
    # that slipped past the sample-name filter), so it cannot be placed as a named
    # episode. Never applies to movies (there is no episode concept to gate on).
    NO_EPISODE_NUMBER = "no_episode_number"


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


@dataclass(frozen=True)
class EpisodeImportResult:
    """One episode file from a season download that validated cleanly.

    Used both for genuinely ``accepted`` files and for the benign
    ``skipped_not_requested`` bucket (same shape — a skip is not a validation
    failure). ``episodes`` is the sorted, non-empty tuple of episode numbers the
    file names (more than one entry for a multi-episode file).
    """

    video: VideoFile
    parsed: ParsedRelease
    episodes: tuple[int, ...]


@dataclass(frozen=True)
class EpisodeImportRejection:
    """A single surfaced reason one file in a season download was refused import.

    Unlike :class:`ImportValidation` (one feature file, all reasons pooled), a
    season download validates every file independently, so a file with more than
    one applicable reason gets one :class:`EpisodeImportRejection` per reason,
    all sharing ``relative_path`` — still honesty over silence, per file.
    """

    relative_path: str
    reason: ImportRejectionReason
    detail: str


@dataclass(frozen=True)
class SeasonImportValidation:
    """Outcome of validating EVERY file in a completed season download.

    Unlike :class:`ImportValidation` (movies-first, single feature file,
    all-or-nothing), a season import legitimately PARTIALLY succeeds:
    ``accepted`` can be non-empty even when ``rejected`` is too — the caller
    imports whatever accepted and surfaces the rest. ``skipped_not_requested`` is
    a distinct, BENIGN bucket (never a rejection) for episodes that validated
    cleanly but were filtered out by an operator-supplied ``requested_episodes``.
    """

    accepted: tuple[EpisodeImportResult, ...]
    rejected: tuple[EpisodeImportRejection, ...]
    skipped_not_requested: tuple[EpisodeImportResult, ...]


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


def _is_multi_part(relative_path: str, expected_title: str) -> bool:
    """Return ``True`` when the chosen file is one slice of a split-disk/multi-part set.

    A ``cd``/``dvd``/``disc``/``disk`` marker is decisive — those tokens are never
    part of a real movie title. A ``part``/``pt`` marker is ambiguous, so it counts
    as a split ONLY when ``expected_title`` (the canonical TMDB title) does not
    itself carry that same part number: a movie genuinely titled ``...Part 1``
    imports, while a 2-CD ``Movie.Part1`` split whose title has no part number is
    still surfaced. Numbers compare as integers so ``Part.01`` and ``Part 1`` match.
    """
    if _DISK_MARKER.search(relative_path):
        return True
    title_part_numbers = {int(match.group(1)) for match in _PART_MARKER.finditer(expected_title)}
    return any(
        int(match.group(1)) not in title_part_numbers
        for match in _PART_MARKER.finditer(relative_path)
    )


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
    6. A split-disk (cd/dvd/disc/disk + number) or genuine multi-part marker in
       the path is :attr:`~ImportRejectionReason.MULTI_PART`. A ``part``/``pt``
       marker whose number the expected title itself carries (a movie genuinely
       titled ``...Part 1``) is NOT a split and does not reject.

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
    # Parse the FULL relative path, not just the basename: a release that ships a
    # generic feature file (``movie.mkv``) under a token-rich folder
    # (``The.Matrix.1999.1080p.WEB-DL/``) carries its identity + quality tokens in
    # the FOLDER. The borrowed parser reads every path component (folder name
    # included), so a folder-qualified parse recovers them; a path that is generic
    # at every level still parses to an unknown title and is honestly rejected. The
    # human-facing ``name`` stays the basename for readable rejection details.
    parsed = parser.parse(chosen.relative_path.replace("\\", "/"))

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

    # 6. Multi-part / split-disk shape. A cd/dvd/disc/disk marker is decisive; a
    #    part/pt marker is a split only when the expected title does not itself
    #    carry that same part number, so a movie titled "...Part 1" still imports.
    if _is_multi_part(chosen.relative_path, expected_title):
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


def validate_season_import(
    files: Sequence[VideoFile],
    *,
    parser: ParserPort,
    profile: QualityProfile,
    expected_title: str,
    expected_tmdb_id: int,
    expected_season: int,
    requested_episodes: Sequence[int] | None = None,
) -> SeasonImportValidation:
    """Validate EVERY file in a completed TV season download; partial success is legit.

    Unlike :func:`validate_import` (movies-first: pick ONE largest file, all gates
    apply to it alone), a season download legitimately ships many independently
    valid episode files, so each video-extension, non-sample file is parsed and
    gated on its own. The caller imports whatever :attr:`SeasonImportValidation.accepted`
    holds and surfaces :attr:`~SeasonImportValidation.rejected` for the rest — there
    is no single pass/fail verdict for the whole download.

    Per candidate file:

    1. Non-video-extension names and sample/extra-named files
       (:func:`_looks_like_sample_name`) are dropped up front, silently — same as
       :func:`validate_import`; they were never episode candidates.
    2. Parse the FULL folder-qualified relative path — a season-pack folder
       routinely carries the season/quality tokens an individual episode filename
       omits (mirrors :func:`validate_import`'s folder-qualified parse).
    3. Media identity, gated on the expected SEASON as well as title
       (:func:`matches_media` with ``expected_season=``, ``expected_year=None``
       because a per-episode release name legitimately omits the show's first-air
       year — same posture as ``decision_service.preview``'s TV path). A file for
       the wrong show, or the right show's WRONG season (a mislabeled or
       bonus-season file inside the pack), is
       :attr:`~ImportRejectionReason.WRONG_MEDIA`.
    4. Quality hard gate, identical to :func:`validate_import` (PROFILE-ALLOWED,
       not equal-to-grabbed).
    5. Sample / indeterminate-size floor, identical to :func:`validate_import`.
    6. Multi-part / split-disk shape, identical to :func:`validate_import`
       (:func:`_is_multi_part`): a split TV episode (``S02E01.CD1``/``Disc 1``)
       still parses with a valid episode number, so without this gate it would
       otherwise reach an ``accepted`` result and the caller's duplicate-
       destination handling would keep only the largest chunk, completing the
       season with an incomplete episode file. Rejected
       :attr:`~ImportRejectionReason.MULTI_PART`, same as a movie split.
    7. Episode-number gate — a file that parses with NO episode number at all
       cannot be placed as a named episode:
       :attr:`~ImportRejectionReason.NO_EPISODE_NUMBER`.

    ALL applicable reasons (3-7) are collected for a file — one
    :class:`EpisodeImportRejection` per reason, never just the first.

    When ``requested_episodes`` is given (the operator asked for specific
    episodes, not the whole season) a file that validated cleanly but whose
    episode(s) share NO overlap with the requested set is moved to the BENIGN
    ``skipped_not_requested`` bucket instead of ``accepted`` — not a rejection. A
    multi-episode file that overlaps even partially is kept in ``accepted`` in
    full; its other, unrequested episode(s) ride along (a file cannot be split).
    """
    requested = set(requested_episodes) if requested_episodes else None

    accepted: list[EpisodeImportResult] = []
    rejected: list[EpisodeImportRejection] = []
    skipped_not_requested: list[EpisodeImportResult] = []

    for video in files:
        name = _basename(video.relative_path)
        if not _is_video(name) or _looks_like_sample_name(name):
            continue

        # Parse the FULL relative path (folder included) — see step 2 above.
        parsed = parser.parse(video.relative_path.replace("\\", "/"))
        reasons: list[tuple[ImportRejectionReason, str]] = []

        # 3. Media identity, gated on title + expected season (no year: see
        #    docstring). A wrong-season file inside an otherwise-correct pack is
        #    WRONG_MEDIA, same as a wrong-show file.
        if not matches_media(
            parsed,
            expected_title=expected_title,
            expected_year=None,
            candidate_tmdb_id=0,
            expected_tmdb_id=expected_tmdb_id,
            expected_season=expected_season,
        ):
            reasons.append(
                (
                    ImportRejectionReason.WRONG_MEDIA,
                    f"file {name!r} parsed as {parsed.clean_title!r} "
                    f"(season={parsed.season!r}); expected {expected_title!r} "
                    f"season {expected_season}",
                )
            )

        # 3b. Ambiguous multi-season pack guard (placement precision). Step 3 parses
        #     the FULL path so a season carried only by the folder is still seen, but
        #     a MULTI-season folder (``S01-S03``) yields a season LIST that
        #     ``_season_covers`` treats as covering the requested season -- so a file
        #     whose OWN name says S01E01 would clear the season gate for a requested
        #     S02 and then be PLACED under S02 as S02E01 (mis-routed, silently
        #     completing the wrong season with the wrong file). When the parse is
        #     multi-season, fall back to the file's OWN season (basename alone) and
        #     require it to equal the requested season; an own-season that differs OR
        #     cannot be determined is ambiguous -> WRONG_MEDIA, never placed. (A
        #     single-season or folder-only ``int`` season is unambiguous and skips
        #     this; season packs are still grab-selectable -- only per-file PLACEMENT
        #     is tightened here.)
        if isinstance(parsed.season, list):
            own_season = parser.parse(name).season
            if own_season != expected_season:
                reasons.append(
                    (
                        ImportRejectionReason.WRONG_MEDIA,
                        f"file {name!r} sits in a multi-season pack (parsed seasons "
                        f"{parsed.season!r}); its own season {own_season!r} does not "
                        f"unambiguously match requested season {expected_season}",
                    )
                )

        # 4. Quality hard gate — key on PROFILE-ALLOWED, not equal-to-grabbed.
        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)
        verdict = check_quality(quality, profile)
        if not verdict.accepted:
            reasons.append(
                (
                    ImportRejectionReason.QUALITY_NOT_WANTED,
                    f"file {name!r} resolved to quality {quality.name!r}, "
                    "which the profile does not allow",
                )
            )

        # 5. Sample / indeterminate-size floor.
        if video.size_bytes < _SAMPLE_FLOOR_BYTES:
            reasons.append(
                (
                    ImportRejectionReason.SAMPLE,
                    f"file {name!r} is {video.size_bytes} bytes, below the "
                    f"{_SAMPLE_FLOOR_BYTES}-byte sample floor (unknown size counts as sample)",
                )
            )

        # 6. Multi-part / split-disk shape — a split TV chunk still parses with a
        #    valid episode number, so without this gate it would slip through as
        #    a legitimate accepted episode and the duplicate-destination logic
        #    would keep only the largest chunk, completing the season with an
        #    incomplete episode file. Identical rule to validate_import's step 6.
        if _is_multi_part(video.relative_path, expected_title):
            reasons.append(
                (
                    ImportRejectionReason.MULTI_PART,
                    f"path {video.relative_path!r} carries a multi-part / split-disk marker",
                )
            )

        # 7. Episode-number gate (TV-only).
        episodes = _episode_numbers(parsed.episode)
        if not episodes:
            reasons.append(
                (
                    ImportRejectionReason.NO_EPISODE_NUMBER,
                    f"file {name!r} carries no parseable episode number",
                )
            )

        if reasons:
            rejected.extend(
                EpisodeImportRejection(
                    relative_path=video.relative_path,
                    reason=reason,
                    detail=detail,
                )
                for reason, detail in reasons
            )
            continue

        result = EpisodeImportResult(video=video, parsed=parsed, episodes=episodes)
        if requested is not None and requested.isdisjoint(episodes):
            skipped_not_requested.append(result)
        else:
            accepted.append(result)

    return SeasonImportValidation(
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        skipped_not_requested=tuple(skipped_not_requested),
    )
