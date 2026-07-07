"""Guessit-field -> quality-taxonomy mapping (the safety-critical CAM/TS path).

The guessit adapter (P3) confines the third-party parser and hands the domain a
plain field mapping (``dict``) plus the raw release title. This module turns that
mapping into a :class:`QualitySource` / :class:`Resolution` / :class:`Modifier` /
:class:`Revision`, and ultimately a :class:`ParsedRelease`, then resolves a named
:class:`Quality` from the taxonomy.

Field values are guessit-normalized strings (confirmed against guessit 4.0.2):
``source`` -> ``"Telesync"``/``"HD Camera"``/``"Web"``/``"Blu-ray"`` ...;
``other`` -> ``"Rip"``/``"Screener"``/``"Remux"`` (str or list); ``screen_size``
-> ``"1080p"``; ``proper_count`` -> ``"1"`` etc.

SAFETY INVARIANT (CAM/TS hard cutoff): the supplementary reject nets ONLY ever
force a release into a *reject tier*. The source net (:func:`_reject_net`) forces
CAM/TELESYNC/TELECINE/WORKPRINT; the modifier net (:func:`_reject_modifier_net`)
forces SCREENER (-> DVDSCR) / REGIONAL (-> R5/R6) — the two hard-cutoff tiers
that hang off a *modifier* rather than a source. Neither net can promote a
release to an acceptable quality. Every value they return is rejected by the
default profile, so classifying *down* into them is always safe and classifying
*up* is impossible. Both invariants are asserted at import time.

Pure domain: stdlib (``re``) + pydantic DTOs + the local quality model. No I/O,
no guessit import here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import cast

from plex_manager.domain.quality import (
    ALL_QUALITIES,
    BLURAY480P,
    BLURAY720P,
    BRDISK,
    CAM,
    DVDR,
    DVDSCR,
    RAWHD,
    REGIONAL,
    REMUX1080P,
    REMUX2160P,
    TELECINE,
    TELESYNC,
    WORKPRINT,
    Modifier,
    Quality,
    QualitySource,
    Resolution,
)
from plex_manager.domain.quality import (
    UNKNOWN as UNKNOWN_QUALITY,
)
from plex_manager.domain.release import ParsedRelease, Revision

__all__ = [
    "map_fields",
    "map_modifier",
    "map_resolution",
    "map_revision",
    "map_source",
    "resolve_quality",
    "to_parsed_release",
]

# -- guessit ``source`` string -> QualitySource ---------------------------------
# "Web" is handled separately (WEBDL vs WEBRIP depends on the ``other`` rip flag).
_SOURCE_MAP: dict[str, QualitySource] = {
    "Camera": QualitySource.CAM,
    "HD Camera": QualitySource.CAM,
    "Telesync": QualitySource.TELESYNC,
    "HD Telesync": QualitySource.TELESYNC,
    "Telecine": QualitySource.TELECINE,
    "HD Telecine": QualitySource.TELECINE,
    "Workprint": QualitySource.WORKPRINT,
    "DVD": QualitySource.DVD,
    "HDTV": QualitySource.TV,
    "Analog HDTV": QualitySource.TV,
    "Satellite": QualitySource.TV,
    "Digital TV": QualitySource.TV,
    "Blu-ray": QualitySource.BLURAY,
    "Ultra HD Blu-ray": QualitySource.BLURAY,
}

# -- Supplementary reject-keyword net -------------------------------------------
# Catches pirate-cam variants guessit misclassifies as ``alternative_title`` or
# misses entirely (HQCAM, PDVD, ...). EVERY target here is a reject tier; this
# table must NEVER map a keyword to DVD/TV/WEBDL/WEBRIP/BLURAY. Ordered most- to
# least-specific; the first match wins. The ``[\s._-]?`` allows separator noise
# (``HD-CAM``, ``HD.CAM``, ``HDCAM``). Word boundaries keep it from firing inside
# unrelated words (e.g. "Camelot" does not match ``\bCAMERA\b``).
_REJECT_NET: tuple[tuple[re.Pattern[str], QualitySource], ...] = (
    (re.compile(r"\bHD[\s._-]?TS\b", re.IGNORECASE), QualitySource.TELESYNC),
    (re.compile(r"\bTS[\s._-]?RIP\b", re.IGNORECASE), QualitySource.TELESYNC),
    (re.compile(r"\bTELESYNC\b", re.IGNORECASE), QualitySource.TELESYNC),
    (re.compile(r"\bHD[\s._-]?TC\b", re.IGNORECASE), QualitySource.TELECINE),
    (re.compile(r"\bTC[\s._-]?RIP\b", re.IGNORECASE), QualitySource.TELECINE),
    (re.compile(r"\bTELECINE\b", re.IGNORECASE), QualitySource.TELECINE),
    (re.compile(r"\bWORK[\s._-]?PRINT\b", re.IGNORECASE), QualitySource.WORKPRINT),
    (re.compile(r"\bHQ[\s._-]?CAM\b", re.IGNORECASE), QualitySource.CAM),
    (re.compile(r"\bHD[\s._-]?CAM\b", re.IGNORECASE), QualitySource.CAM),
    (re.compile(r"\bCAM[\s._-]?RIP\b", re.IGNORECASE), QualitySource.CAM),
    (re.compile(r"\bPRE[\s._-]?DVD\b", re.IGNORECASE), QualitySource.CAM),
    (re.compile(r"\bPDVD\b", re.IGNORECASE), QualitySource.CAM),
    (re.compile(r"\bCAMERA\b", re.IGNORECASE), QualitySource.CAM),
)

# The full set of sources the reject-net is allowed to emit. Asserted at import
# time so a future edit cannot accidentally make the net promote a release.
_REJECT_SOURCES: frozenset[QualitySource] = frozenset(
    {
        QualitySource.CAM,
        QualitySource.TELESYNC,
        QualitySource.TELECINE,
        QualitySource.WORKPRINT,
    }
)
assert all(source in _REJECT_SOURCES for _, source in _REJECT_NET)  # noqa: S101

# -- Supplementary reject-MODIFIER net ------------------------------------------
# Parallel to ``_REJECT_NET`` but for the two hard-cutoff tiers that hang off a
# *modifier* rather than a source: SCREENER (-> DVDSCR, id 28) and REGIONAL
# (-> R5/R6, id 29). guessit reliably emits ``other:"Screener"`` and
# ``other:"Region 5"`` for the canonical spellings, but it MISSES non-standard
# tags (verified: ``...HC.SCR.WEB...`` yields no ``other`` at all, and ``R6``
# never emits ``Region 6``). Without this net those leak through as an acceptable
# WEBDL/DVD. Like the source net, every target is a reject tier; this table must
# NEVER map a keyword to a promoting modifier (NONE/REMUX/RAWHD/BRDISK). Ordered
# most- to least-specific; first match wins.
_REJECT_MODIFIER_NET: tuple[tuple[re.Pattern[str], Modifier], ...] = (
    (re.compile(r"\bDVD[\s._-]?SCR\b", re.IGNORECASE), Modifier.SCREENER),
    (re.compile(r"\bBD[\s._-]?SCR\b", re.IGNORECASE), Modifier.SCREENER),
    (re.compile(r"\bSCREENER\b", re.IGNORECASE), Modifier.SCREENER),
    (re.compile(r"\bSCR\b", re.IGNORECASE), Modifier.SCREENER),
    (re.compile(r"\bR5\b", re.IGNORECASE), Modifier.REGIONAL),
    (re.compile(r"\bR6\b", re.IGNORECASE), Modifier.REGIONAL),
)

# The full set of modifiers the reject net is allowed to emit. Asserted at import
# time so a future edit cannot make the net promote a release.
_REJECT_MODIFIERS: frozenset[Modifier] = frozenset({Modifier.SCREENER, Modifier.REGIONAL})
assert all(  # noqa: S101
    modifier in _REJECT_MODIFIERS for _, modifier in _REJECT_MODIFIER_NET
)

_RESOLUTION_BY_HEIGHT: dict[int, Resolution] = {
    360: Resolution.R360P,
    480: Resolution.R480P,
    540: Resolution.R540P,
    576: Resolution.R576P,
    720: Resolution.R720P,
    1080: Resolution.R1080P,
    2160: Resolution.R2160P,
}

_REJECT_QUALITY_BY_SOURCE: dict[QualitySource, Quality] = {
    QualitySource.CAM: CAM,
    QualitySource.TELESYNC: TELESYNC,
    QualitySource.TELECINE: TELECINE,
    QualitySource.WORKPRINT: WORKPRINT,
}


def _as_str_list(value: object) -> list[str]:
    """Normalize a guessit field (str | list | None | other) to ``list[str]``."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        sequence = cast("list[object] | tuple[object, ...]", value)
        return [str(item) for item in sequence]
    return [str(value)]


