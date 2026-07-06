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

from collections.abc import Sequence
from typing import Literal

from plex_manager.domain.release import ParsedRelease

__all__ = [
    "ReleaseScope",
    "classify_release_scope",
    "covers_requested_episodes",
    "episode_numbers",
]

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


def episode_numbers(episode: int | list[int] | None) -> tuple[int, ...]:
    """Normalize a parsed ``episode`` field to a sorted, deduplicated tuple.

    ``None`` (no episode number at all) collapses to an empty tuple. Shared by
    :func:`covers_requested_episodes` and
    :mod:`plex_manager.domain.import_validation` (imported there as
    ``_episode_numbers``) so the two never disagree about what a parsed
    ``episode`` field means.
    """
    if episode is None:
        return ()
    if isinstance(episode, int):
        return (episode,)
    return tuple(sorted(set(episode)))


def covers_requested_episodes(parsed: ParsedRelease, requested: Sequence[int]) -> bool:
    """Return ``True`` when ``parsed`` plausibly contains one of ``requested``.

    Used by :func:`plex_manager.services.decision_service.preview` to stop a
    release that names the RIGHT season but the WRONG episode from ever being
    grab-selectable, even when a tracker ignores (or can't narrow to) the
    requested episode(s) in its search results.

    Decision, via :func:`classify_release_scope`:

    - ``"season_pack"`` / ``"multi_season_pack"`` -> ``True``. A pack covering the
      requested season inherently contains the requested episode(s), so this
      episode-overlap helper never rejects a pack.

      *Division of authority (issue #24 beta posture).* A multi-season pack
      (``S01-S03``) IS refused for the beta -- this app's one-download-one-season
      model can't satisfy several seasons from one grab -- but that refusal is
      owned SOLELY by :func:`plex_manager.domain.decision_engine.decide`'s
      multi-season gate, which fires with the accurate
      :attr:`~plex_manager.domain.quality_service.RejectionReason.MULTI_SEASON_PACK`.
      This helper only backs the media-identity gate (``matches_media``), which
      runs FIRST; if it returned ``False`` for a multi-season pack, that pack
      would surface as ``WRONG_MEDIA`` before the decision engine could attribute
      the true reason -- an honesty-over-silence violation. So the gates stay
      divided: identity (here) never rejects a pack; the multi-season gate (there)
      is the single rejection authority. The two never disagree that a
      multi-season pack is ultimately un-grabbable -- they only agree on WHICH
      gate surfaces WHY.
    - ``"single_episode"`` -> ``True`` iff the file's own episode number(s)
      (normalized via :func:`episode_numbers`) COVER the WHOLE requested set —
      ``set(requested).issubset(...)`` (issue #70). ANY-overlap (the old posture)
      let an ``S02E04`` single-episode release pass an ``episodes=[4, 5]`` request,
      so the engine could grab a partial release and then block import because
      import validation requires EVERY requested episode. Requiring full coverage
      here means a partial single-episode release is rejected at preview, and a
      complete multi-episode release / pack wins instead. A multi-episode file may
      still carry MORE episodes than requested (its extras ride along, a file
      cannot be split) — only the requested episodes must all be present.

      *Accepted gap (not handled here):* anime absolute numbering and specials
      (season-0/E00) are NOT normalized against the requested set — an absolute-
      numbered anime release whose parsed episode(s) don't line up with the
      canonical requested numbers can still be under-covered. Building an
      absolute<->canonical numbering table is out of scope for this fix.
    - ``"unknown"`` (no season at all) -> ``False``. Conservative: mirrors the
      posture already used by the missing-season identity gate and the
      importer's ``NO_EPISODE_NUMBER`` rule -- an unparseable release is never
      proof that it covers the wanted episode.
    """
    scope = classify_release_scope(parsed)
    if scope in ("season_pack", "multi_season_pack"):
        return True
    if scope == "single_episode":
        # Full-coverage (superset) gate, NOT any-overlap (issue #70): the release's
        # own episode set must contain EVERY requested episode. A multi-episode file
        # may carry more than requested (extras ride along), but a partial single-
        # episode release (S02E04 for a requested {4, 5}) is rejected here rather
        # than grabbed and later blocked at import for the missing episode.
        return set(requested).issubset(episode_numbers(parsed.episode))
    return False
