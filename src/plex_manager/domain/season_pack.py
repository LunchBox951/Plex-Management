"""Season-pack scope classification — what portion of a show a release covers.

TV releases come in four shapes: a single episode (``S02E05``), a multi-episode
file (``S02E05-E06``), a whole-season pack (``S02``, no episode token), or a
multi-season pack (``S01-S03``). :func:`classify_release_scope` reads the SAME
``ParsedRelease.season``/``.episode`` fields the wrong-season gate
(``media_match._season_covers``) already reads, so the scope classification can
never disagree with the identity gate about what a release names.

This is purely a *classifier* — it makes no acceptance decision. The decision
engine consumes the classification to break ties toward a season pack when the
operator explicitly requested a whole season (``prefer_season_pack``); it never
overrides the quality/identity/blocklist gates.

Pure domain: stdlib only. No I/O, no adapter/web imports.
"""

from __future__ import annotations

from typing import Literal

from plex_manager.domain.release import ParsedRelease

__all__ = ["ReleaseScope", "classify_release_scope"]

ReleaseScope = Literal["single_episode", "season_pack", "multi_season_pack", "unknown"]


def classify_release_scope(parsed: ParsedRelease) -> ReleaseScope:
    """Classify what a parsed release covers, from its season/episode fields alone.

    Decision order:

    1. ``season`` is a ``list`` with more than one entry -> ``"multi_season_pack"``
       (``S01-S03``); this is checked first because a multi-season release still
       has no ``episode`` and must not be mistaken for a single-season pack.
    2. A single season (``int``, or a one-element ``list``) with no ``episode`` ->
       ``"season_pack"`` (the whole season, no episode token in the name).
    3. A single season with an ``episode`` set (``int`` or ``list[int]``, i.e. a
       single- or multi-episode file) -> ``"single_episode"``.
    4. No season at all -> ``"unknown"`` (conservative: the name carries no scope
       information, same posture as the wrong-season gate treating a missing
       season as uncertain).
    """
    season = parsed.season
    if isinstance(season, list):
        if len(season) > 1:
            return "multi_season_pack"
        season_value = season[0] if season else None
    else:
        season_value = season

    if season_value is None:
        return "unknown"
    if parsed.episode is None:
        return "season_pack"
    return "single_episode"