def _strip_release_group(raw_title: str, fields: Mapping[str, object]) -> str:
    """Remove the guessit-identified release-group span from ``raw_title``.

    Feeds only the two raw-title reject nets (:func:`_reject_net`,
    :func:`_reject_modifier_net`); the guessit-native ``other`` checks in
    :func:`map_source`/:func:`map_modifier` are untouched. A group named e.g.
    ``SCR``/``R5``/``HQCAM`` would otherwise false-trip a reject net meant to
    catch those tokens in the *release description*, not in an arbitrary
    group tag. Absent/empty/non-str ``release_group`` -> unchanged behavior.

    Every *attached* occurrence of the group is removed -- a token-bounded match
    immediately preceded by ``-`` or ``[``, the two positions where a release
    group is conventionally attached (Radarr's ``ReleaseGroupParser`` recognizes
    exactly these shapes: the scene ``-GROUP`` suffix and the bracketed
    ``[GROUP]`` tag). Stripping *all* attached occurrences (not just the last)
    matters because import validation parses the full relative *path*, so a group
    whose name appears on BOTH the release folder and the filename -- e.g. group
    ``SCR`` in ``Movie...x264-SCR/Movie...x264-SCR.mkv`` -- would otherwise leave
    the folder's ``-SCR`` behind to false-trip the reject net and wrongly reject
    a clean import.

    Anchoring on the attachment character is what keeps a genuine *body token*
    that collides with the group name intact: in
    ``Movie.2024.1080p.HC.SCR.WEB-DL.x264-SCR`` the hardcoded-screener marker
    ``HC.SCR`` is dot-attached, so only the ``-SCR`` group tag is stripped and
    the reject net still sees the real screener token. The right-side
    not-followed-by-a-word-char flank likewise keeps the group name inside a
    longer run (``SCR`` in ``DVDSCR``, ``Scr`` in ``Scream``) untouched. This
    removes only group *tags*, never body mentions; the guessit-native checks
    remain the primary defense for real reject tokens.
    """
    group = fields.get("release_group")
    if not isinstance(group, str) or not group:
        return raw_title
    # ``(?<=[-\[])`` requires the attachment character and deliberately leaves it
    # in place (only the group token is removed). ``(?!\w)`` bounds the right
    # edge to a whole token; ``_`` counts as a word char exactly as the ``\b``
    # boundaries in the reject nets treat it, so the strip removes precisely the
    # attached spans a net could otherwise fire on -- and nothing else.
    pattern = re.compile(rf"(?<=[-\[]){re.escape(group)}(?!\w)", re.IGNORECASE)
    return pattern.sub("", raw_title)


