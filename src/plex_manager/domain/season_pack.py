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
from dataclasses import dataclass
from typing import Literal

from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.release import ParsedRelease

__all__ = [
    "MultiSeasonPackPlan",
    "MultiSeasonRequestIntent",
    "MultiSeasonRequestMode",
    "ReleaseScope",
    "SeasonPackSeasonState",
    "classify_release_scope",
    "covers_requested_episodes",
    "episode_numbers",
    "plan_multi_season_pack",
]

ReleaseScope = Literal["single_episode", "season_pack", "multi_season_pack", "unknown"]
MultiSeasonRequestMode = Literal["whole_show", "explicit_seasons"]

_INSTALLED_STATUSES = frozenset({"completed", "available"})


@dataclass(frozen=True)
class SeasonPackSeasonState:
    """Current request/library state for one tracked TV season."""

    season_number: int
    status: str
    installed_quality_id: int | None = None
    installed_profile_index: int | None = None


@dataclass(frozen=True)
class MultiSeasonRequestIntent:
    """Request intent and tracked season rows used to evaluate a multi-season pack."""

    mode: MultiSeasonRequestMode
    requested_seasons: tuple[int, ...]
    seasons: tuple[SeasonPackSeasonState, ...]


@dataclass(frozen=True)
class MultiSeasonPackPlan:
    """Eligibility decision for one multi-season pack candidate."""

    accepted: bool
    reason: str | None
    covered_seasons: tuple[int, ...]
    target_seasons: tuple[int, ...]
    upgrade_seasons: tuple[int, ...]
    waste_seasons: tuple[int, ...]
    ignored_seasons: tuple[int, ...]


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
      (normalized via :func:`episode_numbers`) overlap ``requested`` at all — a
      multi-episode file with even partial overlap is kept, mirroring
      :func:`plex_manager.domain.import_validation.validate_season_import`'s
      ``skipped_not_requested`` posture at import time.
    - ``"unknown"`` (no season at all) -> ``False``. Conservative: mirrors the
      posture already used by the missing-season identity gate and the
      importer's ``NO_EPISODE_NUMBER`` rule -- an unparseable release is never
      proof that it covers the wanted episode.
    """
    scope = classify_release_scope(parsed)
    if scope in ("season_pack", "multi_season_pack"):
        return True
    if scope == "single_episode":
        requested_set = set(requested)
        return not requested_set.isdisjoint(episode_numbers(parsed.episode))
    return False


def _profile_index(
    profile: QualityProfile,
    quality_id: int | None,
    fallback_index: int | None = None,
) -> int | None:
    if quality_id is None:
        return None
    index = profile.get_index(quality_id)
    return index if index is not None else fallback_index


def plan_multi_season_pack(
    *,
    pack_seasons: Sequence[int],
    candidate_quality_id: int,
    profile: QualityProfile,
    intent: MultiSeasonRequestIntent,
) -> MultiSeasonPackPlan:
    """Classify whether a multi-season pack is useful enough to grab.

    This is the pure policy behind issue #24's follow-up behaviour. It does not
    create seasons and does not perform the grab; it only answers which currently
    tracked seasons a physical pack should satisfy.
    """
    covered = tuple(sorted(set(pack_seasons)))
    requested = set(intent.requested_seasons)
    state_by_season = {state.season_number: state for state in intent.seasons}
    tracked = set(state_by_season)

    if intent.mode == "explicit_seasons" and set(covered) != requested:
        return MultiSeasonPackPlan(
            accepted=False,
            reason="explicit_season_set_mismatch",
            covered_seasons=covered,
            target_seasons=(),
            upgrade_seasons=(),
            waste_seasons=(),
            ignored_seasons=tuple(sorted(set(covered) - requested)),
        )

    eligible = (requested or tracked) & tracked
    ignored = tuple(sorted(set(covered) - eligible))
    candidate_index = profile.get_index(candidate_quality_id)

    target: list[int] = []
    upgrades: list[int] = []
    waste: list[int] = []

    for season_number in covered:
        if season_number not in eligible:
            continue
        state = state_by_season.get(season_number)
        if state is None:
            continue
        if state.status not in _INSTALLED_STATUSES:
            target.append(season_number)
            continue

        installed_index = _profile_index(
            profile,
            state.installed_quality_id,
            state.installed_profile_index,
        )
        if (
            profile.upgrade_allowed
            and candidate_index is not None
            and installed_index is not None
            and candidate_index > installed_index
        ):
            target.append(season_number)
            upgrades.append(season_number)
        else:
            waste.append(season_number)

    target_seasons = tuple(target)
    waste_seasons = tuple(waste)
    reason: str | None = None
    accepted = True
    if not target_seasons:
        accepted = False
        reason = "no_useful_seasons"
    elif waste_seasons and len(target_seasons) <= len(waste_seasons):
        accepted = False
        reason = "useful_seasons_not_majority"

    return MultiSeasonPackPlan(
        accepted=accepted,
        reason=reason,
        covered_seasons=covered,
        target_seasons=target_seasons,
        upgrade_seasons=tuple(upgrades),
        waste_seasons=waste_seasons,
        ignored_seasons=ignored,
    )
