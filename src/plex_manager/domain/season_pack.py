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
_SEARCHABLE_STATUSES = frozenset({"pending", "searching", "no_acceptable_release", "failed"})
# Seasons a *physical* multi-season pack would re-download from scratch: a season
# whose own torrent is already ``downloading``, or that finished-but-import-blocked
# (its bytes are on disk / in flight), is DUPLICATED by a pack that also carries it.
# Unlike an installed season -- ride-along the waste/majority policy tolerates when
# the useful seasons outnumber it -- an in-flight overlap is never harmless: the pack
# collides with a live torrent for the same season (issue #409), so ANY such overlap
# rejects the whole candidate rather than being buried as ignored coverage.
_IN_FLIGHT_STATUSES = frozenset({"downloading", "import_blocked"})


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
    # Covered seasons already in flight under their OWN torrent (``downloading`` /
    # ``import_blocked``) that this physical pack would re-download. A non-empty
    # tuple always pairs with ``accepted=False`` and ``reason="covered_season_in_flight"``.
    in_flight_seasons: tuple[int, ...]


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

    A physical multi-season pack claims only its ``target_seasons`` logically, but
    downloads EVERY ``covered_seasons`` entry. So if any covered season is already
    in flight under its own torrent (``downloading``/``import_blocked``), grabbing
    the pack would re-download content already being fetched -- the exact Suits
    S01-S09-over-individual-packs duplication of issue #409. Such an overlap is a
    hard rejection (``reason="covered_season_in_flight"``), NOT harmless ignored
    coverage; the decision engine then falls through to a same-season pack lower in
    the same result. The installed-season waste/majority policy still applies only
    to ``completed``/``available`` overlaps, whose bytes are settled on disk.
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
            in_flight_seasons=(),
        )

    eligible = (requested or tracked) & tracked
    ignored_set = set(covered) - eligible
    candidate_index = profile.get_index(candidate_quality_id)

    target: list[int] = []
    upgrades: list[int] = []
    waste: list[int] = []
    in_flight: list[int] = []

    for season_number in covered:
        if season_number not in eligible:
            continue
        state = state_by_season.get(season_number)
        if state is None:
            continue
        if state.status in _SEARCHABLE_STATUSES:
            target.append(season_number)
            continue
        if state.status in _IN_FLIGHT_STATUSES:
            # A live torrent already covers this season; the pack would duplicate it.
            in_flight.append(season_number)
            continue
        if state.status not in _INSTALLED_STATUSES:
            ignored_set.add(season_number)
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
            # Replacement is not implemented yet: importing an upgrade target would
            # collide with the installed season files and block the shared torrent.
            waste.append(season_number)
        else:
            waste.append(season_number)

    target_seasons = tuple(target)
    waste_seasons = tuple(waste)
    in_flight_seasons = tuple(in_flight)
    ignored = tuple(sorted(ignored_set))
    reason: str | None = None
    accepted = True
    if in_flight_seasons:
        # Highest precedence: an already-in-flight overlap makes the physical pack
        # redundant regardless of how many other seasons it would usefully serve.
        accepted = False
        reason = "covered_season_in_flight"
    elif not target_seasons:
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
        in_flight_seasons=in_flight_seasons,
    )
