"""Decision engine — the assembled pure brain (parse -> gate -> filter -> rank).

Mirrors Radarr's decision-engine specification pipeline, in a fixed order:

1. **parse** each candidate title via the injected :class:`ParserPort`;
2. **media-identity gate** (injected ``media_match``) — a release that does not
   name the wanted title (an indexer that ignored the id, a stale mapping) is a
   *permanent* rejection and is never scored; this guards the north star "do NOT
   grab the wrong media" and runs BEFORE quality so a high-quality wrong release
   can never out-rank a correct one;
3. **multi-season-pack gate** (:func:`~plex_manager.domain.season_pack.classify_release_scope`)
   — a release spanning more than one season (``S01-S03``) is a *permanent*
   rejection, mirroring Sonarr's ``MultiSeasonSpecification``: this app's one
   download == one season data model can't satisfy several seasons from a single
   grab without stranding sibling ``SeasonRequest``s, so the beta posture is to
   never grab one, not to prefer it (see ADR/issue #24);
4. **season-pack-only gate** (``prefer_season_pack``) — when the operator
   explicitly requested a WHOLE season (no specific episodes named), a release
   that is not itself a season pack is a *permanent* rejection, never scored:
   a single-episode release can never satisfy a whole-season request, so it must
   never be auto-grabbed just because every season pack was exhausted/blocklisted
   (issue #167 -- "The Last Man on Earth" S04 was auto-grabbed as single episodes
   three times in production before this gate existed);
5. **quality hard gate** (:func:`check_quality`) — a disallowed/absent quality is
   a *permanent* rejection and is never scored (north-star hard cutoff);
6. **blocklist filter** — a previously failed/reported release is an
   unconditional skip;
7. **score / sort** the survivors, best-first.

There is deliberately **no relaxed-fallback retry**: if nothing survives, the
result carries ``no_acceptable_release=True`` as an observable state. The engine
never falls back to accepting a blocked or rejected source.

Ranking among allowed releases uses :func:`compare_by_profile` as the primary key
(profile order, not raw resolution), then seeders descending, then size as a
stable final tiebreak. The numeric :attr:`ScoredRelease.score` is assigned AFTER
that sort as a strictly-decreasing projection of each release's final rank (best
= highest), so it can never contradict the accepted order -- ``_compare``, not
the score, is the sole ordering authority.

Pure domain: ports Protocols + the local quality/release model + stdlib.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cmp_to_key

from plex_manager.domain.quality_profile import QualityProfile
from plex_manager.domain.quality_service import (
    RejectionReason,
    check_quality,
    compare_by_profile,
)
from plex_manager.domain.release import CandidateRelease, ParsedRelease, ScoredRelease
from plex_manager.domain.season_pack import (
    MultiSeasonRequestIntent,
    classify_release_scope,
    plan_multi_season_pack,
)
from plex_manager.domain.source_mapping import resolve_quality
from plex_manager.ports.parser import ParserPort

# Release scopes that satisfy a WHOLE-season grab: an exact single-season pack.
# When ``prefer_season_pack`` is set, anything outside this set is a *permanent*
# rejection (the gate below, issue #167) -- a single-episode release can never
# satisfy a whole-season request, so it must never be accepted just because
# every season pack was exhausted/blocklisted.
#
# A multi-season pack (``S01-S03``) is DELIBERATELY excluded: it is permanently
# rejected below (the ``_MULTI_SEASON_SCOPE`` gate) rather than preferred, so it
# never reaches scoring in the first place. See issue #24.
_PACK_SCOPES: frozenset[str] = frozenset({"season_pack"})

# The release scope :func:`classify_release_scope` reports for a pack that spans
# more than one season (``S01-S03``). Kept as a named constant so the gate and its
# tests can't drift from the classifier's string literal.
_MULTI_SEASON_SCOPE = "multi_season_pack"

__all__ = ["BlocklistCheck", "DecisionResult", "MediaMatchCheck", "decide"]

# Returns True when the (candidate, parsed) pair is blocklisted. The caller wires
# this to a BlocklistRepository-backed check; the engine stays pure.
BlocklistCheck = Callable[[CandidateRelease, ParsedRelease], bool]

# Returns True when the (candidate, parsed) pair actually names the wanted media.
# The caller builds this from the request's expected (title, year, tmdb id) via
# the pure ``matches_media`` helper; the engine stays pure and only sees the hook.
MediaMatchCheck = Callable[[CandidateRelease, ParsedRelease], bool]


@dataclass(frozen=True)
class DecisionResult:
    """Outcome of a decision run over a candidate set.

    ``accepted`` is sorted best-first. ``rejected`` pairs each discarded candidate
    with its (surfaced, never-swallowed) reason. ``no_acceptable_release`` is True
    iff ``accepted`` is empty.

    Both collections are immutable tuples (issue #106): a frozen dataclass blocks
    reassigning ``result.accepted`` but NOT mutating a plain list in place, and a
    caller appending/sorting a shared ``DecisionResult`` (e.g. across the several
    read sites in ``auto_grab_service``/``correction_service``/``queue.py``) would
    silently corrupt every other holder of the same result.
    """

    accepted: tuple[ScoredRelease, ...]
    rejected: tuple[tuple[CandidateRelease, RejectionReason], ...]
    no_acceptable_release: bool


def decide(
    candidates: list[CandidateRelease],
    parser: ParserPort,
    profile: QualityProfile,
    media_match: MediaMatchCheck,
    is_blocklisted: BlocklistCheck,
    *,
    prefer_season_pack: bool = False,
    multi_season_intent: MultiSeasonRequestIntent | None = None,
) -> DecisionResult:
    """Run the parse -> match -> gate -> filter -> rank pipeline over ``candidates``.

    ``prefer_season_pack`` (default ``False``, byte-identical to the pre-season-pack
    engine) is set by the caller only when the operator explicitly requested a
    whole TV season: any release :func:`~plex_manager.domain.season_pack.classify_release_scope`
    does NOT classify as a ``"season_pack"`` then becomes a *permanent* rejection,
    never scored (issue #167) -- a single-episode release can never satisfy a
    whole-season request, so it must never be auto-grabbed just because every
    season pack was exhausted/blocklisted. It never overrides the identity/
    multi-season gates that run before it -- a release that fails one of those is
    still rejected for that gate's reason -- and a season pack still must clear
    the quality gate after it to be accepted.
    """
    accepted: list[ScoredRelease] = []
    rejected: list[tuple[CandidateRelease, RejectionReason]] = []

    for candidate in candidates:
        parsed = parser.parse(candidate.title)

        # Media-identity gate FIRST: a release for a different movie/show is never
        # scored, so a high-quality wrong release can't out-rank a correct one.
        if not media_match(candidate, parsed):
            rejected.append((candidate, RejectionReason.WRONG_MEDIA))
            continue

        scope = classify_release_scope(parsed)
        multi_season_plan = None
        if scope == _MULTI_SEASON_SCOPE and multi_season_intent is None:
            rejected.append((candidate, RejectionReason.MULTI_SEASON_PACK))
            continue

        # Season-pack-only gate (issue #167): the operator explicitly requested a
        # WHOLE season, so a release that is not itself a season pack can never
        # satisfy the request -- it is a *permanent* rejection, never scored, not
        # merely a scoring tiebreak. Without this, a single-episode release used
        # to survive to scoring (and get auto-grabbed) once every season pack was
        # exhausted/blocklisted -- confirmed live ("The Last Man on Earth" S04 was
        # auto-grabbed as single episodes three times).
        multi_season_with_intent = scope == _MULTI_SEASON_SCOPE and multi_season_intent is not None
        if prefer_season_pack and scope not in _PACK_SCOPES and not multi_season_with_intent:
            rejected.append((candidate, RejectionReason.NOT_SEASON_PACK))
            continue

        quality = resolve_quality(parsed.source, parsed.resolution, parsed.modifier)

        verdict = check_quality(quality, profile)
        if not verdict.accepted:
            rejected.append((candidate, verdict.reason or RejectionReason.QUALITY_NOT_WANTED))
            continue

        if scope == _MULTI_SEASON_SCOPE:
            if multi_season_intent is None:  # pragma: no cover - guarded above
                rejected.append((candidate, RejectionReason.MULTI_SEASON_PACK))
                continue
            season_numbers = parsed.season if isinstance(parsed.season, list) else []
            multi_season_plan = plan_multi_season_pack(
                pack_seasons=season_numbers,
                candidate_quality_id=quality.id,
                profile=profile,
                intent=multi_season_intent,
            )
            if not multi_season_plan.accepted:
                rejected.append((candidate, RejectionReason.MULTI_SEASON_PACK))
                continue

        if is_blocklisted(candidate, parsed):
            rejected.append((candidate, RejectionReason.BLOCKLISTED))
            continue

        # The quality passed the gate, so it is present in the profile.
        index = profile.get_index(quality.id)
        profile_index = index if index is not None else -1
        accepted.append(
            ScoredRelease(
                candidate=candidate,
                parsed=parsed,
                quality=quality,
                profile_index=profile_index,
                score=0.0,  # placeholder; real value is a post-sort rank projection (#105)
                covered_seasons=(
                    multi_season_plan.covered_seasons if multi_season_plan is not None else ()
                ),
                target_seasons=(
                    multi_season_plan.target_seasons if multi_season_plan is not None else ()
                ),
                upgrade_seasons=(
                    multi_season_plan.upgrade_seasons if multi_season_plan is not None else ()
                ),
                waste_seasons=(
                    multi_season_plan.waste_seasons if multi_season_plan is not None else ()
                ),
                ignored_seasons=(
                    multi_season_plan.ignored_seasons if multi_season_plan is not None else ()
                ),
                skipped_seasons=(
                    (multi_season_plan.waste_seasons + multi_season_plan.ignored_seasons)
                    if multi_season_plan is not None
                    else ()
                ),
            )
        )

    def _compare(left: ScoredRelease, right: ScoredRelease) -> int:
        by_quality = compare_by_profile(left.quality, right.quality, profile)
        if by_quality != 0:
            return by_quality
        # Issue #167: the season-pack-only gate above already rejects every
        # non-pack candidate before it reaches ``accepted`` when
        # ``prefer_season_pack`` is set, so ``left_pack``/``right_pack`` are now
        # always equal here and this block is a no-op for the pack-vs-non-pack
        # case. Left in place (rather than deleted) because it is harmless and
        # documents the ranking intent; it would only ever fire again if a future
        # ``_PACK_SCOPES`` grew a second member.
        if prefer_season_pack:
            left_pack = classify_release_scope(left.parsed) in _PACK_SCOPES
            right_pack = classify_release_scope(right.parsed) in _PACK_SCOPES
            if left_pack != right_pack:
                return 1 if left_pack else -1
        left_seeders = left.candidate.seeders or 0
        right_seeders = right.candidate.seeders or 0
        if left_seeders != right_seeders:
            return (left_seeders > right_seeders) - (left_seeders < right_seeders)
        left_size = left.candidate.size_bytes
        right_size = right.candidate.size_bytes
        return (left_size > right_size) - (left_size < right_size)

    accepted.sort(key=cmp_to_key(_compare), reverse=True)
    # score is a DISPLAY projection of the comparator's final rank, never an
    # input to it: assign strictly-decreasing by position so score order ==
    # accepted order by construction (#105). The comparator, not the score, is
    # the sole ordering authority.
    ranked = [
        scored.model_copy(update={"score": float(len(accepted) - position)})
        for position, scored in enumerate(accepted)
    ]
    return DecisionResult(
        accepted=tuple(ranked),
        rejected=tuple(rejected),
        no_acceptable_release=not ranked,
    )