def _reject_net(raw_title: str) -> QualitySource | None:
    """Return a reject-tier source if a pirate-cam keyword is present, else None.

    INVARIANT: the return value is always ``None`` or a member of
    ``_REJECT_SOURCES``. It is structurally incapable of promoting a release.
    """
    for pattern, source in _REJECT_NET:
        if pattern.search(raw_title):
            return source
    return None


def _reject_modifier_net(raw_title: str) -> Modifier | None:
    """Return SCREENER/REGIONAL if a reject-modifier keyword is present, else None.

    INVARIANT: the return value is always ``None`` or a member of
    ``_REJECT_MODIFIERS``. It is structurally incapable of promoting a release.
    """
    for pattern, modifier in _REJECT_MODIFIER_NET:
        if pattern.search(raw_title):
            return modifier
    return None


def map_source(fields: Mapping[str, object], raw_title: str) -> QualitySource:
    """Map guessit ``source`` (+ rip flag) to a :class:`QualitySource`.

    The reject-keyword net runs last and *overrides* the guessit classification,
    but only ever downward into a reject tier (never to an acceptable source).
    """
    others = {item.casefold() for item in _as_str_list(fields.get("other"))}
    raw_source = fields.get("source")
    base = QualitySource.UNKNOWN
    if isinstance(raw_source, str):
        if raw_source == "Web":
            base = QualitySource.WEBRIP if "rip" in others else QualitySource.WEBDL
        else:
            base = _SOURCE_MAP.get(raw_source, QualitySource.UNKNOWN)

    forced = _reject_net(_strip_release_group(raw_title, fields))
    if forced is not None:
        return forced
    return base


def map_resolution(fields: Mapping[str, object]) -> Resolution:
    """Map guessit ``screen_size`` (``"1080p"``, ``"1080i"``, ...) to a height."""
    screen_size = fields.get("screen_size")
    if not isinstance(screen_size, str):
        return Resolution.UNKNOWN
    match = re.search(r"(\d{3,4})", screen_size)
    if match is None:
        return Resolution.UNKNOWN
    return _RESOLUTION_BY_HEIGHT.get(int(match.group(1)), Resolution.UNKNOWN)


def map_modifier(fields: Mapping[str, object], raw_title: str) -> Modifier:
    """Map guessit ``other`` markers (+ a raw-title reject net) to a Modifier.

    SCREENER and REGIONAL are hard-cutoff reject tiers, so they win over REMUX (a
    quality *boost*): a release that is both screener and remux must reject.
    guessit emits ``other:"Screener"`` for DVDSCR and ``other:"Region 5"`` for R5,
    but it misfiles non-standard tags (no ``other`` for ``HC.SCR.WEB``; no
    ``Region 6`` for R6). The supplementary :func:`_reject_modifier_net` backstops
    those exactly as the source net backstops misfiled CAM/TS keywords. The nets
    can ONLY force SCREENER/REGIONAL — never a promotion. BR-disk and raw-HD are
    not emitted by guessit and stay ``NONE`` in the alpha.
    """
    others = {item.casefold() for item in _as_str_list(fields.get("other"))}
    if "screener" in others:
        return Modifier.SCREENER
    if any("region" in other for other in others):
        return Modifier.REGIONAL
    forced = _reject_modifier_net(_strip_release_group(raw_title, fields))
    if forced is not None:
        return forced
    if "remux" in others:
        return Modifier.REMUX
    return Modifier.NONE


def map_revision(fields: Mapping[str, object], raw_title: str) -> Revision:
    """Build a :class:`Revision` from guessit ``proper_count`` + raw-title markers.

    guessit reports both PROPER and REPACK as ``other:"Proper"`` with a
    ``proper_count``; REPACK vs PROPER and REAL are recovered from the raw title.
    """
    version = 1
    proper_count = fields.get("proper_count")
    if isinstance(proper_count, (str, int)):
        try:
            version = int(proper_count) + 1
        except (TypeError, ValueError):
            version = 1
    is_repack = re.search(r"\bREPACK\b", raw_title, re.IGNORECASE) is not None
    real = len(re.findall(r"\bREAL\b", raw_title, re.IGNORECASE))
    return Revision(version=version, is_repack=is_repack, real=real)


def map_fields(
    fields: Mapping[str, object], raw_title: str
) -> tuple[QualitySource, Resolution, Modifier, Revision]:
    """Map a guessit field mapping to the four classification primitives."""
    return (
        map_source(fields, raw_title),
        map_resolution(fields),
        map_modifier(fields, raw_title),
        map_revision(fields, raw_title),
    )


def _coerce_year(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _coerce_season(value: object) -> int | list[int] | None:
    """Normalize guessit's ``season`` field to ``int`` | ``list[int]`` | ``None``.

    guessit yields a single ``int`` for ``SxxExx`` / a single-season pack and a
    ``list[int]`` for a multi-season pack (``S01-S03``). ``bool`` is excluded (it
    subclasses ``int``); a list is filtered to its ``int`` members and collapsed
    to ``None`` when empty, so the wrong-season gate never sees a bogus value.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        sequence = cast("list[object] | tuple[object, ...]", value)
        seasons = [
            item for item in sequence if isinstance(item, int) and not isinstance(item, bool)
        ]
        return seasons or None
    return None


def _coerce_episode(value: object) -> int | list[int] | None:
    """Normalize guessit's ``episode`` field to ``int`` | ``list[int]`` | ``None``.

    Mirrors :func:`_coerce_season` exactly: guessit yields a single ``int`` for
    ``SxxExx`` and a ``list[int]`` for a multi-episode file (``S02E05E06``). A
    whole season pack has no ``episode`` field at all -> ``None``, which is what
    :func:`plex_manager.domain.season_pack.classify_release_scope` reads to tell a
    season pack from a single/multi-episode file. ``bool`` is excluded (it
    subclasses ``int``); a list is filtered to its ``int`` members and collapsed to
    ``None`` when empty.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)):
        sequence = cast("list[object] | tuple[object, ...]", value)
        episodes = [
            item for item in sequence if isinstance(item, int) and not isinstance(item, bool)
        ]
        return episodes or None
    return None


def to_parsed_release(fields: Mapping[str, object], raw_title: str) -> ParsedRelease:
    """Assemble a :class:`ParsedRelease` from a guessit field mapping + raw title."""
    source, resolution, modifier, revision = map_fields(fields, raw_title)
    clean_title = _coerce_str(fields.get("title")) or raw_title
    return ParsedRelease(
        raw_title=raw_title,
        clean_title=clean_title,
        year=_coerce_year(fields.get("year")),
        season=_coerce_season(fields.get("season")),
        episode=_coerce_episode(fields.get("episode")),
        source=source,
        resolution=resolution,
        modifier=modifier,
        revision=revision,
        release_group=_coerce_str(fields.get("release_group")),
        languages=_as_str_list(fields.get("language")),
        edition=_coerce_str(fields.get("edition")),
        hardcoded_subs=None,
    )


def resolve_quality(source: QualitySource, resolution: Resolution, modifier: Modifier) -> Quality:
    """Resolve a named :class:`Quality` from source/resolution/modifier.

    Mirrors Radarr's ``QualityFinder``: **source wins over resolution** for the
    reject tiers (a ``1080p HDTS`` is still TELESYNC), modifier-specific tiers map
    next, then an exact source+resolution lookup, then a conservative source-only
    fallback (lowest weight, so an ambiguous parse can never over-promote).
    """
    reject = _REJECT_QUALITY_BY_SOURCE.get(source)
    if reject is not None:
        return reject

    if modifier == Modifier.SCREENER:
        return DVDSCR
    if modifier == Modifier.REGIONAL:
        return REGIONAL
    if modifier == Modifier.REMUX:
        if source in (QualitySource.BLURAY, QualitySource.UNKNOWN):
            # Mirrors Radarr's remux handling for a BluRay source (QualityParser
            # bluray branch) and for NO source (the sourceMatch == null && remux
            # branch): 2160p -> Remux2160p, 1080p -> Remux1080p, and below the
            # remux floor the release is still a *BluRay-family* file, so 720p ->
            # Bluray720p and 480p -> Bluray480p rather than Unknown. Without the
            # explicit 720p/480p arms, a no-source remux at those resolutions
            # fell to the UNKNOWN guard and a previously-accepted release was
            # rejected outright.
            if resolution == Resolution.R2160P:
                return REMUX2160P
            if resolution == Resolution.R1080P:
                return REMUX1080P
            if resolution == Resolution.R720P:
                return BLURAY720P
            if resolution == Resolution.R480P:
                return BLURAY480P
            if source == QualitySource.BLURAY and resolution == Resolution.UNKNOWN:
                # A BluRay remux with no parseable resolution: mirror Radarr's
                # QualityParser bluray branch, which after its resolution-specific
                # returns treats a still-unresolved sourced remux as Remux1080p
                # ("Treat a remux without a source as 1080p, not 720p; 720p remux
                # should fallback as 720p BluRay"). Without this, a clear BluRay
                # remux fell through to the source-only fallback (Bluray-480p),
                # under-ranking it below plain 720p/1080p.
                return REMUX1080P
            # Remaining cells fall through: a *known* BluRay resolution at
            # 360p/540p/576p takes the source+resolution lookup, and an UNKNOWN
            # source at those resolutions (or with no resolution at all) hits the
            # UNKNOWN guard -- exactly Radarr's outcome, whose no-source branch
            # skips them and whose QualityFinder(BLURAY, 480, REMUX) fallback
            # resolves to Unknown (no remux tier exists at SD).
        elif source == QualitySource.DVD:
            # Conservative in-tier choice (documented divergence from Radarr,
            # which yields plain DVD for a remux word and reserves DVD-R for
            # disc tokens): a DVD source with a remux word never resolves to a
            # BluRay-Remux tier.
            return DVDR
        # WEBDL/WEBRIP/TV ignore the modifier and fall through unchanged.
    if modifier == Modifier.BRDISK:
        return BRDISK
    if modifier == Modifier.RAWHD:
        return RAWHD

    if source == QualitySource.UNKNOWN:
        return UNKNOWN_QUALITY

    for quality in ALL_QUALITIES:
        if (
            quality.source == source
            and quality.resolution == resolution
            and quality.modifier == Modifier.NONE
        ):
            return quality

    candidates = [
        quality
        for quality in ALL_QUALITIES
        if quality.source == source and quality.modifier == Modifier.NONE
    ]
    if candidates:
        return min(candidates, key=lambda quality: quality.weight)
    return UNKNOWN_QUALITY
